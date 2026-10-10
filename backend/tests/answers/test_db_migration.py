"""Migration idempotency tests for ``state.db.init_db``.

Design references
-----------------
- design.md §Database schema delta (R1, R2, R3, R5, R6, R9, R11, R12, R13)
- design.md §Testing Strategy → Migration tests
- tasks.md → Task 21 (Migration idempotency test)

These tests verify that ``init_db`` is safe to re-run on an already-migrated
database. The per-column ``ALTER TABLE`` loop in ``state/db.py`` is wrapped in
``try/except aiosqlite.OperationalError`` so the second call must:

1. Raise no exception.
2. Leave the ``answers`` table's column shape byte-equal to the first run.
3. Keep the ``daily_cost_log`` table in place (idempotent ``CREATE TABLE IF
   NOT EXISTS``).
"""
from __future__ import annotations

import sqlite3

import pytest

from state import db as state_db


# The 11 new answer columns the migration must add on a fresh DB.
_NEW_ANSWER_COLUMNS: list[str] = [
    "sections",
    "markdown",
    "coverage_mode",
    "question_intent",
    "focus_topic",
    "requested_item_count",
    "intent_confidence_low",
    "provenance",
    "mandatory_data_complete",
    "vsaq_disambiguation_fallback",
    "kg_skeleton_used",
]


def _snapshot_answers_columns(db_path: str) -> list[tuple]:
    """Return ``PRAGMA table_info('answers')`` rows as a list of plain tuples.

    Using plain tuples lets us assert byte-equal equality across two runs.
    """
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("PRAGMA table_info('answers')")
        return [tuple(row) for row in cursor.fetchall()]


def _daily_cost_log_exists(db_path: str) -> bool:
    """Return ``True`` iff the ``daily_cost_log`` table is present."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_cost_log'"
        )
        return cursor.fetchone() is not None


@pytest.mark.asyncio
async def test_init_db_is_idempotent(tmp_path, monkeypatch):
    """``init_db`` must be safe to call repeatedly on the same DB file.

    Validates: Requirements R1, R5, R6, R9, R12 (persistence migration safety)
    """
    db_file = tmp_path / "idempotent.db"
    monkeypatch.setattr(state_db, "DB_PATH", str(db_file))

    # First run: creates schema + applies all migrations.
    await state_db.init_db()
    first_columns = _snapshot_answers_columns(str(db_file))
    first_daily_cost_log = _daily_cost_log_exists(str(db_file))

    assert first_daily_cost_log is True, "daily_cost_log should be created on first init"
    assert len(first_columns) > 0, "answers table should have columns after init"

    # Second run on the same DB must be a no-op and raise nothing.
    await state_db.init_db()  # If any ALTER leaks OperationalError, this raises.
    second_columns = _snapshot_answers_columns(str(db_file))
    second_daily_cost_log = _daily_cost_log_exists(str(db_file))

    # Byte-equal column shape across both runs.
    assert second_columns == first_columns, (
        "PRAGMA table_info('answers') must be identical across init_db calls"
    )
    assert second_daily_cost_log is True, "daily_cost_log must still exist after re-init"


@pytest.mark.asyncio
async def test_init_db_creates_new_columns_on_fresh_db(tmp_path, monkeypatch):
    """On a fresh DB, ``init_db`` must create all 11 new columns and ``daily_cost_log``.

    Validates: Requirements R1, R5, R6, R9, R12 (persistence shape)
    """
    db_file = tmp_path / "fresh.db"
    monkeypatch.setattr(state_db, "DB_PATH", str(db_file))

    await state_db.init_db()

    columns = _snapshot_answers_columns(str(db_file))
    column_names = {row[1] for row in columns}  # row = (cid, name, type, notnull, dflt, pk)

    missing = [c for c in _NEW_ANSWER_COLUMNS if c not in column_names]
    assert missing == [], f"missing migration columns on fresh DB: {missing}"

    assert _daily_cost_log_exists(str(db_file)) is True
