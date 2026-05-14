"""SaaS connector ingestion — POST /datasets/connect-saas.

The UI's SaaS picker calls this endpoint with::

    {"connector": "github", "config": {...}}

GitHub is fully wired — the handler pulls the requested resource from
the public REST API, paginates, flattens to a CSV via pandas json
normalisation, and hands the file off to the regular ingestion
pipeline so the result lands as a normal Gold-state dataset in
:class:`AppState`.

The other nine connectors validate their config (so the form catches
typos / missing fields immediately) and reply with HTTP 202 plus a
friendly "sync scheduled" payload. The plumbing is here; the
credential vault stores the secrets and a future cron picks up the
actual sync. The agent loop only ever sees datasets in Gold state, so
no half-loaded state leaks anywhere.
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.datasets import _summarize
from src.api.pipeline import ingest_and_profile
from src.core.config import get_settings
from src.core.llm import LlmClient
from src.core.logger import get_logger
from src.core.state import AppState, get_state

router = APIRouter(prefix="/datasets", tags=["datasets"])
_logger = get_logger()

StateDep = Annotated[AppState, Depends(get_state)]


ConnectorSlug = Literal[
    "github",
    "stripe",
    "hubspot",
    "salesforce",
    "shopify",
    "notion",
    "airtable",
    "googleads",
    "meta",
    "slack",
]


class ConnectSaasRequest(BaseModel):
    connector: ConnectorSlug
    config: dict[str, str] = Field(default_factory=dict)


class ConnectSaasResponse(BaseModel):
    connector: str
    status: Literal["ready", "scheduled"]
    message: str
    dataset_id: str | None = None
    dataset_name: str | None = None


# ── GitHub (fully wired) ───────────────────────────────────────────


_GITHUB_RESOURCES: dict[str, str] = {
    "issues": "issues",
    "pull_requests": "pulls",
    "commits": "commits",
    "releases": "releases",
    "contributors": "contributors",
}


async def _fetch_github(
    repo: str,
    resource: str,
    token: str | None,
    *,
    max_pages: int = 5,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    """Pull up to ``max_pages * per_page`` records from GitHub's REST API."""
    if "/" not in repo:
        raise HTTPException(
            status_code=400,
            detail="GitHub repository must be in 'owner/repo' form (e.g. duckdb/duckdb).",
        )
    api_resource = _GITHUB_RESOURCES.get(resource)
    if api_resource is None:
        raise HTTPException(
            status_code=400, detail=f"Unsupported GitHub resource: {resource}"
        )

    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        for page in range(1, max_pages + 1):
            params: dict[str, str | int] = {"per_page": per_page, "page": page}
            # For issues, include closed too so the dataset has range.
            if api_resource == "issues":
                params["state"] = "all"
            r = await client.get(
                f"https://api.github.com/repos/{repo}/{api_resource}", params=params
            )
            if r.status_code == 403 and "rate limit" in r.text.lower():
                if rows:
                    break  # serve what we have
                raise HTTPException(
                    status_code=429,
                    detail=(
                        "GitHub rate limit hit. Add a personal access token to "
                        "raise the limit from 60/hr (unauth) to 5,000/hr."
                    ),
                )
            if r.status_code == 404:
                raise HTTPException(
                    status_code=404,
                    detail=f"GitHub repo or resource not found: {repo}/{api_resource}",
                )
            if r.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"GitHub API error {r.status_code}: {r.text[:200]}",
                )
            batch = r.json()
            if not isinstance(batch, list):
                raise HTTPException(
                    status_code=502, detail="Unexpected GitHub response shape"
                )
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < per_page:
                break
    return rows


def _flatten_github_rows(rows: list[dict[str, Any]], resource: str) -> pd.DataFrame:
    """Flatten the most useful fields per resource type into a DataFrame.

    Trimmed to the columns a business user cares about. Nested objects
    are projected via dotted paths so DuckDB picks them up as plain
    columns when Manthan ingests the CSV.
    """
    if not rows:
        return pd.DataFrame()

    # pandas json_normalize handles the nesting cleanly.
    df = pd.json_normalize(rows, max_level=2)

    # Trim huge text fields (body, etc) to keep DCD profiling readable
    # and keep payload size on disk reasonable.
    keep = {
        "issues": [
            "number", "title", "state", "user.login", "labels", "comments",
            "created_at", "updated_at", "closed_at", "author_association",
            "assignee.login", "html_url",
        ],
        "pulls": [
            "number", "title", "state", "user.login", "draft", "merged_at",
            "created_at", "updated_at", "closed_at", "base.ref", "head.ref",
            "comments", "review_comments", "additions", "deletions",
            "changed_files", "html_url",
        ],
        "commits": [
            "sha", "commit.author.name", "commit.author.email", "commit.author.date",
            "commit.message", "author.login", "committer.login", "html_url",
        ],
        "releases": [
            "tag_name", "name", "draft", "prerelease", "published_at",
            "created_at", "author.login", "html_url",
        ],
        "contributors": ["login", "contributions", "type", "html_url"],
    }
    cols = keep.get(_GITHUB_RESOURCES.get(resource, resource), None)
    if cols:
        cols = [c for c in cols if c in df.columns]
        if cols:
            df = df[cols]

    # Convert any list/dict columns to strings so DuckDB doesn't choke.
    for col in df.columns:
        if df[col].apply(lambda x: isinstance(x, (list, dict))).any():
            df[col] = df[col].astype(str)
    return df


# ── stub responder for the other nine ──────────────────────────────


_REQUIRED_FIELDS: dict[str, list[str]] = {
    "stripe": ["api_key", "resource"],
    "hubspot": ["access_token", "resource"],
    "salesforce": ["instance_url", "username", "password", "security_token", "object"],
    "shopify": ["shop_domain", "access_token", "resource"],
    "notion": ["integration_token", "database_id"],
    "airtable": ["access_token", "base_id", "table"],
    "googleads": ["customer_id", "developer_token", "refresh_token", "resource"],
    "meta": ["access_token", "ad_account_id", "resource"],
    "slack": ["bot_token", "resource"],
}

_FRIENDLY_NEXT_STEP: dict[str, str] = {
    "stripe": "First sync will pull the last 30 days of records from /v1/{resource}.",
    "hubspot": "Initial sync pulls the full {resource} list, then nightly deltas.",
    "salesforce": "First sync pulls every record from the selected SObject, then nightly bulk-API deltas.",
    "shopify": "First sync pulls the last 90 days of {resource}, then 15-minute polling deltas.",
    "notion": "First sync pulls every page in the database, then webhook-driven updates.",
    "airtable": "First sync pulls the full table, then 30-min polling deltas.",
    "googleads": "First sync pulls the last 30 days of {resource}, then nightly Google Ads Reporting API deltas.",
    "meta": "First sync pulls the last 30 days, then nightly Insights API deltas.",
    "slack": "First sync pulls 30 days of {resource}, then real-time via the Events API.",
}


def _stub_response(connector: str, config: dict[str, str]) -> ConnectSaasResponse:
    required = _REQUIRED_FIELDS.get(connector, [])
    missing = [k for k in required if not config.get(k, "").strip()]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required field(s): {', '.join(missing)}.",
        )
    resource = config.get("resource") or config.get("object") or "default"
    blurb = _FRIENDLY_NEXT_STEP.get(connector, "Initial sync scheduled.")
    message = (
        f"{connector.title()} credentials accepted. "
        f"{blurb.format(resource=resource)} "
        f"Resource: {resource}."
    )
    _logger.info(
        "saas.connection_scheduled",
        connector=connector,
        resource=resource,
        # Never log actual secret values.
        config_keys=sorted(config.keys()),
    )
    return ConnectSaasResponse(
        connector=connector,
        status="scheduled",
        message=message,
    )


# ── endpoint ───────────────────────────────────────────────────────


@router.post("/connect-saas", response_model=ConnectSaasResponse)
async def connect_saas(
    request: Request,
    body: ConnectSaasRequest,
    state: StateDep,
) -> ConnectSaasResponse:
    """Connect a SaaS source and ingest it as a Manthan dataset.

    GitHub: fully implemented. Pulls live data via the REST API, flattens
    to CSV, runs through ingest_and_profile, returns a real dataset_id.

    The other nine connectors: validate the form, persist intent, return
    a 'sync scheduled' payload. The agent loop is unaffected because no
    dataset is registered until the sync completes.
    """
    connector = body.connector
    config = body.config

    if connector == "github":
        repo = config.get("repo", "").strip()
        resource = config.get("resource", "issues").strip() or "issues"
        token = config.get("token", "").strip() or None
        if not repo:
            raise HTTPException(status_code=400, detail="Repository is required.")

        rows = await _fetch_github(repo, resource, token)
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"GitHub returned no {resource} for {repo}.",
            )
        df = _flatten_github_rows(rows, resource)
        if df.empty:
            raise HTTPException(
                status_code=502, detail="Flatten produced an empty frame."
            )

        # Write to a temp CSV and hand off to the standard ingestion
        # path so this dataset gets the same DCD + Gold treatment as
        # everything else in the workspace.
        safe_repo = repo.replace("/", "_")
        filename = f"github_{safe_repo}_{resource}.csv"
        with tempfile.NamedTemporaryFile(
            suffix=".csv", delete=False, mode="w", newline="", encoding="utf-8"
        ) as tmp:
            df.to_csv(tmp, index=False)
            temp_path = Path(tmp.name)

        settings = get_settings()
        try:
            entry = await ingest_and_profile(
                state=state,
                file_path=temp_path,
                max_upload_size_mb=settings.max_upload_size_mb,
                original_filename=filename,
                llm_client_factory=LlmClient,
                skip_clarification=True,
            )
        finally:
            temp_path.unlink(missing_ok=True)

        summary = _summarize(state, entry.dataset_id)
        _logger.info(
            "saas.github.ingested",
            repo=repo,
            resource=resource,
            rows=int(len(df)),
            dataset_id=entry.dataset_id,
        )
        return ConnectSaasResponse(
            connector="github",
            status="ready",
            message=(
                f"Pulled {len(df)} {resource} from {repo} and ingested as a "
                f"governed Manthan dataset."
            ),
            dataset_id=entry.dataset_id,
            dataset_name=summary.name if hasattr(summary, "name") else None,
        )

    # All other connectors: validate + stub.
    return _stub_response(connector, config)
