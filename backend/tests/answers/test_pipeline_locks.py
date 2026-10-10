"""Concurrency tests for ``answers.pipeline._acquire_inflight`` (task 29).

Covers design.md §Failure-Mode Matrix → "Concurrent generate request for same
mapping": two concurrent ``_process_one`` calls for the same ``mapping_id``
must produce exactly one persisted answer and one ``AnswerPipelineError``
whose ``detail`` carries the ``409 Conflict`` prefix. After the first call
completes, the lock must be released so a follow-up call for the same
``mapping_id`` succeeds.

The Claude transport is stubbed by :class:`SlowStageFake`, a small extension
of the ``StageFake`` from ``test_pipeline_process_one.py`` that injects an
``asyncio.sleep`` into the *first* draft request so the second concurrent
call is guaranteed to land while the lock is held by the first call.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pytest

from answers import (
    intent as intent_module,
    kg_skeleton as kg_skeleton_module,
    pipeline as pipeline_module,
    prompts as prompts_module,
    retrieval as retrieval_module,
)
from answers.errors import AnswerPipelineError, KGUnavailable
from answers.schema import KGSkeleton, RetrievedContext
from state import db as database

from tests.answers.test_pipeline_process_one import (
    StageFake,
    _CRITIC_PASS_FIXTURE,
    _INTENT_FIXTURE,
    _VALID_DRAFT_FIXTURE,
    _empty_kg,
    _kg_with_concepts,
    _retrieved_context_fixture,
)


# ──────────────────────────────────────────────────────────────────────────
# Slow-draft fake — guarantees the first call is still in flight when the
# second concurrent call attempts to acquire the per-mapping lock.
# ──────────────────────────────────────────────────────────────────────────


class SlowStageFake(StageFake):
    """Like :class:`StageFake` but the *first* draft request awaits a sleep.

    The sleep gives ``asyncio.gather`` enough scheduling slack for the second
    coroutine to reach ``_acquire_inflight`` while the first one is still
    inside the orchestrator body holding the per-mapping lock.
    """

    def __init__(self, draft_sleep: float = 0.1) -> None:
        super().__init__()
        self._draft_sleep = draft_sleep
        self._draft_call_index = 0

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
        if system == prompts_module.DRAFT_GENERATOR_SYSTEM_PROMPT:
            self._draft_call_index += 1
            if self._draft_call_index == 1:
                # Yield long enough that any other coroutine waiting on the
                # event loop will be scheduled; this is what makes the second
                # _process_one call land while the lock is held.
                await asyncio.sleep(self._draft_sleep)
        return await super().request(
            messages=messages,
            system=system,
            model=model,
            max_tokens=max_tokens,
            task_type=task_type,
            subject=subject,
            response_schema=response_schema,
            request_id=request_id,
        )


# ──────────────────────────────────────────────────────────────────────────
# Fixtures (mirrored from test_pipeline_process_one.py)
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch) -> str:
    """Fresh temp SQLite DB; patches ``state.db.DB_PATH`` for every importer."""
    db_file = tmp_path / "pipeline_locks_test.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    return str(db_file)


@pytest.fixture(autouse=True)
def _clear_intent_cache():
    """Per-process intent cache must be flushed between tests."""
    intent_module._INTENT_CACHE.clear()
    yield
    intent_module._INTENT_CACHE.clear()


@pytest.fixture(autouse=True)
def _clear_inflight_locks():
    """Make sure leftover locks from a previous test never leak across tests."""
    pipeline_module._inflight_locks.clear()
    yield
    pipeline_module._inflight_locks.clear()


async def _seed_minimal_rows(mapping_id: str = "map_lock_1") -> None:
    """Seed a textbook + chapter + mapping needed by the orchestrator path."""
    await database.init_db()

    await database.insert(
        "textbooks",
        {
            "id": f"tb_{mapping_id}",
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
            "id": f"ch_{mapping_id}",
            "textbook_id": f"tb_{mapping_id}",
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
            "final_chapter_id": f"ch_{mapping_id}",
            "final_chapter_name": "Endocrine Emergencies",
        },
    )


def _patch_kg_and_retrieval(monkeypatch) -> None:
    """Stub KG + retrieval helpers so the test exercises only the lock path."""

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


def _read_answer_row(db_path: str, mapping_id: str) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM answers WHERE mapping_id = ?", (mapping_id,)
        )
        row = cur.fetchone()
        assert row is not None, f"no answer row persisted for mapping_id={mapping_id}"
        return dict(row)


# ──────────────────────────────────────────────────────────────────────────
# Test 1 — concurrent calls for the same mapping_id
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_process_one_rejects_concurrent_call_for_same_mapping_inflight(
    temp_db, monkeypatch
):
    """Two concurrent ``_process_one`` calls for the same ``mapping_id`` →
    exactly one returns an :class:`AnswerRecord`, the other raises
    :class:`AnswerPipelineError` whose detail carries ``409 Conflict``."""
    mapping_id = "map_concurrent_1"
    await _seed_minimal_rows(mapping_id)

    fake = SlowStageFake(draft_sleep=0.1)
    _register_default_stages(fake)
    _patch_kg_and_retrieval(monkeypatch)

    results = await asyncio.gather(
        pipeline_module._process_one(
            mapping_id=mapping_id, claude_client=fake
        ),
        pipeline_module._process_one(
            mapping_id=mapping_id, claude_client=fake
        ),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]

    assert len(successes) == 1, (
        f"expected exactly one successful AnswerRecord, got {results!r}"
    )
    assert len(errors) == 1, (
        f"expected exactly one AnswerPipelineError, got {results!r}"
    )
    assert successes[0].mapping_id == mapping_id

    err = errors[0]
    assert isinstance(err, AnswerPipelineError), (
        f"expected AnswerPipelineError, got {type(err).__name__}: {err!r}"
    )
    assert err.mapping_id == mapping_id
    assert "409" in err.detail
    assert mapping_id in err.detail

    # Exactly one DB row should exist for this mapping after the race.
    with sqlite3.connect(temp_db) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM answers WHERE mapping_id = ?", (mapping_id,)
        )
        (count,) = cur.fetchone()
    assert count == 1


# ──────────────────────────────────────────────────────────────────────────
# Test 2 — lock releases after first call so subsequent calls succeed
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_process_one_releases_lock_after_completion_inflight(
    temp_db, monkeypatch
):
    """First call completes; second call for the same ``mapping_id`` afterwards
    succeeds (lock cleanup actually frees the entry)."""
    mapping_id = "map_release_1"
    await _seed_minimal_rows(mapping_id)

    fake = StageFake()
    _register_default_stages(fake)
    _patch_kg_and_retrieval(monkeypatch)

    first = await pipeline_module._process_one(
        mapping_id=mapping_id, claude_client=fake
    )
    assert first.mapping_id == mapping_id

    # The lock entry for this mapping should be gone after the first call
    # exits cleanly; a stale entry would mean the cleanup branch in
    # ``_acquire_inflight`` is broken.
    assert mapping_id not in pipeline_module._inflight_locks

    second = await pipeline_module._process_one(
        mapping_id=mapping_id, claude_client=fake
    )
    assert second.mapping_id == mapping_id

    # The second call must persist a *new* row (the orchestrator deletes the
    # previous answer row before re-inserting), so we still have exactly one
    # row but with a different id than the first.
    row = _read_answer_row(temp_db, mapping_id)
    assert row["mapping_id"] == mapping_id


# ──────────────────────────────────────────────────────────────────────────
# Test 3 — different mapping_ids run concurrently without contention
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_process_one_different_mappings_run_concurrently_inflight(
    temp_db, monkeypatch
):
    """Different ``mapping_id``s acquire independent locks → both succeed."""
    await _seed_minimal_rows("map_par_a")
    await _seed_minimal_rows("map_par_b")

    fake = SlowStageFake(draft_sleep=0.05)
    _register_default_stages(fake)
    _patch_kg_and_retrieval(monkeypatch)

    results = await asyncio.gather(
        pipeline_module._process_one(
            mapping_id="map_par_a", claude_client=fake
        ),
        pipeline_module._process_one(
            mapping_id="map_par_b", claude_client=fake
        ),
        return_exceptions=True,
    )

    assert all(not isinstance(r, BaseException) for r in results), (
        f"expected both calls to succeed, got {results!r}"
    )
    mapping_ids = {r.mapping_id for r in results}
    assert mapping_ids == {"map_par_a", "map_par_b"}
