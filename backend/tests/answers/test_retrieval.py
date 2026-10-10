"""Unit tests for ``answers.retrieval`` (R6.5, R15.1).

Three documented cases:

1. Empty graph match → ``retrieval_mode == "vector_only"`` and ``chunk_ids``
   come solely from the vector search (design.md §Failure-Mode Matrix → KG
   matched zero concepts).
2. Both vector and graph return chunks → ``retrieval_mode == "hybrid"`` and
   chunks are deduplicated by ID (vector ordering wins on collision).
3. ``cited_textbook_pages`` is sorted, deduplicated, and shifted from
   0-indexed storage values to 1-indexed display values.

All external dependencies (``embedder.search_similar``,
``get_extended_concepts_for_question``, ``get_related_chunk_ids``,
``database.fetch_all``) are monkey-patched so no Chroma / SQLite I/O
happens during the test run.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from answers import retrieval as retrieval_module
from answers.schema import CoverageMode, RetrievedContext


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _make_async_return(value: Any):
    """Return an ``async`` function that returns ``value``, ignoring args."""

    async def _stub(*args, **kwargs):
        return value

    return _stub


def _vector_chunk(
    chunk_id: str,
    text: str,
    page_numbers: list[int] | str,
    *,
    chapter_id: str = "ch1",
    textbook_id: str = "tb1",
    similarity: float = 0.9,
) -> dict:
    """Build a result dict in the shape ``embedder.search_similar`` returns."""
    return {
        "id": chunk_id,
        "text": text,
        "distance": 1.0 - similarity,
        "similarity": similarity,
        "metadata": {
            "chapter_id": chapter_id,
            "textbook_id": textbook_id,
            "page_numbers": (
                page_numbers
                if isinstance(page_numbers, str)
                else json.dumps(page_numbers)
            ),
            "section_heading": "Section",
            "chunk_index": 0,
            "source": "vector",
        },
    }


def _db_chunk_row(
    chunk_id: str,
    text: str,
    page_numbers: list[int],
    *,
    chapter_id: str = "ch1",
    textbook_id: str = "tb1",
) -> dict:
    """Build a row in the shape ``database.fetch_all('chunks', ...)`` returns."""
    return {
        "id": chunk_id,
        "text": text,
        "chapter_id": chapter_id,
        "textbook_id": textbook_id,
        "page_numbers": json.dumps(page_numbers),
        "section_heading": "Section",
        "chunk_index": 0,
    }


def _coverage_mode(depth_target: str = "saq") -> CoverageMode:
    return CoverageMode(
        mode="saq_compressed",
        sections_to_populate=["definition"],
        per_section_caps={"definition": 1},
        depth_target=depth_target,
    )


# ──────────────────────────────────────────────────────────────────────────
# (1) Empty graph match → vector-only
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retrieve_empty_graph_match_is_vector_only(monkeypatch):
    """Graph returns zero concepts → ``retrieval_mode == "vector_only"``."""
    vector_results = [
        _vector_chunk("v1", "vector text 1", [4, 9]),
        _vector_chunk("v2", "vector text 2", [9, 12]),
    ]

    def _fake_search_similar(subject, query, n_results, filter_chapter_id=None):
        return list(vector_results)

    monkeypatch.setattr(retrieval_module.embedder, "search_similar", _fake_search_similar)

    # Graph returns zero concepts → no chunks should be fetched.
    monkeypatch.setattr(
        retrieval_module,
        "get_extended_concepts_for_question",
        _make_async_return(
            {
                "concepts": [],
                "concept_ids": [],
                "concept_names": [],
                "relation_context": [],
                "direct_count": 0,
                "neighbor_count": 0,
            }
        ),
    )

    async def _tripwire_get_related(*args, **kwargs):  # pragma: no cover
        raise AssertionError("get_related_chunk_ids must not be called when concept_ids is empty")

    monkeypatch.setattr(retrieval_module, "get_related_chunk_ids", _tripwire_get_related)

    async def _tripwire_fetch_all(*args, **kwargs):  # pragma: no cover
        raise AssertionError("database.fetch_all must not be called when graph yields nothing")

    monkeypatch.setattr(retrieval_module.database, "fetch_all", _tripwire_fetch_all)

    result = await retrieval_module.retrieve(
        question_text="Define sepsis.",
        chapter_id="ch1",
        subject="general_surgery",
        mode=_coverage_mode("saq"),
    )

    assert isinstance(result, RetrievedContext)
    assert result.retrieval_mode == "vector_only"
    assert result.chunk_ids == ["v1", "v2"]
    assert result.chunk_texts == ["vector text 1", "vector text 2"]
    # 0-indexed pages [4, 9, 12] → 1-indexed display [5, 10, 13], sorted+deduped.
    assert result.cited_textbook_pages == [5, 10, 13]


# ──────────────────────────────────────────────────────────────────────────
# (2) Both layers return chunks → hybrid + dedup
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retrieve_hybrid_mode_dedupes_chunks(monkeypatch):
    """Both layers return chunks → ``retrieval_mode == "hybrid"`` and dedup by ID."""
    vector_results = [
        _vector_chunk("shared", "shared via vector", [0, 1], similarity=0.95),
        _vector_chunk("v_only", "vector exclusive", [2]),
    ]

    def _fake_search_similar(subject, query, n_results, filter_chapter_id=None):
        return list(vector_results)

    monkeypatch.setattr(retrieval_module.embedder, "search_similar", _fake_search_similar)

    monkeypatch.setattr(
        retrieval_module,
        "get_extended_concepts_for_question",
        _make_async_return(
            {
                "concepts": [{"id": "c_x", "name": "X", "concept_type": "disease"}],
                "concept_ids": ["c_x"],
                "concept_names": ["X"],
                "relation_context": [],
                "direct_count": 1,
                "neighbor_count": 0,
            }
        ),
    )
    # Graph reports two chunk IDs: one duplicates the vector hit (must be deduped),
    # the other is graph-exclusive.
    monkeypatch.setattr(
        retrieval_module,
        "get_related_chunk_ids",
        _make_async_return(["shared", "g_only"]),
    )

    async def _fake_fetch_all(table, where, params, *_args, **_kwargs):
        assert table == "chunks"
        assert where == "id = ?"
        chunk_id = params[0]
        if chunk_id == "shared":
            return [_db_chunk_row("shared", "shared via graph", [0, 1])]
        if chunk_id == "g_only":
            return [_db_chunk_row("g_only", "graph exclusive", [3, 4])]
        return []

    monkeypatch.setattr(retrieval_module.database, "fetch_all", _fake_fetch_all)

    result = await retrieval_module.retrieve(
        question_text="Discuss X.",
        chapter_id="ch1",
        subject="general_surgery",
        mode=_coverage_mode("laq"),
    )

    assert result.retrieval_mode == "hybrid"
    # Vector ordering preserved; the duplicate ``shared`` from graph drops out;
    # graph-exclusive ``g_only`` appended at the end.
    assert result.chunk_ids == ["shared", "v_only", "g_only"]
    # Vector text wins on the shared ID (vector chunks are merged first).
    assert result.chunk_texts == [
        "shared via vector",
        "vector exclusive",
        "graph exclusive",
    ]


# ──────────────────────────────────────────────────────────────────────────
# (3) cited_textbook_pages is sorted, deduplicated, 1-indexed
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retrieve_cited_textbook_pages_sorted_deduped_one_indexed(monkeypatch):
    """``cited_textbook_pages`` is sorted ascending, deduped, and 1-indexed."""
    # Deliberately scrambled, overlapping page numbers across vector chunks so
    # the dedup + sort logic has real work to do.
    vector_results = [
        _vector_chunk("v1", "t1", [9, 4]),       # 0-indexed
        _vector_chunk("v2", "t2", [4, 0, 12]),   # overlaps 4 with v1
        _vector_chunk("v3", "t3", [7]),
    ]

    def _fake_search_similar(subject, query, n_results, filter_chapter_id=None):
        return list(vector_results)

    monkeypatch.setattr(retrieval_module.embedder, "search_similar", _fake_search_similar)
    monkeypatch.setattr(
        retrieval_module,
        "get_extended_concepts_for_question",
        _make_async_return(
            {
                "concepts": [],
                "concept_ids": [],
                "concept_names": [],
                "relation_context": [],
                "direct_count": 0,
                "neighbor_count": 0,
            }
        ),
    )

    async def _no_graph_chunks(*args, **kwargs):  # pragma: no cover
        raise AssertionError("get_related_chunk_ids must not be called")

    monkeypatch.setattr(retrieval_module, "get_related_chunk_ids", _no_graph_chunks)

    result = await retrieval_module.retrieve(
        question_text="Anything.",
        chapter_id="ch1",
        subject="general_surgery",
        mode=_coverage_mode("vsaq"),
    )

    # Storage pages {0, 4, 7, 9, 12} → display {1, 5, 8, 10, 13}, sorted ascending.
    assert result.cited_textbook_pages == [1, 5, 8, 10, 13]
    # Sanity: result is sorted (no duplicates, strictly ascending).
    assert result.cited_textbook_pages == sorted(set(result.cited_textbook_pages))
