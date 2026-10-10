# qb-organizer/backend/state/db.py
"""SQLite database management with async support and auto-migrations."""

import aiosqlite
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from config import settings

logger = logging.getLogger(__name__)

DB_PATH = str(settings.db_path)

SCHEMA_SQL = """
-- Textbooks
CREATE TABLE IF NOT EXISTS textbooks (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    sha256_hash TEXT UNIQUE NOT NULL,
    total_pages INTEGER DEFAULT 0,
    total_chapters INTEGER DEFAULT 0,
    total_chars INTEGER DEFAULT 0,
    total_images INTEGER DEFAULT 0,
    file_size_mb REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    kg_status TEXT DEFAULT 'not_built',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Chapters
CREATE TABLE IF NOT EXISTS chapters (
    id TEXT PRIMARY KEY,
    textbook_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    name TEXT NOT NULL,
    start_page INTEGER NOT NULL,
    end_page INTEGER NOT NULL,
    total_chars INTEGER DEFAULT 0,
    total_chunks INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    summary TEXT,
    topics TEXT,          -- JSON array
    key_terms TEXT,       -- JSON array
    exam_likely_topics TEXT,  -- JSON array
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (textbook_id) REFERENCES textbooks(id)
);

-- Text Chunks
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL,
    textbook_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    page_numbers TEXT NOT NULL,  -- JSON array
    section_heading TEXT,
    char_count INTEGER DEFAULT 0,
    has_diagrams INTEGER DEFAULT 0,
    embedding_id TEXT,
    FOREIGN KEY (chapter_id) REFERENCES chapters(id),
    FOREIGN KEY (textbook_id) REFERENCES textbooks(id)
);

-- Question Papers
CREATE TABLE IF NOT EXISTS question_papers (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    subject TEXT NOT NULL,
    university TEXT,
    year INTEGER,
    month TEXT,
    paper_number TEXT,
    schema TEXT,
    total_pages INTEGER DEFAULT 0,
    total_questions INTEGER DEFAULT 0,
    sha256_hash TEXT UNIQUE NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now'))
);

-- Extracted Questions
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    qp_id TEXT NOT NULL,
    question_number TEXT,
    question_text TEXT NOT NULL,
    question_type TEXT DEFAULT 'OTHER',
    marks INTEGER,
    has_sub_parts INTEGER DEFAULT 0,
    sub_parts TEXT,       -- JSON array
    raw_section TEXT,
    status TEXT DEFAULT 'pending',
    FOREIGN KEY (qp_id) REFERENCES question_papers(id)
);

-- Question Mappings (matching results)
CREATE TABLE IF NOT EXISTS mappings (
    id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL,
    question_text TEXT NOT NULL,
    question_type TEXT,
    qp_id TEXT NOT NULL,
    exam_tag TEXT,
    paper_name TEXT,
    matched_chapters TEXT, -- JSON array of ChapterMatch
    best_match TEXT,       -- JSON object
    confidence REAL DEFAULT 0,
    confidence_level TEXT DEFAULT 'low',
    is_multi_chapter INTEGER DEFAULT 0,
    is_reviewed INTEGER DEFAULT 0,
    reviewer_action TEXT,
    final_chapter_id TEXT,
    final_chapter_name TEXT,
    duplicate_group_id TEXT,
    appears_in_exams TEXT,  -- JSON array
    frequency INTEGER DEFAULT 1,
    FOREIGN KEY (question_id) REFERENCES questions(id)
);

-- Cost Tracking
CREATE TABLE IF NOT EXISTS api_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    model TEXT NOT NULL,
    task_type TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    request_id TEXT,
    subject TEXT
);

-- Retry Queue
CREATE TABLE IF NOT EXISTS retry_queue (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,      -- JSON
    task_type TEXT NOT NULL,
    attempt_count INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 5,
    last_error TEXT,
    status TEXT DEFAULT 'pending',  -- pending, success, permanent_failure
    created_at TEXT DEFAULT (datetime('now')),
    last_attempt_at TEXT
);

-- Processing Logs
CREATE TABLE IF NOT EXISTS processing_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    level TEXT DEFAULT 'info',
    task TEXT,
    message TEXT,
    details TEXT   -- JSON
);

-- Generated Answers
CREATE TABLE IF NOT EXISTS answers (
    id TEXT PRIMARY KEY,
    mapping_id TEXT NOT NULL,
    question_text TEXT NOT NULL,
    chapter_id TEXT,
    chapter_name TEXT,
    prologue TEXT,
    bullets TEXT,          -- JSON array of strings
    epilogue TEXT,
    bullet_count INTEGER DEFAULT 5,
    bullet_style TEXT DEFAULT 'detailed',  -- detailed or precise
    preset TEXT DEFAULT 'custom',          -- LAQ, SAQ, VSAQ, custom
    source_chunks TEXT,    -- JSON array of chunk IDs used
    source_pages TEXT,     -- JSON object {textbook_name: "p. X, Y"}
    images TEXT,           -- JSON array of {filename, page, caption, path}
    textbook_name TEXT,
    generated_at TEXT DEFAULT (datetime('now')),
    model_used TEXT,
    token_cost REAL DEFAULT 0,
    status TEXT DEFAULT 'generated',  -- generated, edited, approved
    retrieval_mode TEXT DEFAULT 'auto',  -- auto, graph_only, hybrid
    retrieval_metadata TEXT,             -- JSON: concepts matched, chains, provenance
    FOREIGN KEY (mapping_id) REFERENCES mappings(id)
);

-- Viva Questions (generated or manually written)
CREATE TABLE IF NOT EXISTS viva_questions (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    chapter_name TEXT NOT NULL,
    topic_name TEXT NOT NULL,
    question_text TEXT NOT NULL,
    answer_text TEXT NOT NULL,
    explained_terms TEXT,           -- JSON array of key terms in the answer
    importance TEXT DEFAULT 'standard',  -- must_know, standard, advanced
    source_chapter_id TEXT,         -- qb-organizer chapter reference
    source_pages TEXT,              -- JSON: {"Maheswari": "p. 142-145"}
    source_chunks TEXT,             -- JSON array of chunk IDs used
    difficulty INTEGER DEFAULT 1,   -- 1=easy, 2=medium, 3=hard
    is_manual INTEGER DEFAULT 0,    -- 1 if manually written by user
    firestore_id TEXT,              -- set after push to Firestore
    status TEXT DEFAULT 'generated',-- generated, reviewed, pushed
    created_at TEXT DEFAULT (datetime('now'))
);



-- ── Knowledge Graph Tables ─────────────────────────────────────

-- Medical Concepts (nodes in the knowledge graph)
CREATE TABLE IF NOT EXISTS concepts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    canonical_name TEXT NOT NULL,        -- lowercased, normalized for dedup
    concept_type TEXT NOT NULL,          -- disease, anatomy, procedure, drug, symptom, investigation, pathology, physiology, other
    umls_cui TEXT,                       -- UMLS Concept ID if available
    definition TEXT,                     -- brief definition from textbook(s)
    aliases TEXT,                        -- JSON array of alternate names/abbreviations
    subject TEXT NOT NULL,               -- e.g. "General Surgery"
    importance TEXT DEFAULT 'standard',  -- must_know, standard, advanced
    frequency INTEGER DEFAULT 1,         -- how many times seen across all sources
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Relationships between concepts (edges in the graph)
CREATE TABLE IF NOT EXISTS concept_relations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,         -- is_subtype_of, causes, presents_with, treated_by,
                                         -- investigated_by, complication_of, associated_with,
                                         -- part_of, differential_of, risk_factor_for
    confidence REAL DEFAULT 0.8,
    extracted_by TEXT DEFAULT 'claude',  -- scispacy | claude | manual
    metadata TEXT,                       -- JSON: extra context
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (source_id) REFERENCES concepts(id),
    FOREIGN KEY (target_id) REFERENCES concepts(id)
);

-- Provenance: which textbook+chapter+chunk mentions each concept
CREATE TABLE IF NOT EXISTS concept_sources (
    id TEXT PRIMARY KEY,
    concept_id TEXT NOT NULL,
    textbook_id TEXT NOT NULL,
    chapter_id TEXT,
    chunk_ids TEXT,                      -- JSON array of chunk IDs
    page_numbers TEXT,                   -- JSON array of page numbers
    extracted_text TEXT,                 -- relevant text snippet
    extraction_method TEXT DEFAULT 'claude',
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (concept_id) REFERENCES concepts(id),
    FOREIGN KEY (textbook_id) REFERENCES textbooks(id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_chapters_textbook ON chapters(textbook_id);
CREATE INDEX IF NOT EXISTS idx_chunks_chapter ON chunks(chapter_id);
CREATE INDEX IF NOT EXISTS idx_questions_qp ON questions(qp_id);
CREATE INDEX IF NOT EXISTS idx_mappings_question ON mappings(question_id);
CREATE INDEX IF NOT EXISTS idx_mappings_confidence ON mappings(confidence_level);
CREATE INDEX IF NOT EXISTS idx_retry_status ON retry_queue(status);
CREATE INDEX IF NOT EXISTS idx_answers_mapping ON answers(mapping_id);
CREATE INDEX IF NOT EXISTS idx_viva_subject ON viva_questions(subject);
CREATE INDEX IF NOT EXISTS idx_viva_status ON viva_questions(status);
CREATE INDEX IF NOT EXISTS idx_concepts_canonical ON concepts(canonical_name);
CREATE INDEX IF NOT EXISTS idx_concepts_type ON concepts(concept_type);
CREATE INDEX IF NOT EXISTS idx_concepts_subject ON concepts(subject);
CREATE INDEX IF NOT EXISTS idx_relations_source ON concept_relations(source_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON concept_relations(target_id);
CREATE INDEX IF NOT EXISTS idx_relations_type ON concept_relations(relation_type);
CREATE INDEX IF NOT EXISTS idx_sources_concept ON concept_sources(concept_id);
CREATE INDEX IF NOT EXISTS idx_sources_textbook ON concept_sources(textbook_id);
"""


async def init_db():
    """Initialize the database with the schema."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()

        # ── Schema migrations ──
        try:
            await db.execute("SELECT images FROM answers LIMIT 1")
        except Exception:
            try:
                await db.execute("ALTER TABLE answers ADD COLUMN images TEXT")
                await db.commit()
                logger.info("Migration: added 'images' column to answers table")
            except Exception:
                pass  # Column might already exist

        try:
            await db.execute("SELECT paper_name FROM mappings LIMIT 1")
        except Exception:
            try:
                await db.execute("ALTER TABLE mappings ADD COLUMN paper_name TEXT")
                await db.commit()
                logger.info("Migration: added 'paper_name' column to mappings table")
            except Exception:
                pass  # Column might already exist

        # Legacy migration: add kg_status if missing (for existing DBs before schema update)
        try:
            await db.execute("SELECT kg_status FROM textbooks LIMIT 1")
        except Exception:
            try:
                await db.execute("ALTER TABLE textbooks ADD COLUMN kg_status TEXT DEFAULT 'not_built'")
                await db.commit()
            except Exception:
                pass  # Already exists

        # Migration: add retrieval_mode and retrieval_metadata to answers
        try:
            await db.execute("SELECT retrieval_mode FROM answers LIMIT 1")
        except Exception:
            try:
                await db.execute("ALTER TABLE answers ADD COLUMN retrieval_mode TEXT DEFAULT 'auto'")
                await db.execute("ALTER TABLE answers ADD COLUMN retrieval_metadata TEXT")
                await db.commit()
                logger.info("Migration: added retrieval_mode/retrieval_metadata to answers")
            except Exception:
                pass

        # Migration: add schema to question_papers
        try:
            await db.execute("SELECT schema FROM question_papers LIMIT 1")
        except Exception:
            try:
                await db.execute("ALTER TABLE question_papers ADD COLUMN schema TEXT")
                await db.commit()
                logger.info("Migration: added schema column to question_papers")
            except Exception:
                pass

        # Migration: medical-grade answer pipeline columns on answers table (R1, R2, R3,
        # R5, R6, R9, R11, R12, R13). Each ALTER runs inside its own try/except so the
        # loop is idempotent — `aiosqlite.OperationalError` is raised when a column
        # already exists, which we silently absorb.
        NEW_ANSWER_COLS = [
            ("sections", "TEXT"),
            ("markdown", "TEXT"),
            ("coverage_mode", "TEXT"),
            ("question_intent", "TEXT"),
            ("focus_topic", "TEXT"),
            ("requested_item_count", "INTEGER"),
            ("intent_confidence_low", "INTEGER DEFAULT 0"),
            ("provenance", "TEXT"),
            ("mandatory_data_complete", "INTEGER DEFAULT 1"),
            ("vsaq_disambiguation_fallback", "INTEGER DEFAULT 0"),
            ("kg_skeleton_used", "INTEGER DEFAULT 0"),
        ]
        for col, decl in NEW_ANSWER_COLS:
            try:
                await db.execute(f"ALTER TABLE answers ADD COLUMN {col} {decl}")
                await db.commit()
                logger.info(f"Migration: added '{col}' column to answers table")
            except aiosqlite.OperationalError:
                pass  # column already exists — idempotent

        # New table for daily cost ceiling tracking (R9)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_cost_log (
                date TEXT PRIMARY KEY,
                total_usd REAL DEFAULT 0,
                calls_count INTEGER DEFAULT 0
            )
            """
        )
        await db.commit()

    logger.info(f"Database initialized at {DB_PATH}")


async def get_db():
    """Get an async database connection."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


# ── Generic CRUD helpers ──────────────────────────────────────────

async def insert(table: str, data: dict) -> str:
    """Insert a row and return its id."""
    cols = ", ".join(data.keys())
    placeholders = ", ".join(["?" for _ in data])
    values = []
    for v in data.values():
        if isinstance(v, (list, dict)):
            values.append(json.dumps(v))
        else:
            values.append(v)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})", values)
        await db.commit()
    return data.get("id", "")


async def update(table: str, row_id: str, data: dict):
    """Update a row by id."""
    set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
    values = []
    for v in data.values():
        if isinstance(v, (list, dict)):
            values.append(json.dumps(v))
        else:
            values.append(v)
    values.append(row_id)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE {table} SET {set_clause} WHERE id = ?", values)
        await db.commit()


async def fetch_one(table: str, row_id: str) -> dict | None:
    """Fetch a single row by id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
        row = await cursor.fetchone()
        if row:
            return dict(row)
    return None


async def fetch_all(table: str, where: str = "", params: tuple = (), order_by: str = "") -> list[dict]:
    """Fetch all rows with optional filtering."""
    query = f"SELECT * FROM {table}"
    if where:
        query += f" WHERE {where}"
    if order_by:
        query += f" ORDER BY {order_by}"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def delete(table: str, row_id: str):
    """Delete a row by id."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        await db.commit()


async def count(table: str, where: str = "", params: tuple = ()) -> int:
    """Count rows in a table."""
    query = f"SELECT COUNT(*) FROM {table}"
    if where:
        query += f" WHERE {where}"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
        return row[0] if row else 0


async def execute(sql: str, params: tuple = ()):
    """Execute raw SQL (for DELETE, etc.)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, params)
        await db.commit()


async def fetch_scalar(sql: str, params: tuple = ()):
    """Execute SQL and return a single scalar value (e.g. COUNT(*))."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        return row[0] if row else 0


async def log_activity(level: str, task: str, message: str, details: dict = None):
    """Log a processing activity."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO processing_logs (level, task, message, details) VALUES (?, ?, ?, ?)",
            (level, task, message, json.dumps(details) if details else None)
        )
        await db.commit()
