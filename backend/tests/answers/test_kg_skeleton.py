"""Unit tests for ``answers.kg_skeleton.build_kg_skeleton`` (R6.1–R6.6, R7.1–R7.2).

Exercises the four documented response paths:

1. Empty concept match → ``used=False`` with empty fields (R6.5).
2. Non-empty match → ``used=True``, skeleton text contains
   ``"Concepts:"`` and ``"Relations:"`` headings.
3. Relations are sorted by priority — a ``causes`` edge MUST appear before
   an ``associated_with`` edge in the rendered skeleton text (design.md
   §Components and Interfaces — relation priority order).
4. Exception bubbling out of ``get_extended_concepts_for_question`` →
   :class:`KGUnavailable` (design §Failure-Mode Matrix → KG lookup fails).

Tests stub out ``get_extended_concepts_for_question`` and
``database.fetch_all`` so we never touch the real KG DB.
"""
from __future__ import annotations

from typing import Any

import pytest

from answers import kg_skeleton as kg_module
from answers.errors import KGUnavailable
from answers.schema import KGSkeleton


# ──────────────────────────────────────────────────────────────────────────
# Helpers — build async stubs and patch the module-level lookups.
# ──────────────────────────────────────────────────────────────────────────


def _make_async_return(value: Any):
    """Return an ``async`` function that returns ``value``, ignoring args."""

    async def _stub(*args, **kwargs):
        return value

    return _stub


def _make_async_raise(exc: BaseException):
    """Return an ``async`` function that raises ``exc``, ignoring args."""

    async def _stub(*args, **kwargs):
        raise exc

    return _stub


# ──────────────────────────────────────────────────────────────────────────
# (1) Empty concept match
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_kg_skeleton_empty_match_returns_used_false(monkeypatch):
    """Zero-concept extended lookup → ``KGSkeleton(used=False, …)`` with empty fields (R6.5)."""
    monkeypatch.setattr(
        kg_module,
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

    # Even if fetch_all were called it should not be — patch a tripwire that
    # would fail loudly so any accidental DB call surfaces.
    async def _tripwire(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("database.fetch_all must not be called on empty match")

    monkeypatch.setattr(kg_module.database, "fetch_all", _tripwire)

    result = await kg_module.build_kg_skeleton(
        question_text="What is sepsis?", subject="general_surgery"
    )

    assert isinstance(result, KGSkeleton)
    assert result.used is False
    assert result.concept_ids == []
    assert result.relation_ids == []
    assert result.must_know_concept_ids == []
    assert result.skeleton_block_text == ""


# ──────────────────────────────────────────────────────────────────────────
# (2) Non-empty match
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_kg_skeleton_populates_skeleton_block_text(monkeypatch):
    """Two matched concepts + one relation → skeleton block carries both headings."""
    concepts = [
        {
            "id": "c_dka",
            "name": "Diabetic Ketoacidosis",
            "concept_type": "disease",
            "importance": "must_know",
        },
        {
            "id": "c_keton",
            "name": "Ketosis",
            "concept_type": "pathology",
            "importance": "standard",
        },
    ]
    monkeypatch.setattr(
        kg_module,
        "get_extended_concepts_for_question",
        _make_async_return(
            {
                "concepts": concepts,
                "concept_ids": ["c_dka", "c_keton"],
                "concept_names": ["Diabetic Ketoacidosis", "Ketosis"],
                "relation_context": [],
                "direct_count": 1,
                "neighbor_count": 1,
            }
        ),
    )

    relations = [
        {
            "id": "r_1",
            "source_id": "c_dka",
            "target_id": "c_keton",
            "relation_type": "causes",
            "confidence": 0.95,
        }
    ]
    monkeypatch.setattr(
        kg_module.database, "fetch_all", _make_async_return(relations)
    )

    result = await kg_module.build_kg_skeleton(
        question_text="Discuss DKA pathophysiology.", subject="medicine"
    )

    assert result.used is True
    assert result.concept_ids == ["c_dka", "c_keton"]
    assert result.relation_ids == ["r_1"]
    assert result.must_know_concept_ids == ["c_dka"]

    block = result.skeleton_block_text
    assert "Concepts:" in block
    assert "Relations:" in block
    # Each concept's name and concept_type render verbatim.
    assert "Diabetic Ketoacidosis (disease) [must_know]" in block
    assert "Ketosis (pathology) [standard]" in block
    # The single relation renders with the source --[type]--> target syntax.
    assert "Diabetic Ketoacidosis --[causes]--> Ketosis" in block
    # Must-know section appears when at least one matched concept is must_know.
    assert "MUST APPEAR IN ANSWER IF RELEVANT:" in block
    assert "Diabetic Ketoacidosis" in block.split("MUST APPEAR IN ANSWER IF RELEVANT:")[1]


# ──────────────────────────────────────────────────────────────────────────
# (3) Relation priority ordering
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_kg_skeleton_sorts_relations_by_priority(monkeypatch):
    """A ``causes`` relation MUST be rendered before an ``associated_with`` relation."""
    concepts = [
        {"id": "c_a", "name": "Alpha", "concept_type": "disease", "importance": "standard"},
        {"id": "c_b", "name": "Beta", "concept_type": "disease", "importance": "standard"},
        {"id": "c_c", "name": "Gamma", "concept_type": "disease", "importance": "standard"},
    ]
    monkeypatch.setattr(
        kg_module,
        "get_extended_concepts_for_question",
        _make_async_return(
            {
                "concepts": concepts,
                "concept_ids": ["c_a", "c_b", "c_c"],
                "concept_names": ["Alpha", "Beta", "Gamma"],
                "relation_context": [],
                "direct_count": 1,
                "neighbor_count": 2,
            }
        ),
    )

    # Deliberately register the lower-priority relation FIRST so the sort
    # has actual work to do. ``causes`` (priority 0) must beat
    # ``associated_with`` (priority 5) regardless of insertion order.
    relations = [
        {
            "id": "r_assoc",
            "source_id": "c_a",
            "target_id": "c_c",
            "relation_type": "associated_with",
            "confidence": 0.9,
        },
        {
            "id": "r_causes",
            "source_id": "c_a",
            "target_id": "c_b",
            "relation_type": "causes",
            "confidence": 0.8,
        },
    ]
    monkeypatch.setattr(
        kg_module.database, "fetch_all", _make_async_return(relations)
    )

    result = await kg_module.build_kg_skeleton(
        question_text="Discuss Alpha disease.", subject="medicine"
    )

    assert result.relation_ids == ["r_causes", "r_assoc"]

    block = result.skeleton_block_text
    causes_idx = block.find("--[causes]-->")
    assoc_idx = block.find("--[associated_with]-->")
    assert causes_idx != -1 and assoc_idx != -1
    assert causes_idx < assoc_idx, (
        f"`causes` must render before `associated_with`; "
        f"causes_idx={causes_idx}, assoc_idx={assoc_idx}"
    )


# ──────────────────────────────────────────────────────────────────────────
# (4) Exception path
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_kg_skeleton_raises_kg_unavailable_on_lookup_failure(monkeypatch):
    """``get_extended_concepts_for_question`` raising → :class:`KGUnavailable`."""
    monkeypatch.setattr(
        kg_module,
        "get_extended_concepts_for_question",
        _make_async_raise(RuntimeError("graph DB offline")),
    )

    with pytest.raises(KGUnavailable) as excinfo:
        await kg_module.build_kg_skeleton(
            question_text="anything", subject="medicine"
        )

    # ``mapping_id`` is unknown at this layer per the function contract.
    assert excinfo.value.mapping_id == ""
    assert "graph DB offline" in excinfo.value.detail


@pytest.mark.asyncio
async def test_build_kg_skeleton_raises_kg_unavailable_on_relations_fetch_failure(monkeypatch):
    """``concept_relations`` fetch raising → :class:`KGUnavailable`."""
    monkeypatch.setattr(
        kg_module,
        "get_extended_concepts_for_question",
        _make_async_return(
            {
                "concepts": [
                    {
                        "id": "c_a",
                        "name": "Alpha",
                        "concept_type": "disease",
                        "importance": "standard",
                    }
                ],
                "concept_ids": ["c_a"],
                "concept_names": ["Alpha"],
                "relation_context": [],
                "direct_count": 1,
                "neighbor_count": 0,
            }
        ),
    )
    monkeypatch.setattr(
        kg_module.database,
        "fetch_all",
        _make_async_raise(RuntimeError("sqlite locked")),
    )

    with pytest.raises(KGUnavailable):
        await kg_module.build_kg_skeleton(
            question_text="anything", subject="medicine"
        )
