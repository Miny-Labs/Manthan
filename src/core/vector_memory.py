"""Vultr Vector Store client for Manthan's semantic memory layer.

The ``save_memory`` / ``recall_memory`` agent tools write through both the
SQLite memory store (structured retrieval by scope + key) **and** the
Vultr Vector Store (semantic recall by content meaning). When the user
later asks *"what did we say about revenue last quarter?"*, semantic
search surfaces relevant memories even when the literal keys don't match.

The store is per-scope: one Vector Store collection per
``(scope_type, scope_id)`` pair (e.g. ``manthan-dataset-ds_ab12cd``).
Collections are auto-created on first write and cached for the process
lifetime so we don't round-trip the list endpoint on every call.

Vultr Serverless Inference Vector Store API reference:
    https://api.vultrinference.com

The endpoints used:

- ``GET /vector_store``                — list collections
- ``POST /vector_store``               — create collection ``{name}``
- ``POST /vector_store/{id}/items``    — add ``{content, description?, auto_chunk?}``
- ``GET /vector_store/{id}/items``     — list items
- ``DELETE /vector_store/{id}/items/{itemid}``  — remove item
- ``POST /vector_store/{id}/search``   — semantic search ``{input}``
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from src.core.config import Settings, get_settings
from src.core.exceptions import ManthanError
from src.core.logger import get_logger

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_SEARCH_LIMIT = 5

_logger = get_logger()


class VectorMemoryError(ManthanError):
    """Raised when the Vultr Vector Store API call fails fatally."""


class VultrVectorMemory:
    """Async client for Vultr Serverless Inference's managed Vector Store.

    Designed to be used as an async context manager so the underlying
    httpx client gets closed cleanly. A single instance maintains a small
    in-process cache mapping collection names to ids so writes and
    searches don't re-discover the collection every call.

    All methods are best-effort: when the Vector Store is unreachable
    or returns an error, search/list ops return ``[]`` and the caller
    can fall through to the SQLite-backed structured store. Writes
    raise :class:`VectorMemoryError` so the agent surface can decide
    whether to retry or accept partial persistence.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._collection_cache: dict[str, str] = {}

    async def __aenter__(self) -> VultrVectorMemory:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._settings.vultr_base_url,
                headers={
                    "Authorization": (
                        f"Bearer {self._settings.vultr_api_key.get_secret_value()}"
                    ),
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Collection management ─────────────────────────────────────────

    async def ensure_collection(self, name: str) -> str:
        """Return the collection id, creating it if it doesn't exist.

        Cached for the lifetime of this client. Safe to call repeatedly.
        """
        if name in self._collection_cache:
            return self._collection_cache[name]
        if self._client is None:
            raise VectorMemoryError(
                "VultrVectorMemory must be used as an async context manager"
            )

        # Probe existing collections first.
        try:
            resp = await self._client.get("/vector_store")
            if resp.status_code == 200:
                collections = _unwrap_list(resp.json())
                for coll in collections:
                    if isinstance(coll, dict) and coll.get("name") == name:
                        coll_id = _extract_id(coll)
                        if coll_id:
                            self._collection_cache[name] = coll_id
                            return coll_id
        except httpx.HTTPError as exc:
            _logger.warning("vector_memory.list_failed", error=str(exc)[:200])

        # Create.
        resp = await self._client.post("/vector_store", json={"name": name})
        if resp.status_code >= 400:
            raise VectorMemoryError(
                f"Failed to create Vultr vector store collection {name!r}: "
                f"HTTP {resp.status_code} {resp.text[:200]}"
            )
        coll = resp.json()
        coll_id = _extract_id(coll)
        if not coll_id:
            raise VectorMemoryError(
                f"Vultr returned no collection id for {name!r}: {coll}"
            )
        self._collection_cache[name] = coll_id
        _logger.info("vector_memory.collection_created", name=name, id=coll_id)
        return coll_id

    # ── Item writes ───────────────────────────────────────────────────

    async def add_memory(
        self,
        collection: str,
        content: str,
        *,
        description: str | None = None,
        auto_chunk: bool = False,
    ) -> str:
        """Append one memory item to a collection. Returns the new item id.

        ``content`` is the searchable text (e.g. *"We compute revenue
        net of tax."*). ``description`` is an optional short label that
        Vultr indexes alongside content. ``auto_chunk`` lets the
        upstream split long content into multiple embedded chunks.
        """
        if self._client is None:
            raise VectorMemoryError(
                "VultrVectorMemory must be used as an async context manager"
            )
        coll_id = await self.ensure_collection(collection)
        payload: dict[str, Any] = {"content": content}
        if description:
            payload["description"] = description
        if auto_chunk:
            payload["auto_chunk"] = True
        resp = await self._client.post(
            f"/vector_store/{coll_id}/items", json=payload
        )
        if resp.status_code >= 400:
            raise VectorMemoryError(
                f"Failed to add item to {collection}: "
                f"HTTP {resp.status_code} {resp.text[:200]}"
            )
        item = resp.json()
        item_id = _extract_id(item)
        if not item_id:
            raise VectorMemoryError(
                f"Vultr returned no item id: {item}"
            )
        return item_id

    # ── Semantic search ───────────────────────────────────────────────

    async def search(
        self,
        collection: str,
        query: str,
        *,
        limit: int = _DEFAULT_SEARCH_LIMIT,
    ) -> list[dict[str, Any]]:
        """Run semantic search; return up to ``limit`` matches.

        Each result is a dict with at least ``content`` and an id;
        Vultr may include a score, created timestamp, or description.
        Returns ``[]`` on any error so callers can fall through to the
        structured store without raising.
        """
        if self._client is None:
            raise VectorMemoryError(
                "VultrVectorMemory must be used as an async context manager"
            )
        try:
            coll_id = await self.ensure_collection(collection)
        except VectorMemoryError as exc:
            _logger.warning(
                "vector_memory.search_no_collection",
                collection=collection,
                error=str(exc)[:200],
            )
            return []

        try:
            resp = await self._client.post(
                f"/vector_store/{coll_id}/search",
                json={"input": query, "limit": limit},
            )
        except httpx.HTTPError as exc:
            _logger.warning(
                "vector_memory.search_transport_error", error=str(exc)[:200]
            )
            return []

        if resp.status_code != 200:
            _logger.warning(
                "vector_memory.search_http_error",
                status=resp.status_code,
                body=resp.text[:200],
            )
            return []

        body = resp.json()
        items = _unwrap_list(body)
        return [it for it in items if isinstance(it, dict)]

    # ── Item maintenance ──────────────────────────────────────────────

    async def list_items(self, collection: str) -> list[dict[str, Any]]:
        """List every item in a collection, or ``[]`` if absent / error."""
        if self._client is None:
            raise VectorMemoryError(
                "VultrVectorMemory must be used as an async context manager"
            )
        coll_id = self._collection_cache.get(collection)
        if coll_id is None:
            try:
                coll_id = await self.ensure_collection(collection)
            except VectorMemoryError:
                return []
        try:
            resp = await self._client.get(f"/vector_store/{coll_id}/items")
        except httpx.HTTPError:
            return []
        if resp.status_code != 200:
            return []
        return [it for it in _unwrap_list(resp.json()) if isinstance(it, dict)]

    async def delete_item(self, collection: str, item_id: str) -> bool:
        """Delete one item. Returns True iff the API confirmed deletion."""
        if self._client is None:
            raise VectorMemoryError(
                "VultrVectorMemory must be used as an async context manager"
            )
        coll_id = self._collection_cache.get(collection)
        if coll_id is None:
            return False
        try:
            resp = await self._client.delete(
                f"/vector_store/{coll_id}/items/{item_id}"
            )
        except httpx.HTTPError:
            return False
        return resp.status_code in (200, 204)


# ── helpers ───────────────────────────────────────────────────────────


def make_collection_name(scope_type: str, scope_id: str) -> str:
    """Canonical Vector Store collection name for a memory scope.

    Example: ``("dataset", "ds_ab12cd")`` → ``"manthan-dataset-ds_ab12cd"``.
    """
    return f"manthan-{scope_type}-{scope_id}"


def _extract_id(obj: Any) -> str | None:
    """Vultr returns id in any of: ``id``, ``vector_store_id``, ``item_id``."""
    if not isinstance(obj, dict):
        return None
    for key in ("id", "vector_store_id", "item_id", "collection_id"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _unwrap_list(body: Any) -> list[Any]:
    """Vultr APIs sometimes wrap arrays under ``data`` / ``items`` / ``results``."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("data", "items", "results"):
            val = body.get(key)
            if isinstance(val, list):
                return val
    return []
