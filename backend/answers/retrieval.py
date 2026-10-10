"""Vector + graph retrieval helpers feeding the draft prompt (R6.5, R15).

This module is a focused port of the legacy ``_retrieve_vector_context`` and
``_retrieve_graph_context`` helpers in ``backend/answers/generator.py``. The
new public entry point ``retrieve()`` returns a typed
:class:`answers.schema.RetrievedContext` that the orchestrator can hand to the
draft prompt builder.

Design references
-----------------
- design.md §Module layout → ``retrieval.py``
- design.md §Requirement → Module mapping (R6 — KG-aware retrieval, R15 —
  bullet traceability requires chunk IDs + textbook page numbers).
- design.md §Failure-Mode Matrix → "KG matched zero concepts" (fall back to
  vector-only retrieval, ``provenance.retrieval_mode = "vector_only"``).
"""
from __future__ import annotations

import json
import logging

from answers.schema import CoverageMode, RetrievedContext
from core import embedder
from knowledge.graph_builder import (
    get_extended_concepts_for_question,
    get_related_chunk_ids,
)
from state import db as database

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Depth → chunk-count mapping (R6.5, design §Module mapping)
# ──────────────────────────────────────────────────────────────────────────

_VECTOR_CHUNKS_BY_DEPTH: dict[str, int] = {
    "vsaq": 3,
    "saq": 5,
    "laq": 8,
}

# Default count for graph-sourced chunks (matches legacy behaviour).
_GRAPH_CHUNKS_DEFAULT: int = 8


# ──────────────────────────────────────────────────────────────────────────
# Layer 1 — Vector similarity search (ChromaDB)
# ──────────────────────────────────────────────────────────────────────────


async def _retrieve_vector(
    question_text: str,
    chapter_id: str,
    subject: str,
    n_chunks: int = 5,
) -> list[dict]:
    """Vector similarity search via ChromaDB.

    Mirrors ``generator._retrieve_vector_context``: search the matched
    chapter first, then fall back to a subject-wide search if the chapter
    filter returns nothing.
    """
    results = embedder.search_similar(
        subject,
        question_text,
        n_results=n_chunks,
        filter_chapter_id=chapter_id,
    )
    if not results:
        # Fallback: drop the chapter filter so a poor chapter match still
        # returns something useful.
        results = embedder.search_similar(
            subject, question_text, n_results=n_chunks
        )
    return results


# ──────────────────────────────────────────────────────────────────────────
# Layer 2 — Knowledge-graph-guided retrieval
# ──────────────────────────────────────────────────────────────────────────


async def _retrieve_graph(
    question_text: str,
    subject: str,
    n_chunks: int = _GRAPH_CHUNKS_DEFAULT,
) -> list[dict]:
    """Knowledge-graph-guided retrieval.

    Resolves the question to a set of concept IDs via
    :func:`get_extended_concepts_for_question`, then materializes the
    associated chunks from ``concept_sources`` via
    :func:`get_related_chunk_ids` plus a per-chunk DB fetch. Output rows are
    shaped to match the ChromaDB result dict so callers can mix the two
    sources transparently.
    """
    chunks: list[dict] = []
    try:
        extended = await get_extended_concepts_for_question(
            question_text, subject, limit=10
        )
    except Exception as exc:  # graph DB offline / unexpected failure
        logger.warning("retrieval._retrieve_graph: KG lookup failed: %s", exc)
        return chunks

    concept_ids = extended.get("concept_ids") or []
    if not concept_ids:
        return chunks

    try:
        graph_chunk_ids = await get_related_chunk_ids(concept_ids, subject)
    except Exception as exc:
        logger.warning("retrieval._retrieve_graph: concept_sources fetch failed: %s", exc)
        return chunks

    seen: set[str] = set()
    # Match the legacy pull window: a few extras over n_chunks so the caller
    # has headroom after dedup against the vector list.
    for chunk_id in graph_chunk_ids[: n_chunks + 6]:
        if chunk_id in seen:
            continue
        try:
            rows = await database.fetch_all("chunks", "id = ?", (chunk_id,))
        except Exception as exc:
            logger.warning("retrieval._retrieve_graph: chunks fetch failed: %s", exc)
            continue
        if not rows:
            continue
        c = rows[0]
        chunks.append(
            {
                "id": c["id"],
                "text": c.get("text", ""),
                "distance": 0.3,
                "similarity": 0.7,
                "metadata": {
                    "chapter_id": c.get("chapter_id", ""),
                    "textbook_id": c.get("textbook_id", ""),
                    "page_numbers": c.get("page_numbers", "[]"),
                    "section_heading": c.get("section_heading", ""),
                    "chunk_index": c.get("chunk_index", 0),
                    "source": "graph",
                },
            }
        )
        seen.add(chunk_id)

    return chunks


# ──────────────────────────────────────────────────────────────────────────
# Page-number extraction (R15 traceability)
# ──────────────────────────────────────────────────────────────────────────


def _extract_cited_pages(chunks: list[dict]) -> list[int]:
    """Flatten, dedupe, sort, and convert page numbers to 1-indexed display.

    The persisted ``chunks.page_numbers`` value is a JSON-encoded list of
    0-indexed integers (see ``state.db`` schema + ingestion pipeline). This
    helper accepts either a JSON string or an already-decoded list and
    returns a sorted, deduplicated list of 1-indexed integers suitable for
    display in :class:`Provenance.cited_textbook_pages`.
    """
    pages: set[int] = set()
    for c in chunks:
        meta = c.get("metadata") or {}
        raw = meta.get("page_numbers", "[]")
        decoded: object = raw
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw.replace("'", '"'))
            except (json.JSONDecodeError, TypeError):
                decoded = []
        if not isinstance(decoded, list):
            continue
        for p in decoded:
            try:
                pages.add(int(p))
            except (TypeError, ValueError):
                continue

    # 0-indexed in storage → 1-indexed for display.
    return sorted(p + 1 for p in pages)


# ──────────────────────────────────────────────────────────────────────────
# Top-level retrieval entry point
# ──────────────────────────────────────────────────────────────────────────


async def retrieve(
    question_text: str,
    chapter_id: str,
    subject: str,
    mode: CoverageMode,
) -> RetrievedContext:
    """Top-level retrieval entry point used by the orchestrator.

    Selects vector chunk count from ``mode.depth_target``
    (``vsaq → 3``, ``saq → 5``, ``laq → 8``). Combines vector and graph
    chunks (deduplicated by chunk ID) and sets ``retrieval_mode`` to:

    * ``"vector_only"`` if the graph layer returned nothing;
    * ``"hybrid"`` otherwise.

    ``cited_textbook_pages`` is computed from the retained chunks'
    ``metadata.page_numbers`` and is sorted, deduplicated, and shifted from
    0-indexed storage to 1-indexed display.
    """
    n_vector = _VECTOR_CHUNKS_BY_DEPTH.get(mode.depth_target, 5)

    vector_chunks = await _retrieve_vector(
        question_text, chapter_id, subject, n_chunks=n_vector
    )
    graph_chunks = await _retrieve_graph(
        question_text, subject, n_chunks=_GRAPH_CHUNKS_DEFAULT
    )

    # Dedupe by chunk ID, preferring the vector ordering (vector chunks
    # come first because they are filtered to the matched chapter and
    # carry a real similarity score).
    seen: set[str] = set()
    merged: list[dict] = []
    for c in vector_chunks:
        cid = c.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        merged.append(c)
    for c in graph_chunks:
        cid = c.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        merged.append(c)

    retrieval_mode = "hybrid" if graph_chunks else "vector_only"

    return RetrievedContext(
        chunk_ids=[c["id"] for c in merged],
        chunk_texts=[c.get("text", "") for c in merged],
        cited_textbook_pages=_extract_cited_pages(merged),
        retrieval_mode=retrieval_mode,
    )
