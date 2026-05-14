"""Memory store HTTP router.

Dual-backed memory layer:

- **SQLite** (``src.core.memory.MemoryStore``) keeps structured
  ``(scope_type, scope_id, key)`` entries for deterministic recall —
  identity preferences, dataset caveats, definition cards.
- **Vultr Vector Store** (``src.core.vector_memory.VultrVectorMemory``)
  embeds every saved memory and lets the agent retrieve them by
  semantic similarity. "*What did we say about revenue last quarter?*"
  surfaces relevant entries even when the literal keys don't match.

Writes go to BOTH stores. Searches query the Vector Store first
(semantic), then fall back to SQLite keyword search and merge results.
Vector failures never break the API — they're logged and the SQLite
path takes over so the agent stays responsive even if the Vector Store
is unreachable.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.core.logger import get_logger
from src.core.memory import MemoryEntry, MemoryError
from src.core.state import AppState, get_state
from src.core.vector_memory import (
    VectorMemoryError,
    VultrVectorMemory,
    make_collection_name,
)

router = APIRouter(prefix="/memory", tags=["memory"])

StateDep = Annotated[AppState, Depends(get_state)]

_logger = get_logger()


class MemoryPutRequest(BaseModel):
    """Body of ``POST /memory``."""

    scope_type: str = Field(..., description="dataset | user | global | session")
    scope_id: str
    key: str
    value: Any
    category: str = Field(
        default="note",
        description="preference | definition | caveat | fact | note",
    )
    description: str | None = None


class MemoryEntryResponse(BaseModel):
    """Serialized form of a :class:`MemoryEntry`."""

    scope_type: str
    scope_id: str
    key: str
    value: Any
    category: str
    description: str | None
    created_at: str
    updated_at: str
    # Vector Store linkage — populated when the entry is also stored
    # in Vultr's managed Vector Store for semantic recall.
    vector_item_id: str | None = None

    @classmethod
    def from_entry(
        cls, entry: MemoryEntry, *, vector_item_id: str | None = None
    ) -> MemoryEntryResponse:
        return cls(
            scope_type=entry.scope_type,
            scope_id=entry.scope_id,
            key=entry.key,
            value=entry.value,
            category=entry.category,
            description=entry.description,
            created_at=entry.created_at.isoformat(),
            updated_at=entry.updated_at.isoformat(),
            vector_item_id=vector_item_id,
        )


async def _vector_write(
    scope_type: str,
    scope_id: str,
    key: str,
    value: Any,
    description: str | None,
) -> str | None:
    """Mirror a memory write into the Vultr Vector Store. Best-effort.

    Returns the new item id on success, or ``None`` on any failure
    (so the SQLite write still wins and the request still succeeds).
    """
    try:
        async with VultrVectorMemory() as vector_store:
            content_text = (
                f"{key}: {value}" if not isinstance(value, str) else f"{key}: {value}"
            )
            return await vector_store.add_memory(
                make_collection_name(scope_type, scope_id),
                content_text,
                description=description or key,
            )
    except (VectorMemoryError, Exception) as exc:  # noqa: BLE001
        _logger.warning(
            "vector_memory.mirror_write_failed",
            scope_type=scope_type,
            scope_id=scope_id,
            key=key,
            error=str(exc)[:200],
        )
        return None


async def _vector_search(
    scope_type: str | None,
    scope_id: str | None,
    query: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Semantic recall via Vultr Vector Store. Returns ``[]`` on any error.

    Requires both ``scope_type`` and ``scope_id`` because collections
    are per-scope. When the caller doesn't provide a scope, we skip the
    vector path entirely and let SQLite keyword search handle the query.
    """
    if not scope_type or not scope_id:
        return []
    try:
        async with VultrVectorMemory() as vector_store:
            return await vector_store.search(
                make_collection_name(scope_type, scope_id),
                query,
                limit=limit,
            )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "vector_memory.semantic_search_failed",
            scope_type=scope_type,
            scope_id=scope_id,
            error=str(exc)[:200],
        )
        return []


@router.post("", response_model=MemoryEntryResponse)
async def put_memory(
    request: MemoryPutRequest, state: StateDep
) -> MemoryEntryResponse:
    try:
        entry = state.memory.put(
            scope_type=request.scope_type,
            scope_id=request.scope_id,
            key=request.key,
            value=request.value,
            category=request.category,
            description=request.description,
        )
    except MemoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Mirror the write into the Vultr Vector Store so the agent can
    # semantically recall this memory later, not just by literal key.
    vector_item_id = await _vector_write(
        request.scope_type,
        request.scope_id,
        request.key,
        request.value,
        request.description,
    )

    return MemoryEntryResponse.from_entry(entry, vector_item_id=vector_item_id)


@router.get("/{scope_type}/{scope_id}/{key}", response_model=MemoryEntryResponse)
def get_memory(
    scope_type: str, scope_id: str, key: str, state: StateDep
) -> MemoryEntryResponse:
    try:
        entry = state.memory.get(scope_type=scope_type, scope_id=scope_id, key=key)
    except MemoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return MemoryEntryResponse.from_entry(entry)


@router.delete("/{scope_type}/{scope_id}/{key}")
def delete_memory(
    scope_type: str, scope_id: str, key: str, state: StateDep
) -> dict[str, bool]:
    try:
        removed = state.memory.delete(scope_type=scope_type, scope_id=scope_id, key=key)
    except MemoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"removed": removed}


@router.get("/{scope_type}/{scope_id}", response_model=list[MemoryEntryResponse])
def list_scope(
    scope_type: str,
    scope_id: str,
    state: StateDep,
    category: str | None = None,
) -> list[MemoryEntryResponse]:
    try:
        entries = state.memory.list_scope(
            scope_type=scope_type, scope_id=scope_id, category=category
        )
    except MemoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [MemoryEntryResponse.from_entry(entry) for entry in entries]


@router.get("/search/", response_model=list[MemoryEntryResponse])
async def search_memory(
    state: StateDep,
    query: str,
    scope_type: str | None = None,
    scope_id: str | None = None,
) -> list[MemoryEntryResponse]:
    """Semantic-first memory search.

    Order of operations:

    1. Query the Vultr Vector Store (semantic match on memory content).
    2. Run SQLite keyword search as the deterministic fallback / merge.
    3. Deduplicate by ``(scope_type, scope_id, key)`` so a single memory
       doesn't surface twice when both stores have it.
    """
    # Semantic recall — only meaningful when both scope dims are known.
    vector_hits = await _vector_search(scope_type, scope_id, query)
    vector_keys: set[str] = set()
    semantic_results: list[MemoryEntryResponse] = []

    if vector_hits and scope_type and scope_id:
        for hit in vector_hits:
            content = hit.get("content", "")
            if not isinstance(content, str) or ":" not in content:
                continue
            key_part = content.split(":", 1)[0].strip()
            if not key_part:
                continue
            try:
                entry = state.memory.get(
                    scope_type=scope_type, scope_id=scope_id, key=key_part
                )
            except MemoryError:
                continue
            if entry is None:
                continue
            vector_keys.add(entry.key)
            semantic_results.append(
                MemoryEntryResponse.from_entry(
                    entry, vector_item_id=hit.get("id")
                )
            )

    # SQLite keyword recall — merges with semantic, dedup by key.
    try:
        entries = state.memory.search(query=query, scope_type=scope_type)
    except MemoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    keyword_results = [
        MemoryEntryResponse.from_entry(entry)
        for entry in entries
        if entry.key not in vector_keys
    ]

    return semantic_results + keyword_results
