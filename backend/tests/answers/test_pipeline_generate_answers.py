"""Integration tests for ``answers.pipeline.generate_answers`` (task 30).

Covers the three documented branches of the batch driver:

1. 3-mapping batch with 1 mid-batch failure → ``generated=2, failed=1`` and
   the failed mapping_id surfaces in ``errors``.
2. Empty ``mapping_ids`` list → zero-result ``GenerateBatchResult``.
3. ``progress_callback`` is invoked once per persisted mapping with the
   ``persisted`` event.

The Claude transport is stubbed by the ``StageFake`` already used by
``test_pipeline_process_one.py``; KG + retrieval helpers are monkey-patched
the same way so the tests don't need Chroma or seeded concepts.

The mid-batch failure is created by **not** seeding one of the three
mapping rows — when ``_process_one`` calls ``_load_mapping_context`` the
missing row triggers ``AnswerPipelineError(mapping_id, "mapping not found")``,
which the batch driver catches and records in ``errors``.
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
    db_file = tmp_path / "pipeline_generate_answers_test.db"
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


async def _seed_textbook_and_chapter() -> None:
    """Insert one textbook + chapter row that all seeded mappings share."""
    await database.init_db()
    await database.insert(
        "textbooks",
        {
            "id": "tb_batch",
            "filename": "test.pdf",
            "name": "Test Textbook",
            "subject": "general_medicine",
            "sha256_hash": "hash_batch",
            "status": "ready",
        },
    )
    await database.insert(
        "chapters",
        {
            "id": "ch_batch",
            "textbook_id": "tb_batch",
            "chapter_number": 1,
            "name": "Endocrine Emergencies",
            "start_page": 1,
            "end_page": 30,
            "status": "ready",
        },
    )


async def _seed_mapping(mapping_id: str) -> None:
    await database.insert(
        "mappings",
        {
            "id": mapping_id,
            "question_id": f"q_{mapping_id}",
            "question_text": "Discuss the pathophysiology of diabetic ketoacidosis.",
            "question_type": "SAQ",
            "qp_id": f"qp_{mapping_id}",
            "final_chapter_id": "ch_batch",
            "final_chapter_name": "Endocrine Emergencies",
        },
    )


def _patch_kg_and_retrieval(monkeypatch) -> None:
    """Stub KG + retrieval helpers so the test exercises only the batch path."""

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


def _read_answer_count(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM answers")
        (count,) = cur.fetchone()
    return count


# ──────────────────────────────────────────────────────────────────────────
# Test 1 — 3-mapping batch with mid-batch failure
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_generate_answers_three_mapping_batch_with_one_failure(
    temp_db, monkeypatch
):
    """A 3-mapping batch where the middle mapping is missing → ``generated=2``,
    ``failed=1``, both successes persisted, errors lists the missing
    ``mapping_id``. The batch never aborts."""
    await _seed_textbook_and_chapter()
    await _seed_mapping("map_batch_a")
    # Intentionally skip "map_batch_b" so _load_mapping_context raises.
    await _seed_mapping("map_batch_c")

    fake = StageFake()
    _register_default_stages(fake)
    _patch_kg_and_retrieval(monkeypatch)

    result = await pipeline_module.generate_answers(
        mapping_ids=["map_batch_a", "map_batch_b", "map_batch_c"],
        claude_client=fake,
    )

    assert result.generated == 2
    assert result.failed == 1
    assert result.cost_blocked == 0
    assert len(result.answer_ids) == 2
    assert all(aid.startswith("ans_") for aid in result.answer_ids)
    assert len(result.errors) == 1
    assert "map_batch_b" in result.errors[0]
    assert "mapping not found" in result.errors[0].lower()

    # Both successful answers were persisted; the failed one was not.
    assert _read_answer_count(temp_db) == 2

    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT mapping_id FROM answers ORDER BY mapping_id"
        ).fetchall()
        persisted_ids = {row["mapping_id"] for row in rows}
    assert persisted_ids == {"map_batch_a", "map_batch_c"}

    # Spend snapshots are zero-or-more floats (real wiring lands in task 33).
    assert result.batch_spend_usd >= 0.0
    assert result.daily_spend_usd >= 0.0


# ──────────────────────────────────────────────────────────────────────────
# Test 2 — empty mapping_ids list
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_generate_answers_empty_list_returns_zero_result(
    temp_db, monkeypatch
):
    """Empty ``mapping_ids`` returns zero counts, empty answer_ids, empty
    errors, and a populated daily-spend snapshot from the seeded log row."""
    await _seed_textbook_and_chapter()

    fake = StageFake()
    _register_default_stages(fake)
    _patch_kg_and_retrieval(monkeypatch)

    result = await pipeline_module.generate_answers(
        mapping_ids=[],
        claude_client=fake,
    )

    assert result.generated == 0
    assert result.failed == 0
    assert result.cost_blocked == 0
    assert result.answer_ids == []
    assert result.errors == []
    assert result.batch_spend_usd == 0.0
    assert result.daily_spend_usd >= 0.0

    # ``daily_cost_log`` row was seeded for today so subsequent calls observe
    # a consistent shape.
    with sqlite3.connect(temp_db) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM daily_cost_log")
        (count,) = cur.fetchone()
    assert count >= 1


# ──────────────────────────────────────────────────────────────────────────
# Test 3 — progress_callback invoked once per persisted mapping
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_generate_answers_progress_callback_emits_persisted_per_success(
    temp_db, monkeypatch
):
    """The progress callback fires the ``persisted`` event exactly once per
    successful mapping; failures don't emit ``persisted``."""
    await _seed_textbook_and_chapter()
    await _seed_mapping("map_cb_a")
    # Skip "map_cb_b" to verify it does NOT emit ``persisted``.
    await _seed_mapping("map_cb_c")

    fake = StageFake()
    _register_default_stages(fake)
    _patch_kg_and_retrieval(monkeypatch)

    captured: list[tuple[str, dict]] = []

    async def _callback(event_name: str, data: dict) -> None:
        captured.append((event_name, dict(data)))

    result = await pipeline_module.generate_answers(
        mapping_ids=["map_cb_a", "map_cb_b", "map_cb_c"],
        progress_callback=_callback,
        claude_client=fake,
    )

    assert result.generated == 2
    assert result.failed == 1

    persisted_events = [
        (name, data) for name, data in captured if name == "persisted"
    ]
    assert len(persisted_events) == 2

    persisted_mapping_ids = {data["mapping_id"] for _, data in persisted_events}
    assert persisted_mapping_ids == {"map_cb_a", "map_cb_c"}

    # Each persisted event carries the freshly-minted answer_id, and those
    # answer_ids match the ones returned in the result object.
    persisted_answer_ids = {data["answer_id"] for _, data in persisted_events}
    assert persisted_answer_ids == set(result.answer_ids)
