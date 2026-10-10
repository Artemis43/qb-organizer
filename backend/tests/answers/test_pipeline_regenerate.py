"""Unit tests for ``answers.pipeline.regenerate_answer`` (task 31).

Covers the three documented behaviours of the single-mapping wrapper:

1. Existing answer → ``regenerate_answer`` produces a fresh ``answer_id`` and
   returns a :class:`GenerateBatchResult` with ``generated == 1``.
2. Non-existent ``answer_id`` → raises ``AnswerPipelineError`` whose detail
   begins with ``"answer not found"``.
3. After a regenerate, exactly **one** row exists in ``answers`` for the
   mapping_id — verifying the orchestrator's DELETE-then-INSERT logic
   inside ``_process_one``.

The Claude transport is stubbed by the ``StageFake`` already used by the
``_process_one`` and batch-driver tests; KG + retrieval helpers are
monkey-patched the same way so the tests don't need Chroma or seeded
concepts.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from answers import (
    intent as intent_module,
    kg_skeleton as kg_skeleton_module,
    pipeline as pipeline_module,
    prompts as prompts_module,
    retrieval as retrieval_module,
)
from answers.errors import AnswerPipelineError
from state import db as database

from tests.answers.test_pipeline_process_one import (
    StageFake,
    _CRITIC_PASS_FIXTURE,
    _INTENT_FIXTURE,
    _VALID_DRAFT_FIXTURE,
    _kg_with_concepts,
    _retrieved_context_fixture,
)


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch) -> str:
    """Fresh temp SQLite DB; patches ``state.db.DB_PATH`` for every importer."""
    db_file = tmp_path / "pipeline_regenerate_test.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    return str(db_file)


@pytest.fixture(autouse=True)
def _clear_intent_cache():
    intent_module._INTENT_CACHE.clear()
    yield
    intent_module._INTENT_CACHE.clear()


@pytest.fixture(autouse=True)
def _clear_inflight_locks():
    pipeline_module._inflight_locks.clear()
    yield
    pipeline_module._inflight_locks.clear()


async def _seed_textbook_chapter_mapping(mapping_id: str) -> None:
    """Insert the minimal textbook + chapter + mapping rows the orchestrator
    needs for a SAQ ``discuss`` flow."""
    await database.init_db()
    await database.insert(
        "textbooks",
        {
            "id": "tb_regen",
            "filename": "test.pdf",
            "name": "Test Textbook",
            "subject": "general_medicine",
            "sha256_hash": f"hash_regen_{mapping_id}",
            "status": "ready",
        },
    )
    await database.insert(
        "chapters",
        {
            "id": "ch_regen",
            "textbook_id": "tb_regen",
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
            "final_chapter_id": "ch_regen",
            "final_chapter_name": "Endocrine Emergencies",
        },
    )


def _patch_kg_and_retrieval(monkeypatch) -> None:
    """Stub KG + retrieval so the test exercises only the regenerate path."""

    async def _fake_build_kg(question_text, subject):
        return _kg_with_concepts()

    async def _fake_retrieve(question_text, chapter_id, subject, mode):
        return _retrieved_context_fixture()

    monkeypatch.setattr(kg_skeleton_module, "build_kg_skeleton", _fake_build_kg)
    monkeypatch.setattr(
        pipeline_module.kg_skeleton_module, "build_kg_skeleton", _fake_build_kg
    )
    monkeypatch.setattr(retrieval_module, "retrieve", _fake_retrieve)
    monkeypatch.setattr(
        pipeline_module.retrieval_module, "retrieve", _fake_retrieve
    )


def _register_default_stages(fake: StageFake) -> None:
    fake.register(prompts_module.INTENT_CLASSIFIER_PROMPT, _INTENT_FIXTURE)
    fake.register(
        prompts_module.DRAFT_GENERATOR_SYSTEM_PROMPT, _VALID_DRAFT_FIXTURE
    )
    fake.register(
        prompts_module.EXAMINER_CRITIC_FULL_SYSTEM_PROMPT, _CRITIC_PASS_FIXTURE
    )
    fake.register(
        prompts_module.EXAMINER_CRITIC_VSAQ_LITE_SYSTEM_PROMPT,
        _CRITIC_PASS_FIXTURE,
    )


def _read_answer_ids_for_mapping(db_path: str, mapping_id: str) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id FROM answers WHERE mapping_id = ? ORDER BY id",
            (mapping_id,),
        ).fetchall()
    return [row["id"] for row in rows]


# ──────────────────────────────────────────────────────────────────────────
# Test 1 — existing answer regenerates and returns generated=1
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_regenerate_answer_existing_id_produces_new_answer(
    temp_db, monkeypatch
):
    """First ``_process_one`` seeds an answer row; calling ``regenerate_answer``
    on that row returns ``generated=1`` with a *new* answer id."""
    await _seed_textbook_chapter_mapping("map_regen_a")

    fake = StageFake()
    _register_default_stages(fake)
    _patch_kg_and_retrieval(monkeypatch)

    # Seed: run the pipeline once so an answer row exists to regenerate.
    initial = await pipeline_module._process_one(
        mapping_id="map_regen_a",
        claude_client=fake,
    )
    initial_id = initial.id

    # Sanity check — exactly one row before regenerate.
    assert _read_answer_ids_for_mapping(temp_db, "map_regen_a") == [initial_id]

    result = await pipeline_module.regenerate_answer(
        answer_id=initial_id,
        claude_client=fake,
    )

    assert result.generated == 1
    assert result.failed == 0
    assert result.cost_blocked == 0
    assert len(result.answer_ids) == 1

    new_id = result.answer_ids[0]
    assert new_id.startswith("ans_")
    assert new_id != initial_id  # _process_one mints a fresh id


# ──────────────────────────────────────────────────────────────────────────
# Test 2 — non-existent answer_id raises AnswerPipelineError
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_regenerate_answer_unknown_id_raises_pipeline_error(
    temp_db, monkeypatch
):
    """``regenerate_answer("nonexistent")`` raises with ``"answer not found"``
    in the detail and the offending id as the ``mapping_id`` attribute (which
    we use here as the answer-id slot, per the task contract)."""
    # init_db so the answers table exists and the lookup returns None cleanly.
    await database.init_db()

    fake = StageFake()
    _register_default_stages(fake)
    _patch_kg_and_retrieval(monkeypatch)

    with pytest.raises(AnswerPipelineError) as exc_info:
        await pipeline_module.regenerate_answer(
            answer_id="ans_does_not_exist",
            claude_client=fake,
        )

    assert "answer not found" in exc_info.value.detail.lower()
    assert "ans_does_not_exist" in exc_info.value.detail
    assert exc_info.value.mapping_id == "ans_does_not_exist"


# ──────────────────────────────────────────────────────────────────────────
# Test 3 — after regenerate, only ONE row exists for the mapping_id
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_regenerate_answer_replaces_old_row_via_orchestrator_delete(
    temp_db, monkeypatch
):
    """The orchestrator's DELETE-then-INSERT in ``_process_one`` guarantees
    that exactly one ``answers`` row exists for the mapping_id after a
    regenerate. We verify by counting rows before and after."""
    await _seed_textbook_chapter_mapping("map_regen_b")

    fake = StageFake()
    _register_default_stages(fake)
    _patch_kg_and_retrieval(monkeypatch)

    seed = await pipeline_module._process_one(
        mapping_id="map_regen_b",
        claude_client=fake,
    )
    seed_id = seed.id
    before = _read_answer_ids_for_mapping(temp_db, "map_regen_b")
    assert before == [seed_id]

    result = await pipeline_module.regenerate_answer(
        answer_id=seed_id,
        claude_client=fake,
    )
    assert result.generated == 1

    after = _read_answer_ids_for_mapping(temp_db, "map_regen_b")
    assert len(after) == 1
    # The single surviving row is the freshly-minted one from the regenerate.
    assert after == [result.answer_ids[0]]
    # And the old row is gone.
    assert seed_id not in after
