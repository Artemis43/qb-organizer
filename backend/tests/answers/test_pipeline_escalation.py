"""Integration tests for the cost-gated Sonnet escalation branch (task 33).

Covers the two new branches added to ``_process_one_locked`` after both the
initial Haiku critic and the Haiku regen still return ``verdict="fail"``:

1. **sonnet_allowed** — cost gate returns ``"allow"`` → pipeline regenerates
   with ``settings.sonnet_model``, re-runs the critic (which stays on Haiku
   per R9.2), and the persisted ``provenance.escalation_model`` matches
   ``settings.sonnet_model`` while ``critic_verdict`` flips to ``"pass"``.

2. **sonnet_blocked** — cost gate returns ``"block"`` (forced via a tiny
   ``batch_cost_ceiling_usd``) → pipeline persists the latest Haiku draft
   with ``provenance.cost_ceiling_blocked=True`` and ``escalation_model is
   None``, and emits exactly one ``cost_blocked`` SSE event.

Both tests reuse the ``StageFake`` from ``test_pipeline_process_one.py`` so
a single fake client serves intent / draft / critic stages and can return
different responses per call via ``register_sequence``.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from answers import (
    intent as intent_module,
    kg_skeleton as kg_skeleton_module,
    pipeline as pipeline_module,
    prompts as prompts_module,
    retrieval as retrieval_module,
)
from answers.errors import KGUnavailable
from answers.schema import KGSkeleton, RetrievedContext
from config import settings
from state import db as database


# ──────────────────────────────────────────────────────────────────────────
# Stage-dispatching Claude fake (mirrors test_pipeline_process_one.py)
# ──────────────────────────────────────────────────────────────────────────


class StageFake:
    """Per-system-prompt Claude fake.

    ``register(system, response)`` sets one fixed response.
    ``register_sequence(system, [r1, r2, ...])`` cycles through a list (the
    last response repeats once the cursor exhausts) so a single critic /
    draft instance can return different payloads on successive calls — used
    here to drive the two-fail-then-pass flow needed by the escalation tests.
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
            {"system": system, "task_type": task_type, "model": model}
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

_HAIKU_DRAFT_FIXTURE = {
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

_HAIKU_REGEN_FIXTURE = {
    "definition": (
        "DKA is a life-threatening complication of diabetes mellitus "
        "characterised by hyperglycaemia, ketonaemia, and metabolic "
        "acidosis."
    ),
    "etiology": [
        {
            "text": "Missed insulin doses or new-onset type 1 diabetes",
            "source_chunk_ids": ["chunk_1"],
            "kg_concept_ids": ["c_dka"],
        }
    ],
    "mnemonics": {},
}

_SONNET_DRAFT_FIXTURE = {
    "definition": (
        "Diabetic ketoacidosis is a life-threatening hyperglycaemic "
        "emergency defined by the triad of hyperglycaemia (>250 mg/dL), "
        "ketonaemia/ketonuria, and high anion-gap metabolic acidosis "
        "(pH < 7.30, HCO3 < 18 mEq/L)."
    ),
    "etiology": [
        {
            "text": (
                "Absolute insulin deficiency: insulin omission, new-onset "
                "type 1 diabetes mellitus, or pump failure."
            ),
            "source_chunk_ids": ["chunk_1"],
            "kg_concept_ids": ["c_dka"],
        }
    ],
    "mnemonics": {},
}

_CRITIC_FAIL_FIXTURE = {
    "verdict": "fail",
    "score": 50,
    "structure_score": 40,
    "depth_score": 50,
    "traceability_score": 60,
    "mandatory_data_score": 60,
    "too_shallow_bullets": ["definition"],
    "too_deep_bullets": [],
    "missing_must_know": [],
    "feedback": ["needs more depth"],
}

_CRITIC_PASS_FIXTURE = {
    "verdict": "pass",
    "score": 88,
    "structure_score": 90,
    "depth_score": 85,
    "traceability_score": 85,
    "mandatory_data_score": 95,
    "too_shallow_bullets": [],
    "too_deep_bullets": [],
    "missing_must_know": [],
    "feedback": [],
}


def _kg_with_concepts() -> KGSkeleton:
    return KGSkeleton(
        concept_ids=["c_dka"],
        relation_ids=[],
        must_know_concept_ids=["c_dka"],
        skeleton_block_text="Concepts:\n  - DKA (disease) [must_know]",
        used=True,
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
    """Fresh temp SQLite DB with the full schema applied."""
    db_file = tmp_path / "pipeline_escalation_test.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    return str(db_file)


@pytest.fixture(autouse=True)
def _clear_intent_cache():
    """Intent classifier caches per-process; clear before/after each test."""
    intent_module._INTENT_CACHE.clear()
    yield
    intent_module._INTENT_CACHE.clear()


async def _seed_minimal_rows(mapping_id: str) -> None:
    """Insert one textbook + chapter + mapping referencing them."""
    await database.init_db()

    await database.insert(
        "textbooks",
        {
            "id": "tb_test_esc",
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
            "id": "ch_test_esc",
            "textbook_id": "tb_test_esc",
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
            "question_id": f"q_{mapping_id}",
            "question_text": "Discuss the pathophysiology of diabetic ketoacidosis.",
            "question_type": "SAQ",
            "qp_id": f"qp_{mapping_id}",
            "final_chapter_id": "ch_test_esc",
            "final_chapter_name": "Endocrine Emergencies",
        },
    )


def _patch_kg_and_retrieval(
    monkeypatch,
    *,
    kg_skeleton: Optional[KGSkeleton] = None,
    retrieved: Optional[RetrievedContext] = None,
) -> None:
    """Stub the non-Claude side helpers (KG + retrieval) at module level."""

    async def _fake_build_kg(question_text, subject):
        return kg_skeleton if kg_skeleton is not None else _kg_with_concepts()

    async def _fake_retrieve(question_text, chapter_id, subject, mode):
        return retrieved if retrieved is not None else _retrieved_context_fixture()

    monkeypatch.setattr(kg_skeleton_module, "build_kg_skeleton", _fake_build_kg)
    monkeypatch.setattr(
        pipeline_module.kg_skeleton_module, "build_kg_skeleton", _fake_build_kg
    )
    monkeypatch.setattr(retrieval_module, "retrieve", _fake_retrieve)
    monkeypatch.setattr(
        pipeline_module.retrieval_module, "retrieve", _fake_retrieve
    )


def _patch_settings_with_ceilings(
    monkeypatch,
    *,
    batch_cost_ceiling_usd: float,
    daily_cost_ceiling_usd: float,
) -> None:
    """Swap ``pipeline_module.settings`` with a shim carrying the cost ceilings.

    The real ``Settings`` is a strict pydantic model that rejects unknown
    fields, so we cannot ``monkeypatch.setattr(settings, ...)`` for the
    new ``batch_cost_ceiling_usd`` / ``daily_cost_ceiling_usd`` fields
    until task 35 lands. Instead we build a ``SimpleNamespace`` exposing
    the same ``haiku_model`` / ``sonnet_model`` attributes the pipeline
    actually uses, plus the two new ceiling fields.
    """
    shim = SimpleNamespace(
        haiku_model=settings.haiku_model,
        sonnet_model=settings.sonnet_model,
        batch_cost_ceiling_usd=batch_cost_ceiling_usd,
        daily_cost_ceiling_usd=daily_cost_ceiling_usd,
    )
    monkeypatch.setattr(pipeline_module, "settings", shim)


def _read_answer_row(db_path: str, mapping_id: str) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM answers WHERE mapping_id = ?", (mapping_id,)
        )
        row = cursor.fetchone()
        assert row is not None, f"no answer row persisted for mapping_id={mapping_id}"
        return dict(row)


# ──────────────────────────────────────────────────────────────────────────
# Test 1 — sonnet_allowed: gate allows escalation, Sonnet draft passes critic
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_process_one_sonnet_escalation_allowed(temp_db, monkeypatch):
    """Both Haiku critics fail; cost gate allows; Sonnet draft passes critic.

    Verifies the R9.4 escalation path: ``provenance.escalation_model`` equals
    ``settings.sonnet_model`` and the persisted ``critic_verdict`` is ``pass``.
    """
    await _seed_minimal_rows("map_sonnet_allowed")

    fake = StageFake()
    fake.register(prompts_module.INTENT_CLASSIFIER_PROMPT, _INTENT_FIXTURE)
    # Three drafts in order: initial Haiku → Haiku regen → Sonnet escalation.
    fake.register_sequence(
        prompts_module.DRAFT_GENERATOR_SYSTEM_PROMPT,
        [_HAIKU_DRAFT_FIXTURE, _HAIKU_REGEN_FIXTURE, _SONNET_DRAFT_FIXTURE],
    )
    # Three critics in order: fail (initial), fail (after regen), pass (Sonnet).
    fake.register_sequence(
        prompts_module.EXAMINER_CRITIC_FULL_SYSTEM_PROMPT,
        [_CRITIC_FAIL_FIXTURE, _CRITIC_FAIL_FIXTURE, _CRITIC_PASS_FIXTURE],
    )
    _patch_kg_and_retrieval(monkeypatch)

    # Generous ceilings so the gate definitely allows.
    _patch_settings_with_ceilings(
        monkeypatch,
        batch_cost_ceiling_usd=100.0,
        daily_cost_ceiling_usd=100.0,
    )

    events: list[tuple[str, dict]] = []

    async def _capture(event_name: str, data: dict) -> None:
        events.append((event_name, data))

    record = await pipeline_module._process_one(
        mapping_id="map_sonnet_allowed",
        claude_client=fake,
        progress_callback=_capture,
    )

    # Escalation took place: provenance carries the Sonnet model id.
    assert record.provenance.escalation_model == settings.sonnet_model
    assert record.provenance.regen_model == settings.haiku_model
    assert record.provenance.critic_verdict == "pass"
    assert record.provenance.cost_ceiling_blocked is False

    # No ``cost_blocked`` SSE event should be emitted on the allow branch.
    cost_blocked_events = [e for e in events if e[0] == "cost_blocked"]
    assert cost_blocked_events == []

    # Persisted DB row reflects the same escalation outcome.
    row = _read_answer_row(temp_db, "map_sonnet_allowed")
    persisted_provenance = json.loads(row["provenance"])
    assert persisted_provenance["escalation_model"] == settings.sonnet_model
    assert persisted_provenance["critic_verdict"] == "pass"
    assert persisted_provenance["cost_ceiling_blocked"] is False

    # The pipeline should have called draft three times (Haiku, regen, Sonnet)
    # and critic three times (initial fail, regen fail, Sonnet pass).
    draft_calls = [
        c for c in fake.calls
        if c["system"] == prompts_module.DRAFT_GENERATOR_SYSTEM_PROMPT
    ]
    critic_calls = [
        c for c in fake.calls
        if c["system"] == prompts_module.EXAMINER_CRITIC_FULL_SYSTEM_PROMPT
    ]
    assert len(draft_calls) == 3
    assert len(critic_calls) == 3
    # The third draft call must request the Sonnet model (R9.4).
    assert draft_calls[2]["model"] == settings.sonnet_model
    # The third critic call stays on Haiku (R9.2).
    assert critic_calls[2]["model"] == settings.haiku_model


# ──────────────────────────────────────────────────────────────────────────
# Test 2 — sonnet_blocked: tiny batch ceiling blocks escalation
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_process_one_sonnet_escalation_blocked(temp_db, monkeypatch):
    """Both Haiku critics fail; batch ceiling forces gate to block.

    Verifies the R9.6 fallback: ``cost_ceiling_blocked=True``, no Sonnet
    call is made (escalation_model stays None), and exactly one
    ``cost_blocked`` SSE event is emitted with the expected payload.
    """
    await _seed_minimal_rows("map_sonnet_blocked")

    fake = StageFake()
    fake.register(prompts_module.INTENT_CLASSIFIER_PROMPT, _INTENT_FIXTURE)
    # Only two drafts should fire: initial Haiku and Haiku regen.
    fake.register_sequence(
        prompts_module.DRAFT_GENERATOR_SYSTEM_PROMPT,
        [_HAIKU_DRAFT_FIXTURE, _HAIKU_REGEN_FIXTURE],
    )
    # Two critics, both fail.
    fake.register_sequence(
        prompts_module.EXAMINER_CRITIC_FULL_SYSTEM_PROMPT,
        [_CRITIC_FAIL_FIXTURE, _CRITIC_FAIL_FIXTURE],
    )
    _patch_kg_and_retrieval(monkeypatch)

    # Tiny batch ceiling — projected Sonnet cost (~$0.05) > 0.001 → block.
    _patch_settings_with_ceilings(
        monkeypatch,
        batch_cost_ceiling_usd=0.001,
        daily_cost_ceiling_usd=100.0,
    )

    events: list[tuple[str, dict]] = []

    async def _capture(event_name: str, data: dict) -> None:
        events.append((event_name, data))

    record = await pipeline_module._process_one(
        mapping_id="map_sonnet_blocked",
        claude_client=fake,
        progress_callback=_capture,
    )

    # Returned record reflects the block.
    assert record.provenance.cost_ceiling_blocked is True
    assert record.provenance.escalation_model is None
    assert record.provenance.regen_model == settings.haiku_model
    # The Haiku draft is persisted with the latest fail verdict.
    assert record.provenance.critic_verdict == "fail"

    # Exactly one ``cost_blocked`` SSE event with the expected payload.
    cost_blocked_events = [e for e in events if e[0] == "cost_blocked"]
    assert len(cost_blocked_events) == 1
    payload = cost_blocked_events[0][1]
    assert payload["mapping_id"] == "map_sonnet_blocked"
    assert payload["stage"] == "escalation_sonnet"
    assert payload["projected_usd"] > 0
    assert payload["batch_ceiling_usd"] == 0.001
    assert payload["daily_ceiling_usd"] == 100.0

    # Persisted row matches the in-memory record.
    row = _read_answer_row(temp_db, "map_sonnet_blocked")
    persisted_provenance = json.loads(row["provenance"])
    assert persisted_provenance["cost_ceiling_blocked"] is True
    assert persisted_provenance["escalation_model"] is None
    assert persisted_provenance["critic_verdict"] == "fail"

    # No third draft / critic call should fire because the gate blocked Sonnet.
    draft_calls = [
        c for c in fake.calls
        if c["system"] == prompts_module.DRAFT_GENERATOR_SYSTEM_PROMPT
    ]
    critic_calls = [
        c for c in fake.calls
        if c["system"] == prompts_module.EXAMINER_CRITIC_FULL_SYSTEM_PROMPT
    ]
    assert len(draft_calls) == 2
    assert len(critic_calls) == 2
    # No call requested the Sonnet model.
    assert all(c["model"] != settings.sonnet_model for c in fake.calls)
