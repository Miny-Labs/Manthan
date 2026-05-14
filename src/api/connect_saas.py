"""SaaS connector ingestion — POST /datasets/connect-saas.

Each of the ten supported connectors maps to a focused async fetcher
that hits the provider's REST API with the credentials the UI form
collected, paginates, flattens the JSON into a tabular frame with
``pandas.json_normalize``, trims to the useful business columns,
writes a temp CSV, and hands it to the standard ``ingest_and_profile``
pipeline so the result lands as a normal Gold-state Manthan dataset.

Supported connectors (slug ↔ provider):

    github      Public GitHub REST API
    stripe      Stripe REST API (api.stripe.com/v1)
    hubspot     HubSpot CRM v3 (api.hubapi.com/crm/v3/objects/...)
    salesforce  Salesforce SOAP login → REST API (data/v59.0/query)
    shopify     Shopify Admin REST API (… /admin/api/2024-04/...)
    notion      Notion API (api.notion.com/v1/databases/.../query)
    airtable    Airtable REST API (api.airtable.com/v0/{base}/{table})
    googleads   Google Ads REST API v16 (with OAuth refresh-token flow)
    meta        Meta Graph API v19 (graph.facebook.com/v19.0/...)
    slack       Slack Web API (slack.com/api/...)

Secrets are never logged. The credential payload is destructured on
read and only field names are written to the structured log.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote_plus

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
    status: Literal["ready", "error"]
    message: str
    dataset_id: str | None = None
    dataset_name: str | None = None
    rows: int | None = None


# ── shared helpers ─────────────────────────────────────────────────


_DEFAULT_PAGES = 5
_DEFAULT_PER_PAGE = 100


def _need(config: dict[str, str], key: str, label: str) -> str:
    val = (config.get(key) or "").strip()
    if not val:
        raise HTTPException(status_code=400, detail=f"Missing required field: {label}")
    return val


async def _ingest_dataframe(
    *,
    state: AppState,
    df: pd.DataFrame,
    filename: str,
) -> tuple[str, str | None, int]:
    """Write the frame to a temp CSV and route through ingest_and_profile."""
    if df.empty:
        raise HTTPException(
            status_code=404, detail="Upstream returned no rows for that resource."
        )
    # Stringify nested cells so DuckDB's CSV loader sees flat scalars.
    for col in df.columns:
        if df[col].apply(lambda x: isinstance(x, (list, dict))).any():
            df[col] = df[col].astype(str)
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
    name = getattr(summary, "name", None)
    return entry.dataset_id, name, int(len(df))


def _trim(df: pd.DataFrame, keep: list[str]) -> pd.DataFrame:
    cols = [c for c in keep if c in df.columns]
    return df[cols] if cols else df


# ── GitHub ────────────────────────────────────────────────────────


_GH_PATH = {
    "issues": "issues",
    "pull_requests": "pulls",
    "commits": "commits",
    "releases": "releases",
    "contributors": "contributors",
}


async def _fetch_github(config: dict[str, str]) -> tuple[pd.DataFrame, str, str]:
    repo = _need(config, "repo", "Repository")
    resource = (config.get("resource") or "issues").strip()
    token = (config.get("token") or "").strip() or None
    if "/" not in repo:
        raise HTTPException(status_code=400, detail="repo must be owner/repo.")
    api_resource = _GH_PATH.get(resource)
    if not api_resource:
        raise HTTPException(status_code=400, detail=f"Unknown GitHub resource: {resource}")

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as cli:
        for page in range(1, _DEFAULT_PAGES + 1):
            params: dict[str, Any] = {"per_page": _DEFAULT_PER_PAGE, "page": page}
            if api_resource == "issues":
                params["state"] = "all"
            r = await cli.get(
                f"https://api.github.com/repos/{repo}/{api_resource}", params=params
            )
            if r.status_code == 403 and "rate limit" in r.text.lower():
                if rows:
                    break
                raise HTTPException(status_code=429, detail="GitHub rate limit. Add a PAT.")
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail=f"Not found: {repo}/{api_resource}")
            if r.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"GitHub {r.status_code}: {r.text[:200]}")
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch)
            if len(batch) < _DEFAULT_PER_PAGE:
                break

    df = pd.json_normalize(rows, max_level=2)
    keep_by_resource = {
        "issues": [
            "number", "title", "state", "user.login", "comments",
            "created_at", "updated_at", "closed_at",
            "author_association", "assignee.login", "html_url",
        ],
        "pulls": [
            "number", "title", "state", "user.login", "draft", "merged_at",
            "created_at", "updated_at", "closed_at",
            "base.ref", "head.ref", "comments", "review_comments",
            "additions", "deletions", "changed_files", "html_url",
        ],
        "commits": [
            "sha", "commit.author.name", "commit.author.email",
            "commit.author.date", "commit.message", "author.login",
            "committer.login", "html_url",
        ],
        "releases": [
            "tag_name", "name", "draft", "prerelease",
            "published_at", "created_at", "author.login", "html_url",
        ],
        "contributors": ["login", "contributions", "type", "html_url"],
    }
    df = _trim(df, keep_by_resource.get(api_resource, []))
    safe = repo.replace("/", "_")
    return df, f"github_{safe}_{resource}.csv", f"GitHub {repo} · {resource}"


# ── Stripe ────────────────────────────────────────────────────────


async def _fetch_stripe(config: dict[str, str]) -> tuple[pd.DataFrame, str, str]:
    api_key = _need(config, "api_key", "Stripe secret key")
    resource = (config.get("resource") or "charges").strip()
    rows: list[dict[str, Any]] = []
    starting_after: str | None = None

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as cli:
        for _ in range(_DEFAULT_PAGES):
            params: dict[str, Any] = {"limit": _DEFAULT_PER_PAGE}
            if starting_after:
                params["starting_after"] = starting_after
            r = await cli.get(f"https://api.stripe.com/v1/{resource}", params=params)
            if r.status_code == 401:
                raise HTTPException(status_code=401, detail="Stripe rejected the key.")
            if r.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"Stripe {r.status_code}: {r.text[:200]}")
            payload = r.json()
            data = payload.get("data", [])
            if not data:
                break
            rows.extend(data)
            if not payload.get("has_more"):
                break
            starting_after = data[-1].get("id")

    df = pd.json_normalize(rows, max_level=2)
    keep_by_resource = {
        "charges": [
            "id", "amount", "currency", "captured", "paid", "status",
            "created", "customer", "description", "receipt_email",
            "payment_method_details.type", "outcome.network_status",
            "outcome.risk_level", "billing_details.address.country",
        ],
        "customers": [
            "id", "email", "name", "phone", "currency", "delinquent",
            "balance", "created", "address.country", "address.city",
        ],
        "invoices": [
            "id", "customer", "total", "amount_paid", "amount_remaining",
            "currency", "status", "paid", "attempt_count", "created",
            "due_date", "subscription",
        ],
        "subscriptions": [
            "id", "customer", "status", "current_period_start",
            "current_period_end", "cancel_at_period_end", "canceled_at",
            "start_date", "trial_start", "trial_end",
        ],
        "balance_transactions": [
            "id", "amount", "fee", "net", "currency", "type", "status",
            "created", "available_on", "source",
        ],
    }
    df = _trim(df, keep_by_resource.get(resource, []))
    return df, f"stripe_{resource}.csv", f"Stripe · {resource}"


# ── HubSpot ───────────────────────────────────────────────────────


async def _fetch_hubspot(config: dict[str, str]) -> tuple[pd.DataFrame, str, str]:
    token = _need(config, "access_token", "HubSpot private-app token")
    resource = (config.get("resource") or "contacts").strip()
    rows: list[dict[str, Any]] = []
    after: str | None = None

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"Authorization": f"Bearer {token}"},
    ) as cli:
        for _ in range(_DEFAULT_PAGES):
            params: dict[str, Any] = {"limit": _DEFAULT_PER_PAGE}
            if after:
                params["after"] = after
            r = await cli.get(
                f"https://api.hubapi.com/crm/v3/objects/{resource}", params=params
            )
            if r.status_code == 401:
                raise HTTPException(status_code=401, detail="HubSpot rejected the token.")
            if r.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"HubSpot {r.status_code}: {r.text[:200]}")
            payload = r.json()
            results = payload.get("results", [])
            if not results:
                break
            for rec in results:
                flat = {"id": rec.get("id"), "createdAt": rec.get("createdAt"), "updatedAt": rec.get("updatedAt")}
                flat.update(rec.get("properties", {}))
                rows.append(flat)
            paging = payload.get("paging") or {}
            after = (paging.get("next") or {}).get("after")
            if not after:
                break

    df = pd.json_normalize(rows, max_level=1)
    return df, f"hubspot_{resource}.csv", f"HubSpot · {resource}"


# ── Salesforce ─────────────────────────────────────────────────────


async def _sf_login(
    instance: str, username: str, password: str, token: str
) -> tuple[str, str]:
    """SOAP login dance — returns ``(access_token, instance_url)``.

    The login URL is always ``login.salesforce.com`` (or test for sandbox);
    after a successful login Salesforce gives back the user's actual
    instance URL which is what the REST endpoints sit on.
    """
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:urn="urn:partner.soap.sforce.com">
  <soapenv:Body>
    <urn:login>
      <urn:username>{username}</urn:username>
      <urn:password>{password}{token}</urn:password>
    </urn:login>
  </soapenv:Body>
</soapenv:Envelope>"""
    login_url = "https://login.salesforce.com/services/Soap/u/59.0"
    async with httpx.AsyncClient(timeout=30.0) as cli:
        r = await cli.post(
            login_url,
            content=soap_body,
            headers={
                "Content-Type": "text/xml; charset=UTF-8",
                "SOAPAction": "login",
            },
        )
    if r.status_code >= 400 or "<sessionId>" not in r.text:
        raise HTTPException(
            status_code=401,
            detail=(
                "Salesforce login failed. Check username, password, and "
                "security token (appended to the password)."
            ),
        )
    import re

    sid = re.search(r"<sessionId>([^<]+)</sessionId>", r.text)
    surl = re.search(r"<serverUrl>([^<]+)</serverUrl>", r.text)
    if not sid or not surl:
        raise HTTPException(status_code=502, detail="Unexpected Salesforce login response.")
    inst = surl.group(1).split("/services/")[0]
    return sid.group(1), inst


async def _fetch_salesforce(config: dict[str, str]) -> tuple[pd.DataFrame, str, str]:
    username = _need(config, "username", "Salesforce username")
    password = _need(config, "password", "Salesforce password")
    token = _need(config, "security_token", "Salesforce security token")
    instance = _need(config, "instance_url", "Salesforce instance URL")
    obj = _need(config, "object", "Salesforce SObject")

    sid, inst = await _sf_login(instance, username, password, token)

    # Pick a reasonable set of fields per object.
    field_map = {
        "Account": "Id, Name, Type, Industry, NumberOfEmployees, AnnualRevenue, "
                   "BillingCountry, OwnerId, CreatedDate",
        "Contact": "Id, FirstName, LastName, Email, Phone, Title, "
                   "AccountId, OwnerId, CreatedDate",
        "Opportunity": "Id, Name, StageName, Amount, Probability, CloseDate, "
                       "AccountId, OwnerId, CreatedDate, IsClosed, IsWon",
        "Lead": "Id, FirstName, LastName, Company, Title, Status, "
                "LeadSource, Email, CreatedDate, ConvertedDate",
        "Case": "Id, Subject, Status, Priority, Origin, AccountId, "
                "OwnerId, CreatedDate, ClosedDate",
    }
    fields = field_map.get(obj, "Id, CreatedDate")
    soql = f"SELECT {fields} FROM {obj} ORDER BY CreatedDate DESC LIMIT 500"

    async with httpx.AsyncClient(
        timeout=30.0, headers={"Authorization": f"Bearer {sid}"}
    ) as cli:
        r = await cli.get(
            f"{inst}/services/data/v59.0/query",
            params={"q": soql},
        )
    if r.status_code >= 400:
        raise HTTPException(
            status_code=502, detail=f"Salesforce query failed: {r.text[:200]}"
        )
    records = r.json().get("records", [])
    for rec in records:
        rec.pop("attributes", None)
    df = pd.json_normalize(records, max_level=1)
    return df, f"salesforce_{obj.lower()}.csv", f"Salesforce · {obj}"


# ── Shopify ───────────────────────────────────────────────────────


async def _fetch_shopify(config: dict[str, str]) -> tuple[pd.DataFrame, str, str]:
    shop = _need(config, "shop_domain", "Shopify shop domain").rstrip("/")
    token = _need(config, "access_token", "Shopify admin token")
    resource = (config.get("resource") or "orders").strip()
    # Force the shop to look like *.myshopify.com if the user pasted a friendly URL.
    if not shop.endswith(".myshopify.com"):
        shop = shop.split("//")[-1].split("/")[0]

    rows: list[dict[str, Any]] = []
    page_info: str | None = None
    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"X-Shopify-Access-Token": token, "Accept": "application/json"},
    ) as cli:
        for _ in range(_DEFAULT_PAGES):
            params: dict[str, Any] = {"limit": 50}
            if resource == "orders":
                params["status"] = "any"
            if page_info:
                params = {"page_info": page_info, "limit": 50}
            r = await cli.get(
                f"https://{shop}/admin/api/2024-04/{resource}.json", params=params
            )
            if r.status_code == 401:
                raise HTTPException(status_code=401, detail="Shopify rejected the token.")
            if r.status_code >= 400:
                raise HTTPException(
                    status_code=502, detail=f"Shopify {r.status_code}: {r.text[:200]}"
                )
            payload = r.json()
            batch = payload.get(resource) or payload.get(resource.rstrip("s") + "s") or []
            if not batch:
                break
            rows.extend(batch)
            # Link header pagination
            link = r.headers.get("link", "")
            page_info = None
            for part in link.split(","):
                if 'rel="next"' in part and "page_info=" in part:
                    seg = part.split("page_info=")[1]
                    page_info = seg.split(">")[0].strip(" >\"'&")
                    break
            if not page_info:
                break

    df = pd.json_normalize(rows, max_level=2)
    keep = {
        "orders": [
            "id", "name", "email", "financial_status", "fulfillment_status",
            "total_price", "subtotal_price", "currency", "created_at",
            "updated_at", "customer.id", "shipping_address.country",
        ],
        "customers": [
            "id", "email", "first_name", "last_name", "orders_count",
            "total_spent", "currency", "verified_email", "state",
            "created_at", "updated_at", "default_address.country",
        ],
        "products": [
            "id", "title", "vendor", "product_type", "status", "tags",
            "created_at", "updated_at", "published_at",
        ],
        "inventory_items": [
            "id", "sku", "cost", "tracked", "requires_shipping",
            "country_code_of_origin", "created_at", "updated_at",
        ],
    }
    df = _trim(df, keep.get(resource, []))
    return df, f"shopify_{resource}.csv", f"Shopify · {resource}"


# ── Notion ────────────────────────────────────────────────────────


async def _fetch_notion(config: dict[str, str]) -> tuple[pd.DataFrame, str, str]:
    token = _need(config, "integration_token", "Notion integration token")
    db_id = _need(config, "database_id", "Notion database ID").replace("-", "")

    rows: list[dict[str, Any]] = []
    next_cursor: str | None = None
    async with httpx.AsyncClient(
        timeout=30.0,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
    ) as cli:
        for _ in range(_DEFAULT_PAGES):
            body: dict[str, Any] = {"page_size": _DEFAULT_PER_PAGE}
            if next_cursor:
                body["start_cursor"] = next_cursor
            r = await cli.post(
                f"https://api.notion.com/v1/databases/{db_id}/query", json=body
            )
            if r.status_code == 401:
                raise HTTPException(status_code=401, detail="Notion rejected the token.")
            if r.status_code >= 400:
                raise HTTPException(
                    status_code=502, detail=f"Notion {r.status_code}: {r.text[:200]}"
                )
            payload = r.json()
            for page in payload.get("results", []):
                flat: dict[str, Any] = {
                    "id": page.get("id"),
                    "created_time": page.get("created_time"),
                    "last_edited_time": page.get("last_edited_time"),
                    "url": page.get("url"),
                }
                for prop_name, prop in (page.get("properties") or {}).items():
                    flat[prop_name] = _notion_prop_value(prop)
                rows.append(flat)
            if not payload.get("has_more"):
                break
            next_cursor = payload.get("next_cursor")
            if not next_cursor:
                break

    df = pd.DataFrame(rows)
    return df, "notion_database.csv", f"Notion · database {db_id[:8]}…"


def _notion_prop_value(prop: dict[str, Any]) -> Any:
    """Flatten a single Notion property to a primitive value."""
    t = prop.get("type")
    if t == "title":
        parts = prop.get("title", [])
        return "".join(p.get("plain_text", "") for p in parts) if parts else None
    if t == "rich_text":
        parts = prop.get("rich_text", [])
        return "".join(p.get("plain_text", "") for p in parts) if parts else None
    if t == "number":
        return prop.get("number")
    if t == "select":
        sel = prop.get("select")
        return sel.get("name") if sel else None
    if t == "multi_select":
        return ", ".join(s.get("name", "") for s in (prop.get("multi_select") or []))
    if t == "status":
        st = prop.get("status")
        return st.get("name") if st else None
    if t == "date":
        d = prop.get("date")
        return d.get("start") if d else None
    if t == "people":
        return ", ".join(p.get("name", "") or p.get("id", "") for p in (prop.get("people") or []))
    if t == "checkbox":
        return prop.get("checkbox")
    if t == "url":
        return prop.get("url")
    if t == "email":
        return prop.get("email")
    if t == "phone_number":
        return prop.get("phone_number")
    if t == "created_time":
        return prop.get("created_time")
    if t == "last_edited_time":
        return prop.get("last_edited_time")
    if t == "formula":
        f = prop.get("formula") or {}
        return f.get(f.get("type")) if f else None
    return str(prop.get(t)) if t and prop.get(t) is not None else None


# ── Airtable ──────────────────────────────────────────────────────


async def _fetch_airtable(config: dict[str, str]) -> tuple[pd.DataFrame, str, str]:
    token = _need(config, "access_token", "Airtable personal access token")
    base = _need(config, "base_id", "Airtable base ID")
    table = _need(config, "table", "Airtable table name")
    offset: str | None = None
    rows: list[dict[str, Any]] = []

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"Authorization": f"Bearer {token}"},
    ) as cli:
        for _ in range(_DEFAULT_PAGES):
            params: dict[str, Any] = {"pageSize": _DEFAULT_PER_PAGE}
            if offset:
                params["offset"] = offset
            r = await cli.get(
                f"https://api.airtable.com/v0/{base}/{quote_plus(table)}", params=params
            )
            if r.status_code == 401 or r.status_code == 403:
                raise HTTPException(status_code=401, detail="Airtable rejected the token.")
            if r.status_code >= 400:
                raise HTTPException(
                    status_code=502, detail=f"Airtable {r.status_code}: {r.text[:200]}"
                )
            payload = r.json()
            for rec in payload.get("records", []):
                flat = {"id": rec.get("id"), "createdTime": rec.get("createdTime")}
                flat.update(rec.get("fields") or {})
                rows.append(flat)
            offset = payload.get("offset")
            if not offset:
                break

    df = pd.DataFrame(rows)
    return df, f"airtable_{table}.csv", f"Airtable · {table}"


# ── Google Ads ────────────────────────────────────────────────────


async def _fetch_googleads(config: dict[str, str]) -> tuple[pd.DataFrame, str, str]:
    customer_id = _need(config, "customer_id", "Google Ads customer ID").replace("-", "")
    dev_token = _need(config, "developer_token", "developer token")
    refresh_token = _need(config, "refresh_token", "OAuth refresh token")
    resource = (config.get("resource") or "campaign_performance").strip()
    client_id = config.get("client_id", "").strip()
    client_secret = config.get("client_secret", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=400,
            detail=(
                "Google Ads connector needs the OAuth client_id and "
                "client_secret in addition to the refresh token. Add them "
                "to the connector form before retrying."
            ),
        )

    # Exchange refresh token for an access token.
    async with httpx.AsyncClient(timeout=30.0) as cli:
        tok_r = await cli.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if tok_r.status_code >= 400:
            raise HTTPException(
                status_code=401,
                detail=f"Google OAuth token exchange failed: {tok_r.text[:200]}",
            )
        access_token = tok_r.json().get("access_token")

        gaql = {
            "campaign_performance": (
                "SELECT campaign.id, campaign.name, campaign.status, "
                "metrics.impressions, metrics.clicks, metrics.cost_micros, "
                "metrics.conversions FROM campaign "
                "WHERE segments.date DURING LAST_30_DAYS"
            ),
            "ad_group_performance": (
                "SELECT ad_group.id, ad_group.name, ad_group.status, "
                "campaign.name, metrics.impressions, metrics.clicks, "
                "metrics.cost_micros FROM ad_group "
                "WHERE segments.date DURING LAST_30_DAYS"
            ),
            "keyword_view": (
                "SELECT ad_group_criterion.criterion_id, "
                "ad_group_criterion.keyword.text, "
                "ad_group_criterion.keyword.match_type, "
                "metrics.impressions, metrics.clicks, metrics.cost_micros "
                "FROM keyword_view WHERE segments.date DURING LAST_30_DAYS"
            ),
            "search_terms": (
                "SELECT search_term_view.search_term, "
                "metrics.impressions, metrics.clicks, metrics.cost_micros, "
                "metrics.conversions FROM search_term_view "
                "WHERE segments.date DURING LAST_30_DAYS"
            ),
        }.get(resource)
        if not gaql:
            raise HTTPException(status_code=400, detail=f"Unknown Google Ads report: {resource}")

        r = await cli.post(
            f"https://googleads.googleapis.com/v16/customers/{customer_id}/googleAds:searchStream",
            headers={
                "Authorization": f"Bearer {access_token}",
                "developer-token": dev_token,
                "Content-Type": "application/json",
            },
            json={"query": gaql},
        )
    if r.status_code >= 400:
        raise HTTPException(
            status_code=502, detail=f"Google Ads {r.status_code}: {r.text[:300]}"
        )
    payload = r.json()
    rows: list[dict[str, Any]] = []
    for batch in payload if isinstance(payload, list) else [payload]:
        for rec in batch.get("results", []):
            rows.append(rec)
    df = pd.json_normalize(rows, max_level=3)
    return df, f"googleads_{resource}.csv", f"Google Ads · {resource}"


# ── Meta Ads ──────────────────────────────────────────────────────


async def _fetch_meta(config: dict[str, str]) -> tuple[pd.DataFrame, str, str]:
    token = _need(config, "access_token", "Meta access token")
    account = _need(config, "ad_account_id", "Meta ad account ID")
    resource = (config.get("resource") or "campaigns").strip()
    if not account.startswith("act_"):
        account = f"act_{account}"

    fields = {
        "campaigns": "id,name,status,objective,start_time,stop_time,daily_budget,lifetime_budget",
        "adsets": "id,name,campaign_id,status,daily_budget,lifetime_budget,targeting,start_time,stop_time",
        "ads": "id,name,adset_id,campaign_id,status,created_time,updated_time",
        "insights": "campaign_name,impressions,clicks,spend,reach,frequency,cpm,cpc,ctr,date_start,date_stop",
    }.get(resource, "id,name")
    edge = "insights" if resource == "insights" else resource

    rows: list[dict[str, Any]] = []
    next_url: str | None = (
        f"https://graph.facebook.com/v19.0/{account}/{edge}"
        f"?fields={fields}&access_token={token}&limit={_DEFAULT_PER_PAGE}"
    )
    async with httpx.AsyncClient(timeout=30.0) as cli:
        for _ in range(_DEFAULT_PAGES):
            if not next_url:
                break
            r = await cli.get(next_url)
            if r.status_code == 401:
                raise HTTPException(status_code=401, detail="Meta rejected the token.")
            if r.status_code >= 400:
                raise HTTPException(
                    status_code=502, detail=f"Meta {r.status_code}: {r.text[:200]}"
                )
            payload = r.json()
            data = payload.get("data") or []
            rows.extend(data)
            next_url = (payload.get("paging") or {}).get("next")

    df = pd.json_normalize(rows, max_level=2)
    return df, f"meta_{resource}.csv", f"Meta Ads · {resource}"


# ── Slack ─────────────────────────────────────────────────────────


async def _fetch_slack(config: dict[str, str]) -> tuple[pd.DataFrame, str, str]:
    token = _need(config, "bot_token", "Slack bot token")
    resource = (config.get("resource") or "channels").strip()
    channel = (config.get("channel_id") or "").strip() or None

    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    endpoint_map = {
        "messages": "conversations.history",
        "channels": "conversations.list",
        "members": "users.list",
        "reactions": "reactions.list",
    }
    endpoint = endpoint_map.get(resource)
    if not endpoint:
        raise HTTPException(status_code=400, detail=f"Unknown Slack resource: {resource}")

    async with httpx.AsyncClient(
        timeout=30.0, headers={"Authorization": f"Bearer {token}"}
    ) as cli:
        for _ in range(_DEFAULT_PAGES):
            params: dict[str, Any] = {"limit": _DEFAULT_PER_PAGE}
            if cursor:
                params["cursor"] = cursor
            if resource == "messages":
                if not channel:
                    raise HTTPException(
                        status_code=400, detail="Channel ID is required for messages."
                    )
                params["channel"] = channel
            r = await cli.get(f"https://slack.com/api/{endpoint}", params=params)
            if r.status_code >= 400:
                raise HTTPException(
                    status_code=502, detail=f"Slack {r.status_code}: {r.text[:200]}"
                )
            payload = r.json()
            if not payload.get("ok"):
                raise HTTPException(
                    status_code=502, detail=f"Slack error: {payload.get('error')}"
                )
            keys = ["messages", "channels", "members", "items"]
            data = next((payload.get(k) for k in keys if k in payload), [])
            rows.extend(data or [])
            cursor = (payload.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break

    df = pd.json_normalize(rows, max_level=2)
    keep = {
        "messages": ["ts", "user", "text", "type", "subtype", "thread_ts", "reply_count", "reactions"],
        "channels": ["id", "name", "is_private", "is_archived", "num_members", "created", "topic.value", "purpose.value"],
        "members": ["id", "name", "real_name", "is_bot", "is_admin", "is_owner", "tz", "deleted", "profile.email", "profile.title"],
        "reactions": ["type", "channel", "message.ts", "message.user", "message.text"],
    }
    df = _trim(df, keep.get(resource, []))
    return df, f"slack_{resource}.csv", f"Slack · {resource}"


# ── endpoint ──────────────────────────────────────────────────────


_FETCHERS = {
    "github": _fetch_github,
    "stripe": _fetch_stripe,
    "hubspot": _fetch_hubspot,
    "salesforce": _fetch_salesforce,
    "shopify": _fetch_shopify,
    "notion": _fetch_notion,
    "airtable": _fetch_airtable,
    "googleads": _fetch_googleads,
    "meta": _fetch_meta,
    "slack": _fetch_slack,
}


@router.post("/connect-saas", response_model=ConnectSaasResponse)
async def connect_saas(
    request: Request,
    body: ConnectSaasRequest,
    state: StateDep,
) -> ConnectSaasResponse:
    """Ingest a SaaS source as a Manthan dataset.

    Routes to a connector-specific fetcher that returns a flat
    DataFrame plus a filename. The frame goes through the standard
    ingestion pipeline so the resulting dataset shows up in Manthan
    with a DCD, rollups, click-to-audit metrics — the works.
    """
    fetcher = _FETCHERS.get(body.connector)
    if fetcher is None:
        raise HTTPException(status_code=400, detail=f"Unknown connector: {body.connector}")

    _logger.info(
        "saas.connect.start",
        connector=body.connector,
        config_keys=sorted(body.config.keys()),
    )
    df, filename, label = await fetcher(body.config)
    dataset_id, dataset_name, row_count = await _ingest_dataframe(
        state=state, df=df, filename=filename
    )
    _logger.info(
        "saas.connect.ingested",
        connector=body.connector,
        dataset_id=dataset_id,
        rows=row_count,
    )
    return ConnectSaasResponse(
        connector=body.connector,
        status="ready",
        message=f"Pulled {row_count:,} rows from {label} and ingested as a governed Manthan dataset.",
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        rows=row_count,
    )
