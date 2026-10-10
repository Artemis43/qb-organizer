"""Unit tests for ``answers.pipeline.purge_answers`` (task 32, R10).

Covers the documented behaviour of the admin purge action:

1. ``confirm=False`` is rejected with an :class:`AnswerPipelineError` whose
   detail begins with ``"400"`` (R10.7).
2. Subject-less purge with ``confirm=True`` deletes every row in ``answers``
   and returns ``firestore_documents_deleted=0`` when no Firebase service
   account is configured (R10.2 + §Failure-Mode Matrix Firestore-unavailable
   row).
3. Subject-scoped purge with ``confirm=True`` deletes only the rows whose
   mapping joins to a question_paper with that subject; rows for other
   subjects are untouched (R10.3).

The Firestore service-account file is gated by
``settings.firebase_service_account_path``; tests point it to a
non-existent path so ``_firestore_service_account_configured`` returns
``False`` and the Firestore-side delete short-circuits to ``0`` without
contacting any external service.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from answers import pipeline as pipeline_module
from answers.errors import AnswerPipelineError
from answers.schema import PurgeReport
from config import settings
from state import db as database


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch) -> str:
    """Fresh temp SQLite DB; patches ``state.db.DB_PATH`` so every importer
    sees the same file."""
    db_file = tmp_path / "pipeline_purge_test.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    return str(db_file)


@pytest.fixture(autouse=True)
def _no_firestore(tmp_path: Path, monkeypatch):
    """Point ``firebase_service_account_path`` at a path that does not exist
    so ``_firestore_service_account_configured`` returns ``False`` and the
    Firestore-side delete reports 0 without raising."""
    missing = tmp_path / "no-such-firebase-account.json"
    monkeypatch.setattr(settings, "firebase_service_account_path", str(missing))
    yield


async def _seed_two_subjects(answers_per_subject: int = 3) -> None:
    """Seed two question_papers (one per subject), one mapping + one answer
    per subject's question, plus the supporting question rows.

    Layout::

        General Surgery  →  qp_surg  →  q_surg_1   → map_surg_1   → ans_surg_1
                                       q_surg_2   → map_surg_2   → ans_surg_2
                                       ...
        General Medicine →  qp_med   →  q_med_1    → map_med_1    → ans_med_1
                                       ...

    The exact value of ``answers_per_subject`` is small (3 in tests) so the
    test stays fast and doesn't need page-batch logic.
    """
    await database.init_db()

    for subject_short, subject_full in [
        ("surg", "General Surgery"),
        ("med", "General Medicine"),
    ]:
        await database.insert(
            "question_papers",
            {
                "id": f"qp_{subject_short}",
                "filename": f"{subject_short}.pdf",
                "subject": subject_full,
                "sha256_hash": f"hash_qp_{subject_short}",
                "status": "ready",
            },
        )
        for i in range(1, answers_per_subject + 1):
            q_id = f"q_{subject_short}_{i}"
            map_id = f"map_{subject_short}_{i}"
            ans_id = f"ans_{subject_short}_{i}"

            await database.insert(
                "questions",
                {
                    "id": q_id,
                    "qp_id": f"qp_{subject_short}",
                    "question_number": str(i),
                    "question_text": f"Question {i} for {subject_full}.",
                    "question_type": "SAQ",
                    "status": "ready",
                },
            )
            await database.insert(
                "mappings",
                {
                    "id": map_id,
                    "question_id": q_id,
                    "question_text": f"Question {i} for {subject_full}.",
                    "question_type": "SAQ",
                    "qp_id": f"qp_{subject_short}",
                },
            )
            await database.insert(
                "answers",
                {
                    "id": ans_id,
                    "mapping_id": map_id,
                    "question_text": f"Question {i} for {subject_full}.",
                    "status": "generated",
                },
            )


def _count_rows(db_path: str, table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchone()[0]


def _existing_answer_ids(db_path: str) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT id FROM answers ORDER BY id").fetchall()
    return [r[0] for r in rows]


# ──────────────────────────────────────────────────────────────────────────
# Test 1 — confirm=False is rejected with a 400-prefixed error
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_purge_answers_without_confirm_raises_400(temp_db):
    """``purge_answers(None, confirm=False)`` raises ``AnswerPipelineError``
    whose detail starts with ``"400"`` per R10.7."""
    # init_db so the answers table exists; the function must refuse before
    # touching any row.
    await database.init_db()

    with pytest.raises(AnswerPipelineError) as exc_info:
        await pipeline_module.purge_answers(subject=None, confirm=False)

    assert "400" in exc_info.value.detail
    assert "confirm" in exc_info.value.detail.lower()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_purge_answers_subject_filter_without_confirm_raises_400(temp_db):
    """Even with a subject filter, ``confirm=False`` must refuse (R10.7)."""
    await database.init_db()

    with pytest.raises(AnswerPipelineError) as exc_info:
        await pipeline_module.purge_answers(
            subject="General Surgery", confirm=False
        )

    assert exc_info.value.detail.startswith("400")


# ──────────────────────────────────────────────────────────────────────────
# Test 2 — subject-less purge deletes all rows (R10.2)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_purge_answers_global_deletes_every_row(temp_db):
    """``purge_answers(None, confirm=True)`` deletes every row in
    ``answers`` and reports the correct counts."""
    await _seed_two_subjects(answers_per_subject=3)

    # Sanity: 6 answer rows total before the purge (3 + 3).
    assert _count_rows(temp_db, "answers") == 6

    report: PurgeReport = await pipeline_module.purge_answers(
        subject=None, confirm=True
    )

    # Every row gone (R10.2).
    assert _count_rows(temp_db, "answers") == 0

    # PurgeReport reflects the deletion. ``mappings_reset`` covers every
    # mapping in scope (R10.4), including those that had no answer row.
    assert report.answers_deleted == 6
    # 6 mappings total (3 per subject), all in scope when subject is None.
    assert report.mappings_reset == 6
    # No service account configured → Firestore delete is a 0-doc no-op.
    assert report.firestore_documents_deleted == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_purge_answers_global_with_no_data_returns_zero_counts(temp_db):
    """An empty ``answers`` table still returns a valid :class:`PurgeReport`
    with all-zero counts when ``confirm=True``."""
    await database.init_db()

    report = await pipeline_module.purge_answers(subject=None, confirm=True)

    assert report.answers_deleted == 0
    assert report.mappings_reset == 0
    assert report.firestore_documents_deleted == 0


# ──────────────────────────────────────────────────────────────────────────
# Test 3 — subject-scoped purge leaves other subjects untouched (R10.3)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_purge_answers_subject_scope_only_touches_in_scope_rows(temp_db):
    """``purge_answers("General Surgery", confirm=True)`` deletes only the
    Surgery answers and leaves Medicine answers intact (R10.3)."""
    await _seed_two_subjects(answers_per_subject=3)

    # Pre-conditions.
    surg_before = _count_rows(
        temp_db,
        "answers",
        "mapping_id IN (SELECT id FROM mappings WHERE qp_id='qp_surg')",
    )
    med_before = _count_rows(
        temp_db,
        "answers",
        "mapping_id IN (SELECT id FROM mappings WHERE qp_id='qp_med')",
    )
    assert surg_before == 3 and med_before == 3

    report = await pipeline_module.purge_answers(
        subject="General Surgery", confirm=True
    )

    # Surgery rows gone, Medicine rows untouched (R10.3).
    surg_after = _count_rows(
        temp_db,
        "answers",
        "mapping_id IN (SELECT id FROM mappings WHERE qp_id='qp_surg')",
    )
    med_after = _count_rows(
        temp_db,
        "answers",
        "mapping_id IN (SELECT id FROM mappings WHERE qp_id='qp_med')",
    )
    assert surg_after == 0
    assert med_after == 3

    # Medicine answer ids are unchanged.
    remaining_ids = _existing_answer_ids(temp_db)
    assert remaining_ids == ["ans_med_1", "ans_med_2", "ans_med_3"]

    # Report counts: only Surgery's 3 mappings are in scope.
    assert report.answers_deleted == 3
    assert report.mappings_reset == 3
    assert report.firestore_documents_deleted == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_purge_answers_unknown_subject_returns_zero_counts(temp_db):
    """Subject filter that matches nothing returns a no-op report."""
    await _seed_two_subjects(answers_per_subject=3)

    report = await pipeline_module.purge_answers(
        subject="Nonexistent Subject", confirm=True
    )

    # Nothing deleted, nothing reset, no Firestore work.
    assert report.answers_deleted == 0
    assert report.mappings_reset == 0
    assert report.firestore_documents_deleted == 0

    # All 6 rows still present.
    assert _count_rows(temp_db, "answers") == 6
