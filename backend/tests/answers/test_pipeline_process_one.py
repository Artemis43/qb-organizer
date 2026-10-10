"""Integration tests for ``answers.pipeline._process_one`` (task 28).

Covers the four documented branches of the per-mapping orchestrator:

1. Happy path → answer is persisted, ``provenance.critic_verdict == "pass"``.
2. Validator fails twice → ``mandatory_data_complete=False`` is persisted.
3. Critic fails first attempt, passes on regen → ``regen_model`` is set and
   ``critic_verdict`` ends as ``"pass"``.
4. KG miss → ``kg_skeleton_used=False`` is persisted.

The Claude transport is stubbed by :class:`StageFake`, a per-system-prompt
dispatching fake that returns canned dicts to ``intent``, ``draft``, and
``critic`` from a single client. ``kg_skeleton.build_kg_skeleton`` and
``retrieval.retrieve`` are monkey-patched at module level so we don't have
to seed Chroma or the concept_relations table.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from answers import (
    coverage as coverage_module,
    intent as intent_module,
    kg_skeleton as kg_skeleton_module,
    pipeline as pipeline_module,
    prompts as prompts_module,
    retrieval as retrieval_module,
)
from answers.errors import KGUnavailable
from answers.schema import KGSkeleton, RetrievedContext
from state import db as database


# ──────────────────────────────────────────────────────────────────────────
# Stage-dispatching Claude fake
# ──────────────────────────────────────────────────────────────────────────


class StageFake:
    """Per-system-prompt Claude fake.

    Each stage of the pipeline (intent / draft / critic) calls Claude with a
    different system prompt; this fake dispatches on ``system`` so a single
    client can serve all stages. ``register(system_prompt, response)`` sets
    one fixed response; ``register_sequence(system_prompt, [r1, r2, ...])``
    cycles through a list (the last response repeats once exhausted) so a
    single critic instance can return ``fail`` then ``pass``.
    """

    def __init__(self) -> None:
        self._by_system: dict[str, Any] = {}
        self._sequences: dict[str, list[Any]] = {}
        self._cursors: dict[str, int] = {}
        self.calls: list[dict] = []
        self.call_count: int = 0

    def register(self, system_prompt: str, response: Any) -> None:
        self._by_system[system_prompt] = response

    def register_sequence(self, system_prompt: str, responses: list[Any]) -> None:
        self._sequences[system_prompt] = list(responses)
        self._cursors[system_prompt] = 0

    async def request(
        self,
        messages: list[dict],
        system: str = "",
        model: str = "",
        max_tokens: int = 4096,
        task_type: str = "general",
        subject: str = "",
        response_schema: Any = None,
        request_id: str = "",
    ) -> Any:
        self.call_count += 1
        self.calls.append(
            {
                "system": system,
                "task_type": task_type,
                "model": model,
            }
        )

        if system in self._sequences:
            seq = self._sequences[system]
            i = self._cursors[system]
            self._cursors[system] = i + 1
            response = seq[min(i, len(seq) - 1)]
        elif system in self._by_system:
            response = self._by_system[system]
        else:
            raise KeyError(
                f"StageFake: no response registered for system prompt "
                f"(prefix={system[:60]!r}, task_type={task_type})"
            )

        # Deep clone via JSON when possible so tests can mutate freely.
        if isinstance(response, (dict, list)):
            try:
                return json.loads(json.dumps(response))
            except (TypeError, ValueError):
                return response
        return response


# ──────────────────────────────────────────────────────────────────────────
# Canned fixtures
# ──────────────────────────────────────────────────────────────────────────


_INTENT_FIXTURE = {
    "intent": "discuss",
    "focus_topic": "diabetic ketoacidosis",
    "requested_item_count": None,
    "intent_confidence_low": False,
}

_VALID_DRAFT_FIXTURE = {
    "definition": (
        "Diabetic ketoacidosis (DKA) is an acute metabolic emergency "
        "characterized by hyperglycemia, ketonemia, and high anion-gap "
        "metabolic acidosis."
    ),
    "etiology": [
        {
            "text": "Insulin omission in known type 1 diabetes mellitus",
            "source_chunk_ids": ["chunk_1"],
            "kg_concept_ids": ["c_dka"],
        }
    ],
    "mnemonics": {},
}

# A draft whose pharm bullet has empty dose/frequency — passes Pydantic
# (strings are non-None) but fails validators.check_pharm.
_BROKEN_PHARM_DRAFT_FIXTURE = {
    "definition": "Test definition for broken pharm.",
    "management": {
        "pharm": [
            {
                "text": "Insulin therapy",
                "drug_class": "Hormone",
                "drug_name": "Insulin",
                "dose": "",
                "frequency": "",
                "source_chunk_ids": ["chunk_1"],
                "kg_concept_ids": [],
            }
        ]
    },
    "mnemonics": {},
}

_CRITIC_PASS_FIXTURE = {
    "verdict": "pass",
    "score": 85,
    "structure_score": 80,
    "depth_score": 85,
    "traceability_score": 80,
    "mandatory_data_score": 100,
    "too_shallow_bullets": [],
    "too_deep_bullets": [],
    "missing_must_know": [],
    "feedback": [],
}

_CRITIC_FAIL_FIXTURE = {
    "verdict": "fail",
    "score": 50,
    "structure_score": 40,
    "depth_score": 50,
    "traceability_score": 60,
    "mandatory_data_score": 60,
    "too_shallow_bullets": ["foo"],
    "too_deep_bullets": [],
    "missing_must_know": [],
    "feedback": ["needs more depth"],
}


def _kg_with_concepts() -> KGSkeleton:
    return KGSkeleton(
        concept_ids=["c_dka", "c_insulin"],
        relation_ids=["r1"],
        must_know_concept_ids=["c_dka"],
        skeleton_block_text="Concepts:\n  - DKA (disease) [must_know]",
        used=True,
    )


def _empty_kg() -> KGSkeleton:
    return KGSkeleton(
        concept_ids=[],
        relation_ids=[],
        must_know_concept_ids=[],
        skeleton_block_text="",
        used=False,
    )


def _retrieved_context_fixture() -> RetrievedContext:
    return RetrievedContext(
        chunk_ids=["chunk_1"],
        chunk_texts=["sample chunk text"],
        cited_textbook_pages=[42],
        retrieval_mode="hybrid",
    )


# ──────────────────────────────────────────────────────────────────────────
# Test fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch) -> str:
    """Create a fresh temp SQLite DB with the full schema applied.

    Patches both ``state.db.DB_PATH`` and ``answers.pipeline.database.DB_PATH``
    so every module that imports ``state.db`` sees the same temp DB.
    """
    db_file = tmp_path / "pipeline_test.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    return str(db_file)


@pytest.fixture(autouse=True)
def _clear_intent_cache():
    """The intent classifier caches per-process; clear before/after each test."""
    intent_module._INTENT_CACHE.clear()
    yield
    intent_module._INTENT_CACHE.clear()


async def _seed_minimal_rows(mapping_id: str = "map_test_1") -> None:
    """Insert one textbook + chapter + mapping referencing them.

    The mapping carries a SAQ ``question_text`` so the orchestrator drives the
    full LAQ_Skeleton coverage path through the discuss branch.
    """
    await database.init_db()

    await database.insert(
        "textbooks",
        {
            "id": "tb_test_1",
            "filename": "test.pdf",
            "name": "Test Textbook",
            "subject": "general_medicine",
            "sha256_hash": f"hash_{mapping_id}",
            "status": "ready",
        },
    )
    await database.insert(
        "chapters",
        {
            "id": "ch_test_1",
            "textbook_id": "tb_test_1",
            "chapter_number": 1,
            "name": "Endocrine Emergencies",
            "start_page": 1,
            "end_page": 30,
            "status": "ready",
        },
    )
    await database.insert(
        "mappings",
        {
            "id": mapping_id,
            "question_id": "q_test_1",
            "question_text": "Discuss the pathophysiology of diabetic ketoacidosis.",
            "question_type": "SAQ",
            "qp_id": "qp_test_1",
            "final_chapter_id": "ch_test_1",
            "final_chapter_name": "Endocrine Emergencies",
        },
    )


def _patch_kg_and_retrieval(
    monkeypatch,
    *,
    kg_skeleton: Optional[KGSkeleton] = None,
    raise_kg_unavailable: bool = False,
    retrieved: Optional[RetrievedContext] = None,
) -> None:
    """Stub the non-Claude side helpers (KG + retrieval) at module level."""
    async def _fake_build_kg(question_text, subject):
        if raise_kg_unavailable:
            raise KGUnavailable(mapping_id="", detail="forced miss")
        return kg_skeleton if kg_skeleton is not None else _kg_with_concepts()

    async def _fake_retrieve(question_text, chapter_id, subject, mode):
        return retrieved if retrieved is not None else _retrieved_context_fixture()

    monkeypatch.setattr(
        kg_skeleton_module, "build_kg_skeleton", _fake_build_kg
    )
    monkeypatch.setattr(
        pipeline_module.kg_skeleton_module, "build_kg_skeleton", _fake_build_kg
    )
    monkeypatch.setattr(retrieval_module, "retrieve", _fake_retrieve)
    monkeypatch.setattr(
        pipeline_module.retrieval_module, "retrieve", _fake_retrieve
    )


def _register_default_stages(
    fake: StageFake,
    *,
    draft_payload: Any = None,
    critic_payload: Any = None,
) -> None:
    """Pre-register sane defaults for intent / draft / critic system prompts."""
    fake.register(prompts_module.INTENT_CLASSIFIER_PROMPT, _INTENT_FIXTURE)
    fake.register(
        prompts_module.DRAFT_GENERATOR_SYSTEM_PROMPT,
        draft_payload if draft_payload is not None else _VALID_DRAFT_FIXTURE,
    )
    fake.register(
        prompts_module.EXAMINER_CRITIC_FULL_SYSTEM_PROMPT,
        critic_payload if critic_payload is not None else _CRITIC_PASS_FIXTURE,
    )
    fake.register(
        prompts_module.EXAMINER_CRITIC_VSAQ_LITE_SYSTEM_PROMPT,
        critic_payload if critic_payload is not None else _CRITIC_PASS_FIXTURE,
    )


def _read_answer_row(db_path: str, mapping_id: str) -> dict:
    """Read the persisted row for ``mapping_id`` directly via sqlite3."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM answers WHERE mapping_id = ?", (mapping_id,)
        )
        row = cursor.fetchone()
        assert row is not None, f"no answer row persisted for mapping_id={mapping_id}"
        return dict(row)


# ──────────────────────────────────────────────────────────────────────────
# Test 1 — happy path
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_process_one_happy_path_persists_pass_verdict(
    temp_db, monkeypatch
):
    """All stages succeed → answer persisted, critic_verdict == 'pass'."""
    await _seed_minimal_rows("map_happy_1")

    fake = StageFake()
    _register_default_stages(fake)
    _patch_kg_and_retrieval(monkeypatch)

    record = await pipeline_module._process_one(
        mapping_id="map_happy_1",
        claude_client=fake,
    )

    # Returned AnswerRecord shape
    assert record.mapping_id == "map_happy_1"
    assert record.provenance.critic_verdict == "pass"
    assert record.provenance.critic_score == 85
    assert record.kg_skeleton_used is True
    assert record.mandatory_data_complete is True

    # Persisted DB row
    row = _read_answer_row(temp_db, "map_happy_1")
    assert row["mapping_id"] == "map_happy_1"
    assert row["chapter_id"] == "ch_test_1"
    assert row["coverage_mode"]  # non-empty
    assert row["question_intent"] == "discuss"
    assert row["kg_skeleton_used"] == 1
    assert row["mandatory_data_complete"] == 1
    assert row["intent_confidence_low"] == 0

    persisted_provenance = json.loads(row["provenance"])
    assert persisted_provenance["critic_verdict"] == "pass"
    assert persisted_provenance["regen_model"] is None
    assert persisted_provenance["draft_model"]  # set
    assert persisted_provenance["retrieval_mode"] == "hybrid"
    assert persisted_provenance["cited_textbook_pages"] == [42]

    # Sections JSON round-trips
    sections = json.loads(row["sections"])
    assert "definition" in sections


# ──────────────────────────────────────────────────────────────────────────
# Test 2 — validator fails twice → mandatory_data_complete=False
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_process_one_validator_fails_twice_persists_incomplete(
    temp_db, monkeypatch
):
    """Pharm bullet missing dose/frequency → validator fails twice → persist
    with ``mandatory_data_complete=False`` (R5.7)."""
    await _seed_minimal_rows("map_validator_fail")

    fake = StageFake()
    _register_default_stages(
        fake,
        draft_payload=_BROKEN_PHARM_DRAFT_FIXTURE,
        # Critic returns pass so we isolate the validator branch.
        critic_payload=_CRITIC_PASS_FIXTURE,
    )
    _patch_kg_and_retrieval(monkeypatch)

    record = await pipeline_module._process_one(
        mapping_id="map_validator_fail",
        claude_client=fake,
    )

    assert record.mandatory_data_complete is False

    row = _read_answer_row(temp_db, "map_validator_fail")
    assert row["mandatory_data_complete"] == 0

    # The pipeline should have invoked draft.generate_draft TWICE (initial +
    # 1 simple validator-retry), so the draft system prompt fired ≥ 2 times.
    draft_calls = [
        c for c in fake.calls
        if c["system"] == prompts_module.DRAFT_GENERATOR_SYSTEM_PROMPT
    ]
    assert len(draft_calls) >= 2


# ──────────────────────────────────────────────────────────────────────────
# Test 3 — critic fails then passes on regen → regen_model set
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_process_one_critic_fail_then_pass_on_regen(
    temp_db, monkeypatch
):
    """Critic fails attempt 1, passes attempt 2 → regen_model is recorded
    and the persisted critic_verdict is ``pass`` (R8.4)."""
    await _seed_minimal_rows("map_critic_regen")

    fake = StageFake()
    fake.register(prompts_module.INTENT_CLASSIFIER_PROMPT, _INTENT_FIXTURE)
    fake.register(
        prompts_module.DRAFT_GENERATOR_SYSTEM_PROMPT, _VALID_DRAFT_FIXTURE
    )
    # Critic returns fail then pass — pipeline should regen Haiku and re-run.
    fake.register_sequence(
        prompts_module.EXAMINER_CRITIC_FULL_SYSTEM_PROMPT,
        [_CRITIC_FAIL_FIXTURE, _CRITIC_PASS_FIXTURE],
    )
    _patch_kg_and_retrieval(monkeypatch)

    record = await pipeline_module._process_one(
        mapping_id="map_critic_regen",
        claude_client=fake,
    )

    assert record.provenance.critic_verdict == "pass"
    assert record.provenance.regen_model is not None
    assert record.provenance.escalation_model is None  # task 33 not yet wired

    row = _read_answer_row(temp_db, "map_critic_regen")
    persisted_provenance = json.loads(row["provenance"])
    assert persisted_provenance["critic_verdict"] == "pass"
    assert persisted_provenance["regen_model"]  # non-empty

    # Critic should have been called twice (initial fail + regen pass).
    critic_calls = [
        c for c in fake.calls
        if c["system"] == prompts_module.EXAMINER_CRITIC_FULL_SYSTEM_PROMPT
    ]
    assert len(critic_calls) == 2


# ──────────────────────────────────────────────────────────────────────────
# Test 4 — KG miss → kg_skeleton_used=False persists
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_process_one_kg_miss_persists_kg_unused(temp_db, monkeypatch):
    """``KGUnavailable`` from the KG builder → ``kg_skeleton_used=False``
    persists and the pipeline still produces an answer (R6.5)."""
    await _seed_minimal_rows("map_kg_miss")

    fake = StageFake()
    _register_default_stages(fake)
    # Force the KG builder to raise so the orchestrator falls back to empty.
    _patch_kg_and_retrieval(monkeypatch, raise_kg_unavailable=True)

    record = await pipeline_module._process_one(
        mapping_id="map_kg_miss",
        claude_client=fake,
    )

    assert record.kg_skeleton_used is False
    assert record.provenance.kg_concept_ids_used == []
    assert record.provenance.must_know_presented == []

    row = _read_answer_row(temp_db, "map_kg_miss")
    assert row["kg_skeleton_used"] == 0
    persisted_provenance = json.loads(row["provenance"])
    assert persisted_provenance["kg_concept_ids_used"] == []
