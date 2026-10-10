# qb-organizer/backend/server.py
"""FastAPI backend server for QB Organizer.

Provides REST API endpoints + Server-Sent Events (SSE) for real-time progress.
"""

import asyncio
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from datetime import datetime, timezone

from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from config import settings
from state import db as database
from claude.client import get_claude_client, ClaudeError
from knowledge.builder import ingest_textbook, process_kb_batch_results
from knowledge.graph_builder import (
    build_knowledge_graph, process_kg_batch_results,
    extract_relations_for_textbook, process_relations_batch_results,
    get_graph_stats, search_concepts, get_concept_with_relations,
    get_graph_for_subject, get_concepts_for_question, VALID_CONCEPT_TYPES,
)
from extraction.question_extractor import extract_questions_from_qp, batch_extract_qps
from matching.matcher import match_questions_to_chapters
from export.json_exporter import export_subject
from core import embedder
from answers.generator import generate_answers, regenerate_answer, PRESETS

# ── API Models ────────────────────────────────────────────────────


class GenerateRequest(BaseModel):
    """POST /api/answers/generate body."""
    mapping_ids: list[str]
    force_regenerate: bool = False


class GenerateResponse(BaseModel):
    """POST /api/answers/generate response (SSE follows over /api/progress/{task_id})."""
    task_id: str
    message: str


class PurgeRequest(BaseModel):
    """POST /api/answers/purge body."""
    subject: Optional[str] = None
    confirm: bool = False


class PurgeReportResponse(BaseModel):
    """POST /api/answers/purge response."""
    answers_deleted: int
    mappings_reset: int
    firestore_documents_deleted: int


# ── Logging Setup ─────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(settings.logs_dir / "server.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ── App Setup ─────────────────────────────────────────────────────

app = FastAPI(title="QB Organizer API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# SSE event queues for real-time progress
progress_queues: dict[str, asyncio.Queue] = {}


@app.on_event("startup")
async def startup():
    await database.init_db()
    logger.info("QB Organizer backend started")


# ── SSE Progress ──────────────────────────────────────────────────

async def send_progress(task_id: str, step: str, current: int, total: int, message: str):
    """Send progress update to SSE listeners."""
    if task_id in progress_queues:
        await progress_queues[task_id].put({
            "step": step,
            "current": current,
            "total": total,
            "percentage": round(current / total * 100, 1) if total > 0 else 0,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


@app.get("/api/progress/{task_id}")
async def progress_stream(task_id: str):
    """SSE endpoint for real-time progress."""
    queue = asyncio.Queue()
    progress_queues[task_id] = queue

    async def event_generator():
        try:
            while True:
                data = await asyncio.wait_for(queue.get(), timeout=300)
                yield {"event": "progress", "data": json.dumps(data)}
                if data.get("step") == "done":
                    break
        except asyncio.TimeoutError:
            yield {"event": "timeout", "data": json.dumps({"message": "No updates for 5 min"})}
        finally:
            progress_queues.pop(task_id, None)

    return EventSourceResponse(event_generator())


# ── Dashboard ─────────────────────────────────────────────────────

@app.get("/api/dashboard")
async def get_dashboard():
    """Get dashboard statistics."""
    textbooks = await database.fetch_all("textbooks")
    chapters = await database.fetch_all("chapters")
    qps = await database.fetch_all("question_papers")
    questions = await database.fetch_all("questions")
    mappings = await database.fetch_all("mappings")
    answers = await database.fetch_all("answers")

    # Knowledge Graph statistics
    kg_concepts = await database.count("concepts")
    kg_relations = await database.count("concept_relations")
    kg_must_know = await database.count("concepts", where="importance = ?", params=("must_know",))

    # Answers statistics
    total_answers = len(answers)
    total_questions = len(questions)
    pct_answered = round((total_answers / total_questions * 100), 1) if total_questions > 0 else 0
    
    total_bullets = sum(a.get("bullet_count", 0) for a in answers)
    avg_bullets = round(total_bullets / total_answers, 1) if total_answers > 0 else 0
    
    answer_modes = {"auto": 0, "graph_only": 0, "hybrid": 0}
    for a in answers:
        mode = a.get("retrieval_mode", "auto")
        answer_modes[mode] = answer_modes.get(mode, 0) + 1

    # Review progress
    total_mappings = len(mappings)
    count_reviewed = sum(1 for m in mappings if m.get("is_reviewed") == 1)
    count_auto_accepted = sum(1 for m in mappings if m.get("reviewer_action") == "auto_accepted")
    count_manually_reviewed = count_reviewed - count_auto_accepted
    count_pending = total_mappings - count_reviewed

    # Confidence distribution
    conf_dist = {"high": 0, "medium": 0, "low": 0}
    for m in mappings:
        level = m.get("confidence_level", "low")
        conf_dist[level] = conf_dist.get(level, 0) + 1

    # Per-subject drill-down
    subject_details = {}
    for tb in textbooks:
        sub = tb["subject"]
        subject_details.setdefault(sub, {"textbooks": 0, "chapters": 0, "qps": 0, "questions": 0, "mappings": 0, "reviewed": 0, "answered": 0, "status": tb["status"]})
        subject_details[sub]["textbooks"] += 1

    chapter_subjects = {}
    for ch in chapters:
        tb = next((t for t in textbooks if t["id"] == ch.get("textbook_id")), None)
        if tb:
            sub = tb["subject"]
            chapter_subjects[ch["id"]] = sub
            subject_details.setdefault(sub, {"textbooks": 0, "chapters": 0, "qps": 0, "questions": 0, "mappings": 0, "reviewed": 0, "answered": 0, "status": "active"})
            subject_details[sub]["chapters"] += 1

    qp_subjects = {}
    for qp in qps:
        sub = qp["subject"]
        qp_subjects[qp["id"]] = sub
        subject_details.setdefault(sub, {"textbooks": 0, "chapters": 0, "qps": 0, "questions": 0, "mappings": 0, "reviewed": 0, "answered": 0, "status": "active"})
        subject_details[sub]["qps"] += 1

    for q in questions:
        sub = qp_subjects.get(q.get("qp_id"))
        if sub:
            subject_details[sub]["questions"] += 1

    mapping_subjects = {}
    for m in mappings:
        sub = qp_subjects.get(m.get("qp_id"))
        if sub:
            mapping_subjects[m["id"]] = sub
            subject_details[sub]["mappings"] += 1
            if m.get("is_reviewed") == 1:
                subject_details[sub]["reviewed"] += 1

    for a in answers:
        sub = mapping_subjects.get(a.get("mapping_id"))
        if sub:
            subject_details[sub]["answered"] += 1

    # Legacy subjects format for backwards compatibility
    subjects_summary = []
    for k, v in subject_details.items():
        subjects_summary.append({
            "name": k,
            "textbooks": v["textbooks"],
            "chapters": v["chapters"],
            "qps": v["qps"],
            "questions": v["questions"],
            "status": v["status"]
        })

    # Cost summary
    cost_summary = {"total_spent": 0, "budget_limit": settings.budget_limit, "breakdown": {}, "api_calls_made": 0}
    try:
        client = get_claude_client()
        cost_summary = await client.cost_tracker.get_summary()
    except Exception:
        pass

    # Recent activity
    logs = await database.fetch_all("processing_logs", order_by="id DESC")
    recent = [{"message": l["message"], "task": l["task"], "timestamp": l["timestamp"], "level": l["level"]} for l in logs[:10]]

    return {
        "total_textbooks": len(textbooks),
        "total_chapters": len(chapters),
        "total_chunks": await database.count("chunks"),
        "total_qps": len(qps),
        "total_questions": total_questions,
        "total_matched": total_mappings,
        "confidence_distribution": conf_dist,
        "subjects": subjects_summary,
        "subject_details": subject_details,
        "cost": cost_summary,
        "recent_activity": recent,
        "kg_stats": {
            "concepts": kg_concepts,
            "relations": kg_relations,
            "must_know": kg_must_know
        },
        "answer_stats": {
            "total": total_answers,
            "pct_answered": pct_answered,
            "avg_bullets": avg_bullets,
            "modes": answer_modes
        },
        "review_progress": {
            "total": total_mappings,
            "reviewed": count_reviewed,
            "pending": count_pending,
            "auto_accepted": count_auto_accepted,
            "manually_reviewed": count_manually_reviewed
        }
    }


# ── Textbook Management ──────────────────────────────────────────

@app.post("/api/textbooks/upload")
async def upload_textbook(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    name: str = Form(...),
    subject: str = Form(...),
):
    """Upload and ingest a textbook PDF."""
    task_id = str(uuid.uuid4())[:8]

    # Save uploaded file
    save_path = settings.textbooks_dir / file.filename
    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)

    async def process():
        try:
            async def progress_cb(step, current, total, message):
                await send_progress(task_id, step, current, total, message)

            result = await ingest_textbook(
                file_path=str(save_path),
                name=name,
                subject=subject,
                progress_callback=progress_cb,
            )
            await send_progress(task_id, "done", 1, 1, json.dumps(result))
        except Exception as e:
            await send_progress(task_id, "error", 0, 0, str(e))

    background_tasks.add_task(process)

    return {"task_id": task_id, "message": "Upload started. Track progress via SSE."}


@app.get("/api/textbooks")
async def list_textbooks():
    """List all textbooks."""
    return await database.fetch_all("textbooks", order_by="created_at DESC")


@app.get("/api/textbooks/{textbook_id}")
async def get_textbook(textbook_id: str):
    """Get textbook details with chapters."""
    textbook = await database.fetch_one("textbooks", textbook_id)
    if not textbook:
        raise HTTPException(404, "Textbook not found")

    chapters = await database.fetch_all(
        "chapters", "textbook_id = ?", (textbook_id,), "chapter_number ASC"
    )

    # Parse JSON fields
    for ch in chapters:
        for field in ["topics", "key_terms", "exam_likely_topics"]:
            if ch.get(field) and isinstance(ch[field], str):
                try:
                    ch[field] = json.loads(ch[field])
                except json.JSONDecodeError:
                    ch[field] = []

    return {**textbook, "chapters": chapters}


@app.post("/api/textbooks/{textbook_id}/check-batch")
async def check_textbook_batch(textbook_id: str):
    """Check and process KB + KG batch results for a textbook."""
    from state.checkpoint import Checkpoint
    textbook = await database.fetch_one("textbooks", textbook_id)
    if not textbook:
        raise HTTPException(404, "Textbook not found")

    # Find the KB batch ID from checkpoint
    # Try the standard naming pattern: ingest_textbook_{subject}_{id}
    subject = textbook["subject"]
    checkpoint = Checkpoint("ingest_textbook", f"{subject}_{textbook_id}")
    batch_id = checkpoint.get_meta("kb_batch_id")

    if not batch_id:
        # Fallback: search checkpoint files by glob
        cp_files = list(settings.checkpoints_dir.glob(f"ingest_textbook_*_{textbook_id}*.json"))
        if cp_files:
            with open(cp_files[0], "r") as f:
                cp_data = json.load(f)
            batch_id = cp_data.get("metadata", {}).get("kb_batch_id")

    if not batch_id:
        raise HTTPException(404, "No batch ID found. The textbook may not have been processed yet.")

    result = await process_kb_batch_results(textbook_id, batch_id)
    return result


# ── Question Paper Management ────────────────────────────────────

@app.post("/api/papers/upload")
async def upload_question_paper(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    subject: str = Form(...),
    university: str = Form(None),
    year: int = Form(None),
    month: str = Form(None),
    schema: str = Form(None),
):
    """Upload and extract questions from a single QP."""
    task_id = str(uuid.uuid4())[:8]

    save_path = settings.qp_dir / file.filename
    with open(save_path, "wb") as f:
        content = await file.read()
        f.write(content)

    async def process():
        try:
            async def progress_cb(step, current, total, message):
                await send_progress(task_id, step, current, total, message)

            result = await extract_questions_from_qp(
                file_path=str(save_path),
                subject=subject,
                metadata_overrides={"university": university, "year": year, "month": month, "schema": schema},
                progress_callback=progress_cb,
            )
            await send_progress(task_id, "done", 1, 1, json.dumps(result, default=str))
        except Exception as e:
            await send_progress(task_id, "error", 0, 0, str(e))

    background_tasks.add_task(process)
    return {"task_id": task_id, "message": "QP processing started."}


@app.post("/api/papers/upload-batch")
async def upload_batch_qps(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    subject: str = Form(...),
):
    """Upload multiple QPs for batch processing."""
    saved_paths = []
    for file in files:
        save_path = settings.qp_dir / file.filename
        with open(save_path, "wb") as f:
            content = await file.read()
            f.write(content)
        saved_paths.append(str(save_path))

    task_id = str(uuid.uuid4())[:8]

    async def process():
        try:
            result = await batch_extract_qps(saved_paths, subject)
            await send_progress(task_id, "done", 1, 1, json.dumps(result, default=str))
        except Exception as e:
            await send_progress(task_id, "error", 0, 0, str(e))

    background_tasks.add_task(process)
    return {"task_id": task_id, "count": len(saved_paths), "message": "Batch QP upload started."}


@app.get("/api/papers")
async def list_papers(subject: str = None):
    """List question papers."""
    if subject:
        return await database.fetch_all("question_papers", "subject = ?", (subject,), "year DESC")
    return await database.fetch_all("question_papers", order_by="created_at DESC")


@app.get("/api/papers/{qp_id}/questions")
async def get_qp_questions(qp_id: str):
    """Get all questions from a specific QP."""
    questions = await database.fetch_all("questions", "qp_id = ?", (qp_id,))
    for q in questions:
        if q.get("sub_parts") and isinstance(q["sub_parts"], str):
            try:
                q["sub_parts"] = json.loads(q["sub_parts"])
            except json.JSONDecodeError:
                q["sub_parts"] = []
    return questions


# ── Matching ──────────────────────────────────────────────────────

@app.post("/api/match")
async def run_matching(
    background_tasks: BackgroundTasks,
    subject: str = Form(...),
    textbook_id: str = Form(...),
    paper_ids: str = Form(None),
):
    """Run the 3-layer matching engine."""
    task_id = str(uuid.uuid4())[:8]
    selected_paper_ids = [p.strip() for p in paper_ids.split(",") if p.strip()] if paper_ids else None

    async def process():
        try:
            async def progress_cb(step, current, total, message):
                await send_progress(task_id, step, current, total, message)

            result = await match_questions_to_chapters(
                subject=subject,
                textbook_id=textbook_id,
                progress_callback=progress_cb,
                paper_ids=selected_paper_ids,
            )
            await send_progress(task_id, "done", 1, 1, json.dumps(result))
        except Exception as e:
            await send_progress(task_id, "error", 0, 0, str(e))

    background_tasks.add_task(process)
    return {"task_id": task_id, "message": "Matching started."}


@app.get("/api/mappings")
async def list_mappings(subject: str = None, confidence_level: str = None):
    """List question mappings with optional filters."""
    where_parts = []
    params = []

    if subject:
        where_parts.append("qp_id IN (SELECT id FROM question_papers WHERE subject = ?)")
        params.append(subject)
    if confidence_level:
        where_parts.append("confidence_level = ?")
        params.append(confidence_level)

    where = " AND ".join(where_parts) if where_parts else ""
    mappings = await database.fetch_all("mappings", where, tuple(params), "confidence DESC")

    # Fetch mapping IDs that have answers to populate has_answer field
    answered_mapping_ids = set()
    try:
        ans_rows = await database.fetch_all("answers")
        for row in ans_rows:
            if row.get("mapping_id"):
                answered_mapping_ids.add(row["mapping_id"])
    except Exception:
        pass

    # Parse JSON fields
    for m in mappings:
        for field in ["matched_chapters", "best_match", "appears_in_exams"]:
            if m.get(field) and isinstance(m[field], str):
                try:
                    m[field] = json.loads(m[field])
                except json.JSONDecodeError:
                    pass
        # SQLite stores booleans as 0/1 — coerce to real booleans
        m["is_reviewed"] = bool(m.get("is_reviewed"))
        m["is_multi_chapter"] = bool(m.get("is_multi_chapter"))
        m["has_answer"] = m["id"] in answered_mapping_ids
    return mappings


@app.put("/api/mappings/{mapping_id}/review")
async def review_mapping(mapping_id: str, data: dict):
    """Submit a review decision for a mapping.

    Supports:
      - action: "accepted" | "reassigned" | "rejected"
      - chapter_id: single chapter ID (backward compat)
      - chapter_ids: list of chapter IDs (multi-chapter assignment)
    """
    mapping = await database.fetch_one("mappings", mapping_id)
    if not mapping:
        raise HTTPException(404, "Mapping not found")

    update_data = {
        "is_reviewed": True,
        "reviewer_action": data.get("action", "accepted"),
    }

    # Multi-chapter assignment
    chapter_ids = data.get("chapter_ids") or []
    single_id = data.get("chapter_id")
    if single_id and not chapter_ids:
        chapter_ids = [single_id]

    if data.get("action") == "reassigned" and chapter_ids:
        chapters_matched = []
        for cid in chapter_ids:
            ch = await database.fetch_one("chapters", cid)
            if ch:
                chapters_matched.append({
                    "chapter_id": cid,
                    "chapter_name": ch["name"],
                    "reviewer_assigned": True,
                })

        if chapters_matched:
            primary = chapters_matched[0]
            update_data["final_chapter_id"] = primary["chapter_id"]
            update_data["final_chapter_name"] = primary["chapter_name"]
            update_data["is_multi_chapter"] = len(chapters_matched) > 1
            update_data["matched_chapters"] = chapters_matched

    elif data.get("action") == "accepted":
        # Accept top suggestion as final
        best = mapping.get("best_match")
        if best:
            if isinstance(best, str):
                try:
                    best = json.loads(best)
                except json.JSONDecodeError:
                    best = {}
            if best.get("chapter_id") and not mapping.get("final_chapter_id"):
                update_data["final_chapter_id"] = best["chapter_id"]
                update_data["final_chapter_name"] = best.get("chapter_name", "")

    await database.update("mappings", mapping_id, update_data)
    return {"status": "ok", "message": "Review saved."}


# ── Export ────────────────────────────────────────────────────────

@app.post("/api/export")
async def do_export(subject: str = Form(...), university_id: str = Form("")):
    """Export organized data as Firestore-ready JSON."""
    result = await export_subject(subject, university_id)
    return result


@app.get("/api/export/download/{subject}")
async def download_export(subject: str):
    """Download the export JSON file."""
    filename = f"{subject.lower().replace(' ', '_')}_export.json"
    filepath = settings.exports_dir / filename
    if not filepath.exists():
        raise HTTPException(404, "Export file not found. Run export first.")
    return FileResponse(str(filepath), filename=filename, media_type="application/json")


# ── Settings & Status ────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    """Get system status."""
    try:
        client = get_claude_client()
        claude_status = await client.get_status()
    except Exception as e:
        claude_status = {"error": str(e)}

    return {
        "backend": "running",
        "database": "connected",
        "claude": claude_status,
        "settings": {
            "confidence_high": settings.confidence_high,
            "confidence_low": settings.confidence_low,
            "budget_limit": settings.budget_limit,
        },
    }


@app.get("/api/chapters")
async def list_chapters(subject: str = None, textbook_id: str = None):
    """List chapters with optional filters."""
    if textbook_id:
        chapters = await database.fetch_all("chapters", "textbook_id = ?", (textbook_id,), "chapter_number ASC")
    elif subject:
        chapters = await database.fetch_all(
            "chapters",
            "textbook_id IN (SELECT id FROM textbooks WHERE subject = ?)",
            (subject,),
            "chapter_number ASC"
        )
    else:
        chapters = await database.fetch_all("chapters", order_by="chapter_number ASC")

    for ch in chapters:
        for field in ["topics", "key_terms", "exam_likely_topics"]:
            if ch.get(field) and isinstance(ch[field], str):
                try:
                    ch[field] = json.loads(ch[field])
                except json.JSONDecodeError:
                    ch[field] = []
    return chapters


@app.get("/api/logs")
async def get_logs(limit: int = 50):
    """Get recent processing logs."""
    return await database.fetch_all("processing_logs", order_by="id DESC")


# ── Settings API ──────────────────────────────────────────────────

def _read_env_file() -> dict:
    """Parse .env file into a dict."""
    env_path = Path(__file__).parent / ".env"
    data = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                data[key.strip()] = val.strip()
    return data


def _write_env_file(data: dict):
    """Write settings back to .env, preserving comments and structure."""
    env_path = Path(__file__).parent / ".env"
    lines = []
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in data:
                    lines.append(f"{key}={data[key]}")
                    del data[key]
                    continue
            lines.append(line)
    # Append any new keys
    for key, val in data.items():
        lines.append(f"{key}={val}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.get("/api/settings")
async def get_settings():
    """Get current application settings."""
    env = _read_env_file()
    # Mask API key
    api_key = env.get("ANTHROPIC_API_KEY", "")
    masked_key = f"{api_key[:12]}...{api_key[-4:]}" if len(api_key) > 20 else "not set"

    try:
        client = get_claude_client()
        cost_info = await client.cost_tracker.get_summary()
    except Exception:
        cost_info = {"total_spent": 0, "budget_limit": 25, "budget_remaining": 25, "breakdown": {}}

    return {
        "api_key_masked": masked_key,
        "api_key_set": len(api_key) > 10,
        "haiku_model": env.get("HAIKU_MODEL", settings.haiku_model),
        "sonnet_model": env.get("SONNET_MODEL", settings.sonnet_model),
        "budget_limit": float(env.get("BUDGET_LIMIT", settings.budget_limit)),
        "batch_cost_ceiling_usd": float(env.get("BATCH_COST_CEILING_USD", settings.batch_cost_ceiling_usd)),
        "daily_cost_ceiling_usd": float(env.get("DAILY_COST_CEILING_USD", settings.daily_cost_ceiling_usd)),
        "confidence_high": float(env.get("CONFIDENCE_HIGH", settings.confidence_high)),
        "confidence_low": float(env.get("CONFIDENCE_LOW", settings.confidence_low)),
        "chunk_size": int(env.get("CHUNK_SIZE", settings.chunk_size)),
        "chunk_overlap": int(env.get("CHUNK_OVERLAP", settings.chunk_overlap)),
        "embedding_model": env.get("EMBEDDING_MODEL", settings.embedding_model),
        "backend_port": int(env.get("BACKEND_PORT", settings.backend_port)),
        "frontend_url": env.get("FRONTEND_URL", settings.frontend_url),
        
        # New Settings Fields
        "default_answer_mode": env.get("DEFAULT_ANSWER_MODE", settings.default_answer_mode),
        "default_answer_preset": env.get("DEFAULT_ANSWER_PRESET", settings.default_answer_preset),
        "answer_temperature": float(env.get("ANSWER_TEMPERATURE", settings.answer_temperature)),
        "kg_extraction_model": env.get("KG_EXTRACTION_MODEL", settings.kg_extraction_model),
        "kg_max_concepts_per_batch": int(env.get("KG_MAX_CONCEPTS_PER_BATCH", settings.kg_max_concepts_per_batch)),
        "kg_enable_relation_extraction": env.get("KG_ENABLE_RELATION_EXTRACTION", str(settings.kg_enable_relation_extraction)).lower() == "true",
        "kg_default_limit": int(env.get("KG_DEFAULT_LIMIT", settings.kg_default_limit)),
        "fs_collection_prefix": env.get("FS_COLLECTION_PREFIX", settings.fs_collection_prefix),
        "imagekit_folder": env.get("IMAGEKIT_FOLDER", settings.imagekit_folder),
        
        "cost": cost_info,
    }


@app.put("/api/settings")
async def update_settings(data: dict):
    """Update settings and persist to .env file."""
    env = _read_env_file()

    # Map frontend keys to .env keys
    field_map = {
        "api_key": "ANTHROPIC_API_KEY",
        "haiku_model": "HAIKU_MODEL",
        "sonnet_model": "SONNET_MODEL",
        "budget_limit": "BUDGET_LIMIT",
        "batch_cost_ceiling_usd": "BATCH_COST_CEILING_USD",
        "daily_cost_ceiling_usd": "DAILY_COST_CEILING_USD",
        "confidence_high": "CONFIDENCE_HIGH",
        "confidence_low": "CONFIDENCE_LOW",
        "chunk_size": "CHUNK_SIZE",
        "chunk_overlap": "CHUNK_OVERLAP",
        "embedding_model": "EMBEDDING_MODEL",
        "backend_port": "BACKEND_PORT",
        "frontend_url": "FRONTEND_URL",
        
        # New Settings Fields
        "default_answer_mode": "DEFAULT_ANSWER_MODE",
        "default_answer_preset": "DEFAULT_ANSWER_PRESET",
        "answer_temperature": "ANSWER_TEMPERATURE",
        "kg_extraction_model": "KG_EXTRACTION_MODEL",
        "kg_max_concepts_per_batch": "KG_MAX_CONCEPTS_PER_BATCH",
        "kg_enable_relation_extraction": "KG_ENABLE_RELATION_EXTRACTION",
        "kg_default_limit": "KG_DEFAULT_LIMIT",
        "fs_collection_prefix": "FS_COLLECTION_PREFIX",
        "imagekit_folder": "IMAGEKIT_FOLDER",
    }

    changed = []
    for frontend_key, env_key in field_map.items():
        if frontend_key in data:
            new_val = str(data[frontend_key])
            # Don't overwrite API key with masked value
            if frontend_key == "api_key" and ("..." in new_val or len(new_val) < 10):
                continue
            old_val = env.get(env_key, "")
            if new_val != old_val:
                env[env_key] = new_val
                changed.append(frontend_key)

    if changed:
        _write_env_file(env)
        logger.info(f"Settings updated: {', '.join(changed)} → .env saved")
        # Hot-reload numeric settings into the running config
        if "confidence_high" in changed:
            settings.confidence_high = float(data["confidence_high"])
        if "confidence_low" in changed:
            settings.confidence_low = float(data["confidence_low"])
        if "budget_limit" in changed:
            settings.budget_limit = float(data["budget_limit"])
        if "batch_cost_ceiling_usd" in changed:
            settings.batch_cost_ceiling_usd = float(data["batch_cost_ceiling_usd"])
        if "daily_cost_ceiling_usd" in changed:
            settings.daily_cost_ceiling_usd = float(data["daily_cost_ceiling_usd"])
        if "chunk_size" in changed:
            settings.chunk_size = int(data["chunk_size"])
        if "chunk_overlap" in changed:
            settings.chunk_overlap = int(data["chunk_overlap"])
            
        # Hot-reload new configuration fields
        if "default_answer_mode" in changed:
            settings.default_answer_mode = data["default_answer_mode"]
        if "default_answer_preset" in changed:
            settings.default_answer_preset = data["default_answer_preset"]
        if "answer_temperature" in changed:
            settings.answer_temperature = float(data["answer_temperature"])
        if "kg_extraction_model" in changed:
            settings.kg_extraction_model = data["kg_extraction_model"]
        if "kg_max_concepts_per_batch" in changed:
            settings.kg_max_concepts_per_batch = int(data["kg_max_concepts_per_batch"])
        if "kg_enable_relation_extraction" in changed:
            settings.kg_enable_relation_extraction = str(data["kg_enable_relation_extraction"]).lower() == "true"
        if "kg_default_limit" in changed:
            settings.kg_default_limit = int(data["kg_default_limit"])
        if "fs_collection_prefix" in changed:
            settings.fs_collection_prefix = data["fs_collection_prefix"]
        if "imagekit_folder" in changed:
            settings.imagekit_folder = data["imagekit_folder"]

    return {"status": "ok", "changed": changed, "message": f"Updated {len(changed)} setting(s), saved to .env"}


@app.get("/api/settings/sysinfo")
async def api_settings_sysinfo():
    """Get system stats for settings page."""
    import sys
    import shutil
    import os
    import importlib.metadata

    packages = ["fastapi", "anthropic", "aiosqlite", "chromadb", "pydantic", "firebase-admin"]
    versions = {}
    for pkg in packages:
        try:
            versions[pkg] = importlib.metadata.version(pkg)
        except Exception:
            versions[pkg] = "not installed"

    db_size_mb = 0
    if os.path.exists(settings.db_path):
        db_size_mb = os.path.getsize(settings.db_path) / (1024 * 1024)

    total, used, free = shutil.disk_usage(str(settings.data_dir))
    disk_stats = {
        "total_gb": round(total / (1024**3), 1),
        "used_gb": round(used / (1024**3), 1),
        "free_gb": round(free / (1024**3), 1),
        "pct_used": round((used / total) * 100, 1)
    }

    chunk_count = await database.count("chunks")

    return {
        "python_version": sys.version.split()[0],
        "package_versions": versions,
        "database_size_mb": round(db_size_mb, 2),
        "disk_usage": disk_stats,
        "vector_chunks": chunk_count,
    }


# ── Textbook Chapter Details ──────────────────────────────────────

@app.get("/api/textbooks/{textbook_id}/chapters")
async def get_textbook_chapters(textbook_id: str):
    """Get rich chapter details for a textbook."""
    textbook = await database.fetch_one("textbooks", textbook_id)
    if not textbook:
        raise HTTPException(404, "Textbook not found")

    chapters = await database.fetch_all("chapters", "textbook_id = ?", (textbook_id,), "chapter_number ASC")

    # Enrich with parsed fields and chunk counts
    enriched = []
    for ch in chapters:
        for field in ["topics", "key_terms", "exam_likely_topics"]:
            if ch.get(field) and isinstance(ch[field], str):
                try:
                    ch[field] = json.loads(ch[field])
                except json.JSONDecodeError:
                    ch[field] = []

        # Get chunk count from DB
        chunk_rows = await database.fetch_all("chunks", "chapter_id = ?", (ch["id"],))
        ch["chunk_count"] = len(chunk_rows)

        enriched.append(ch)

    return {"textbook": textbook, "chapters": enriched}


# ── Subjects list ─────────────────────────────────────────────────

@app.get("/api/subjects")
async def list_subjects():
    """Get unique subjects from processed textbooks."""
    textbooks = await database.fetch_all("textbooks")
    subjects = {}
    for tb in textbooks:
        subj = tb.get("subject", "Unknown")
        if subj not in subjects:
            subjects[subj] = {"subject": subj, "textbook_count": 0, "textbook_ids": []}
        subjects[subj]["textbook_count"] += 1
        subjects[subj]["textbook_ids"].append(tb["id"])
    return list(subjects.values())


# ── Delete Operations ─────────────────────────────────────────────

@app.delete("/api/textbooks/{textbook_id}")
async def delete_textbook(textbook_id: str):
    """Delete a textbook and all associated data (chapters, chunks, embeddings)."""
    textbook = await database.fetch_one("textbooks", textbook_id)
    if not textbook:
        raise HTTPException(404, "Textbook not found")

    # Delete chunks, chapters, textbook
    await database.execute("DELETE FROM chunks WHERE textbook_id = ?", (textbook_id,))
    await database.execute("DELETE FROM chapters WHERE textbook_id = ?", (textbook_id,))
    await database.execute("DELETE FROM textbooks WHERE id = ?", (textbook_id,))

    # Delete the PDF file
    pdf_path = settings.textbooks_dir / textbook["filename"]
    if pdf_path.exists():
        pdf_path.unlink()

    # Clean ChromaDB vectors
    try:
        collection = embedder.get_or_create_collection(textbook["subject"])
        results = collection.get(where={"textbook_id": textbook_id}, include=[])
        if results and results["ids"]:
            collection.delete(ids=results["ids"])
            logger.info(f"Deleted {len(results['ids'])} vectors from ChromaDB")
    except Exception as e:
        logger.warning(f"ChromaDB cleanup: {e}")

    # Delete checkpoints
    for cp in settings.checkpoints_dir.glob(f"*{textbook_id}*"):
        cp.unlink(missing_ok=True)

    logger.info(f"Deleted textbook: {textbook['name']} ({textbook_id})")
    return {"status": "ok", "message": f"Deleted textbook '{textbook['name']}' and all associated data."}


@app.delete("/api/papers/{qp_id}")
async def delete_paper(qp_id: str):
    """Delete a question paper and its extracted questions."""
    qp = await database.fetch_one("question_papers", qp_id)
    if not qp:
        raise HTTPException(404, "Question paper not found")

    await database.execute("DELETE FROM mappings WHERE qp_id = ?", (qp_id,))
    await database.execute("DELETE FROM questions WHERE qp_id = ?", (qp_id,))
    await database.execute("DELETE FROM question_papers WHERE id = ?", (qp_id,))

    pdf_path = settings.qp_dir / qp["filename"]
    if pdf_path.exists():
        pdf_path.unlink()

    logger.info(f"Deleted QP: {qp['filename']} ({qp_id})")
    return {"status": "ok", "message": f"Deleted '{qp['filename']}' and questions."}


@app.delete("/api/mappings/{mapping_id}")
async def delete_mapping(mapping_id: str):
    """Delete a single mapping."""
    await database.execute("DELETE FROM mappings WHERE id = ?", (mapping_id,))
    return {"status": "ok", "message": "Mapping deleted."}


@app.delete("/api/mappings")
async def delete_all_mappings(subject: str = None):
    """Delete all mappings, optionally by subject."""
    if subject:
        await database.execute(
            "DELETE FROM mappings WHERE qp_id IN (SELECT id FROM question_papers WHERE subject = ?)",
            (subject,))
    else:
        await database.execute("DELETE FROM mappings", ())
    return {"status": "ok", "message": "Mappings deleted."}


@app.post("/api/reset")
async def full_reset():
    """Nuclear option: delete ALL data and start fresh."""
    for table in ["answers", "mappings", "questions", "question_papers", "chunks", "chapters",
                   "textbooks", "api_costs", "processing_logs", "retry_queue"]:
        try:
            await database.execute(f"DELETE FROM {table}", ())
        except Exception:
            pass

    # Wipe ChromaDB
    try:
        import chromadb
        from chromadb.config import Settings as ChromaSettings
        client = chromadb.PersistentClient(
            path=str(settings.chroma_dir),
            settings=ChromaSettings(anonymized_telemetry=False))
        for col in client.list_collections():
            client.delete_collection(col.name)
    except Exception as e:
        logger.warning(f"ChromaDB cleanup: {e}")

    # Wipe data files
    for d in [settings.textbooks_dir, settings.qp_dir, settings.exports_dir, settings.checkpoints_dir]:
        if d.exists():
            for f in d.iterdir():
                if f.is_file():
                    f.unlink(missing_ok=True)

    logger.info("Full data reset completed")
    return {"status": "ok", "message": "All data wiped. Ready for fresh start."}


# ── Answer Generation ─────────────────────────────────────────────

@app.get("/api/answers/presets")
async def get_answer_presets():
    """Return available answer presets."""
    return {
        "presets": {
            "LAQ": {**PRESETS["LAQ"], "description": "15-20 detailed bullet points"},
            "SAQ": {**PRESETS["SAQ"], "description": "8-12 detailed bullet points"},
            "VSAQ": {**PRESETS["VSAQ"], "description": "7-8 precise bullet points"},
        },
        "styles": ["detailed", "precise"],
        "custom_range": {"min": 3, "max": 25},
    }


@app.post("/api/answers/generate")
async def api_generate_answers(
    background_tasks: BackgroundTasks,
    mapping_ids: str = Form(...),
    preset: str = Form("custom"),
    custom_bullet_count: int = Form(None),
    custom_style: str = Form(None),
    mode: str = Form("auto"),
):
    """Generate answers for selected questions (max 5).

    mode: 'auto' (merged vector+graph), 'graph_only' (pure GraphRAG), 'hybrid' (dual-path + fusion)
    """
    ids = [mid.strip() for mid in mapping_ids.split(",") if mid.strip()]
    if not ids:
        raise HTTPException(400, "No mapping IDs provided")
    if len(ids) > 5:
        raise HTTPException(400, "Maximum 5 questions per batch")
    if mode not in ("auto", "graph_only", "hybrid"):
        mode = "auto"

    task_id = f"ans_{uuid.uuid4().hex[:8]}"

    async def run_generation():
        # Small delay to let SSE connect
        await asyncio.sleep(0.5)
        try:
            async def progress_cb(step, current, total, message):
                await send_progress(task_id, step, current, total, message)

            result = await generate_answers(
                ids,
                preset=preset,
                custom_bullet_count=custom_bullet_count,
                custom_style=custom_style,
                mode=mode,
                progress_callback=progress_cb,
            )
            await send_progress(task_id, "done", 1, 1, json.dumps(result))
        except Exception as e:
            logger.exception("Answer generation failed")
            await send_progress(task_id, "error", 0, 0, str(e))

    background_tasks.add_task(run_generation)
    return {"task_id": task_id, "status": "started", "count": len(ids), "mode": mode}


@app.get("/api/answers")
async def get_answers(
    subject: str = None,
    chapter_id: str = None,
    status: str = None,
):
    """List all generated answers with optional filters."""
    conditions = []
    params = []
    if subject:
        conditions.append(
            "mapping_id IN (SELECT id FROM mappings WHERE qp_id IN "
            "(SELECT id FROM question_papers WHERE subject = ?))"
        )
        params.append(subject)
    if chapter_id:
        conditions.append("chapter_id = ?")
        params.append(chapter_id)
    if status:
        conditions.append("status = ?")
        params.append(status)

    where = " AND ".join(conditions) if conditions else None
    rows = await database.fetch_all("answers", where, tuple(params) if params else None, "generated_at DESC")

    answers = []
    for r in rows:
        meta_raw = r.get("retrieval_metadata")
        retrieval_metadata = None
        if meta_raw and isinstance(meta_raw, str):
            try:
                retrieval_metadata = json.loads(meta_raw)
            except json.JSONDecodeError:
                retrieval_metadata = None
        answers.append({
            "id": r["id"],
            "mapping_id": r["mapping_id"],
            "question_text": r["question_text"],
            "chapter_name": r.get("chapter_name", ""),
            "prologue": r.get("prologue", ""),
            "bullets": json.loads(r.get("bullets", "[]")) if r.get("bullets") else [],
            "epilogue": r.get("epilogue", ""),
            "bullet_count": r.get("bullet_count", 0),
            "bullet_style": r.get("bullet_style", "detailed"),
            "preset": r.get("preset", "custom"),
            "source_pages": json.loads(r.get("source_pages", "{}")) if r.get("source_pages") else {},
            "images": json.loads(r.get("images", "[]")) if r.get("images") else [],
            "textbook_name": r.get("textbook_name", ""),
            "generated_at": r.get("generated_at", ""),
            "model_used": r.get("model_used", ""),
            "status": r.get("status", "generated"),
            "retrieval_mode": r.get("retrieval_mode", "auto"),
            "retrieval_metadata": retrieval_metadata,
        })
    return answers


@app.get("/api/answers/stats")
async def get_answer_stats():
    """Get answer generation statistics."""
    total = await database.fetch_all("answers", None, None)
    answered_mappings = set(r["mapping_id"] for r in total)
    all_mappings = await database.fetch_all("mappings", "is_reviewed = 1 OR confidence_level = 'high'", None)

    return {
        "total_answers": len(total),
        "total_eligible": len(all_mappings),
        "answered_count": len(answered_mappings),
        "unanswered_count": len(all_mappings) - len(answered_mappings),
        "by_preset": {
            "LAQ": len([r for r in total if r.get("preset") == "LAQ"]),
            "SAQ": len([r for r in total if r.get("preset") == "SAQ"]),
            "VSAQ": len([r for r in total if r.get("preset") == "VSAQ"]),
            "custom": len([r for r in total if r.get("preset") == "custom"]),
        },
        "by_status": {
            "generated": len([r for r in total if r.get("status") == "generated"]),
            "edited": len([r for r in total if r.get("status") == "edited"]),
            "approved": len([r for r in total if r.get("status") == "approved"]),
        },
    }


@app.get("/api/answers/{mapping_id}")
async def get_answer_for_mapping(mapping_id: str):
    """Get answer for a specific mapping."""
    rows = await database.fetch_all("answers", "mapping_id = ?", (mapping_id,))
    if not rows:
        return None
    r = rows[0]
    meta_raw = r.get("retrieval_metadata")
    retrieval_metadata = None
    if meta_raw and isinstance(meta_raw, str):
        try:
            retrieval_metadata = json.loads(meta_raw)
        except json.JSONDecodeError:
            retrieval_metadata = None
    return {
        "id": r["id"],
        "mapping_id": r["mapping_id"],
        "question_text": r["question_text"],
        "chapter_name": r.get("chapter_name", ""),
        "prologue": r.get("prologue", ""),
        "bullets": json.loads(r.get("bullets", "[]")) if r.get("bullets") else [],
        "epilogue": r.get("epilogue", ""),
        "bullet_count": r.get("bullet_count", 0),
        "bullet_style": r.get("bullet_style", "detailed"),
        "preset": r.get("preset", "custom"),
        "source_pages": json.loads(r.get("source_pages", "{}")) if r.get("source_pages") else {},
        "images": json.loads(r.get("images", "[]")) if r.get("images") else [],
        "textbook_name": r.get("textbook_name", ""),
        "generated_at": r.get("generated_at", ""),
        "status": r.get("status", "generated"),
        "retrieval_mode": r.get("retrieval_mode", "auto"),
        "retrieval_metadata": retrieval_metadata,
    }


@app.put("/api/answers/{answer_id}")
async def update_answer(answer_id: str, body: dict = None):
    """Edit an answer (manual correction)."""
    if not body:
        raise HTTPException(400, "Request body required")

    answer = await database.fetch_one("answers", answer_id)
    if not answer:
        raise HTTPException(404, "Answer not found")

    updates = {}
    if "prologue" in body:
        updates["prologue"] = body["prologue"]
    if "bullets" in body:
        updates["bullets"] = json.dumps(body["bullets"])
        updates["bullet_count"] = len(body["bullets"])
    if "epilogue" in body:
        updates["epilogue"] = body["epilogue"]
    if updates:
        updates["status"] = "edited"
        await database.update("answers", answer_id, updates)

    return {"status": "ok", "updated": list(updates.keys())}


@app.delete("/api/answers/{answer_id}")
async def delete_answer(answer_id: str):
    """Delete a generated answer."""
    await database.execute("DELETE FROM answers WHERE id = ?", (answer_id,))
    return {"status": "ok"}


@app.post("/api/answers/{answer_id}/regenerate")
async def api_regenerate_answer(
    answer_id: str,
    background_tasks: BackgroundTasks,
    preset: str = Form(None),
    custom_bullet_count: int = Form(None),
    custom_style: str = Form(None),
):
    """Regenerate a single answer with different parameters."""
    answer = await database.fetch_one("answers", answer_id)
    if not answer:
        raise HTTPException(404, "Answer not found")

    task_id = f"regen_{uuid.uuid4().hex[:8]}"

    async def run():
        await asyncio.sleep(0.5)
        try:
            result = await regenerate_answer(
                answer_id,
                preset=preset,
                custom_bullet_count=custom_bullet_count,
                custom_style=custom_style,
            )
            await send_progress(task_id, "done", 1, 1, json.dumps(result))
        except Exception as e:
            await send_progress(task_id, "error", 0, 0, str(e))

    background_tasks.add_task(run)
    return {"task_id": task_id}



# ── Image Serving ─────────────────────────────────────────────────

@app.get("/api/images/{textbook_id}/{filename}")
async def serve_image(textbook_id: str, filename: str):
    """Serve extracted textbook images."""
    image_path = settings.data_dir / "images" / textbook_id / filename
    if not image_path.exists():
        raise HTTPException(404, "Image not found")
    return FileResponse(str(image_path))


# ── QB Firestore Push ─────────────────────────────────────────────

from qb.firestore_pusher import push_qb_to_firestore


@app.post("/api/qb/push-to-firestore")
async def api_qb_push_firestore(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Push question bank questions + answers to Firestore with ImageKit image upload."""
    body = await request.json()
    subject = body.get("subject")
    mapping_ids = body.get("mapping_ids")  # Optional: specific IDs
    upload_images = body.get("upload_images", True)
    dry_run = body.get("dry_run", False)

    if not subject:
        raise HTTPException(400, "subject is required")

    task_id = f"qb_push_{uuid.uuid4().hex[:8]}"

    async def process():
        await asyncio.sleep(0.5)
        try:
            async def progress_cb(stage, current, total, msg):
                await send_progress(task_id, stage, current, total, msg)

            result = await push_qb_to_firestore(
                subject=subject,
                mapping_ids=mapping_ids,
                upload_images=upload_images,
                progress_callback=progress_cb,
                dry_run=dry_run,
            )
            await send_progress(task_id, "done", 1, 1, json.dumps(result))
        except Exception as e:
            logger.exception("QB push failed")
            await send_progress(task_id, "error", 0, 0, str(e))

    background_tasks.add_task(process)
    return {"task_id": task_id, "message": "QB push started."}


@app.get("/api/qb/firestore-status")
async def api_qb_firestore_status():
    """Check if Firestore is connected for QB push."""
    try:
        from qb.firestore_pusher import _get_firestore
        fs = _get_firestore()
        # Quick read to verify connection
        fs.collection("subjects").limit(1).get()
        return {"connected": True}
    except Exception as e:
        return {"connected": False, "error": str(e)}


@app.get("/api/qb/push-stats")
async def api_qb_push_stats():
    """Get QB push statistics — subjects with pushable questions."""
    textbooks = await database.fetch_all("textbooks")
    # Deduplicate by subject
    seen = set()
    result = []
    for tb in textbooks:
        subject = tb["subject"]
        if subject in seen:
            continue
        seen.add(subject)

        total = await database.fetch_scalar(
            "SELECT COUNT(*) FROM mappings WHERE qp_id IN "
            "(SELECT id FROM question_papers WHERE subject = ?)",
            (subject,)
        )
        pushable = await database.fetch_scalar(
            "SELECT COUNT(*) FROM mappings WHERE qp_id IN "
            "(SELECT id FROM question_papers WHERE subject = ?) "
            "AND (is_reviewed = 1 OR confidence_level = 'high')",
            (subject,)
        )
        answered = await database.fetch_scalar(
            "SELECT COUNT(DISTINCT a.mapping_id) FROM answers a "
            "JOIN mappings m ON a.mapping_id = m.id "
            "WHERE m.qp_id IN (SELECT id FROM question_papers WHERE subject = ?)",
            (subject,)
        )
        images = await database.fetch_scalar(
            "SELECT COUNT(*) FROM answers WHERE images IS NOT NULL "
            "AND images != '[]' AND mapping_id IN "
            "(SELECT id FROM mappings WHERE qp_id IN "
            "(SELECT id FROM question_papers WHERE subject = ?))",
            (subject,)
        )

        result.append({
            "subject": subject,
            "total_questions": total,
            "pushable_questions": pushable,
            "answered_questions": answered,
            "answers_with_images": images,
        })

    return {"subjects": result}


# ── Viva Organizer ────────────────────────────────────────────────

from viva.generator import run_viva_pipeline
from viva.auto_tagger import auto_tag_batch
from viva.firestore_pusher import push_to_firestore, check_firestore_connection


@app.post("/api/viva/generate")
async def api_viva_generate(
    background_tasks: BackgroundTasks,
    subject: str = Form(...),
    textbook_id: str = Form(...),
    chapter_ids: str = Form(None),
    questions_per_chapter: int = Form(8),
):
    """Generate viva questions and answers from textbook chapters."""
    task_id = f"viva_{uuid.uuid4().hex[:8]}"
    selected_chapters = [c.strip() for c in chapter_ids.split(",") if c.strip()] if chapter_ids else None

    async def process():
        await asyncio.sleep(0.5)
        try:
            async def progress_cb(step, current, total, message):
                await send_progress(task_id, step, current, total, message)

            result = await run_viva_pipeline(
                subject=subject,
                textbook_id=textbook_id,
                chapter_ids=selected_chapters,
                questions_per_chapter=questions_per_chapter,
                progress_callback=progress_cb,
            )
            await send_progress(task_id, "done", 1, 1, json.dumps(result))
        except Exception as e:
            logger.exception("Viva generation failed")
            await send_progress(task_id, "error", 0, 0, str(e))

    background_tasks.add_task(process)
    return {"task_id": task_id, "message": "Viva generation started."}


@app.get("/api/viva/questions")
async def api_viva_list(
    subject: str = None,
    status: str = None,
    importance: str = None,
    chapter_name: str = None,
):
    """List generated viva questions with optional filters."""
    conditions = []
    params = []

    if subject:
        conditions.append("subject = ?")
        params.append(subject)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if importance:
        conditions.append("importance = ?")
        params.append(importance)
    if chapter_name:
        conditions.append("chapter_name = ?")
        params.append(chapter_name)

    where = " AND ".join(conditions) if conditions else ""
    rows = await database.fetch_all("viva_questions", where, tuple(params), "subject, chapter_name, topic_name, importance DESC")

    questions = []
    for r in rows:
        q = dict(r)
        # Parse JSON fields
        for field in ["explained_terms", "source_chunks"]:
            if q.get(field) and isinstance(q[field], str):
                try:
                    q[field] = json.loads(q[field])
                except json.JSONDecodeError:
                    q[field] = []
        if q.get("source_pages") and isinstance(q["source_pages"], str):
            try:
                q["source_pages"] = json.loads(q["source_pages"])
            except json.JSONDecodeError:
                q["source_pages"] = {}
        q["is_manual"] = bool(q.get("is_manual"))
        questions.append(q)

    return questions


@app.get("/api/viva/stats")
async def api_viva_stats():
    """Get viva question statistics."""
    all_q = await database.fetch_all("viva_questions")
    subjects = {}
    for q in all_q:
        subj = q.get("subject", "Unknown")
        if subj not in subjects:
            subjects[subj] = {"total": 0, "answered": 0, "pushed": 0,
                              "must_know": 0, "standard": 0, "advanced": 0,
                              "chapters": set(), "topics": set()}
        subjects[subj]["total"] += 1
        if q.get("answer_text"):
            subjects[subj]["answered"] += 1
        if q.get("status") == "pushed":
            subjects[subj]["pushed"] += 1
        imp = q.get("importance", "standard")
        if imp in subjects[subj]:
            subjects[subj][imp] += 1
        subjects[subj]["chapters"].add(q.get("chapter_name", ""))
        subjects[subj]["topics"].add(q.get("topic_name", ""))

    # Convert sets to counts
    for subj in subjects:
        subjects[subj]["chapter_count"] = len(subjects[subj].pop("chapters"))
        subjects[subj]["topic_count"] = len(subjects[subj].pop("topics"))

    return {
        "total_questions": len(all_q),
        "by_subject": subjects,
    }


@app.post("/api/viva/manual")
async def api_viva_manual(data: dict):
    """Add a manually written viva question. Auto-tags on save."""
    required = ["subject", "chapter_name", "topic_name", "question_text", "answer_text"]
    for field in required:
        if not data.get(field):
            raise HTTPException(400, f"Missing required field: {field}")

    q_id = f"vq_{uuid.uuid4().hex[:10]}"

    # Auto-tag immediately
    from viva.auto_tagger import get_subject_key_terms, _build_term_set, extract_explained_terms
    chapter_terms = await get_subject_key_terms(data["subject"])
    all_terms = _build_term_set(chapter_terms)
    combined = f"{data['question_text']} {data['answer_text']}"
    terms = extract_explained_terms(combined, all_terms)

    await database.insert("viva_questions", {
        "id": q_id,
        "subject": data["subject"],
        "chapter_name": data["chapter_name"],
        "topic_name": data["topic_name"],
        "question_text": data["question_text"],
        "answer_text": data["answer_text"],
        "explained_terms": terms,
        "importance": data.get("importance", "standard"),
        "difficulty": data.get("difficulty", 1),
        "is_manual": 1,
        "status": "generated",
    })

    return {"id": q_id, "explained_terms": terms, "message": "Question added and auto-tagged."}


@app.put("/api/viva/questions/{question_id}")
async def api_viva_update(question_id: str, data: dict):
    """Edit a viva question."""
    question = await database.fetch_one("viva_questions", question_id)
    if not question:
        raise HTTPException(404, "Question not found")

    allowed = ["question_text", "answer_text", "topic_name", "chapter_name",
               "importance", "difficulty", "status"]
    updates = {k: v for k, v in data.items() if k in allowed}

    # Re-tag if answer or question text changed
    if "answer_text" in updates or "question_text" in updates:
        from viva.auto_tagger import get_subject_key_terms, _build_term_set, extract_explained_terms
        subject = question["subject"]
        chapter_terms = await get_subject_key_terms(subject)
        all_terms = _build_term_set(chapter_terms)
        q_text = updates.get("question_text", question["question_text"])
        a_text = updates.get("answer_text", question["answer_text"])
        terms = extract_explained_terms(f"{q_text} {a_text}", all_terms)
        updates["explained_terms"] = terms

    if updates:
        await database.update("viva_questions", question_id, updates)

    return {"status": "ok", "updated": list(updates.keys())}


@app.delete("/api/viva/questions/{question_id}")
async def api_viva_delete(question_id: str):
    """Delete a viva question."""
    await database.execute("DELETE FROM viva_questions WHERE id = ?", (question_id,))
    return {"status": "ok"}


@app.delete("/api/viva/questions")
async def api_viva_delete_all(subject: str = None, status: str = None):
    """Delete viva questions with optional filters."""
    if subject and status:
        await database.execute("DELETE FROM viva_questions WHERE subject = ? AND status = ?", (subject, status))
    elif subject:
        await database.execute("DELETE FROM viva_questions WHERE subject = ?", (subject,))
    elif status:
        await database.execute("DELETE FROM viva_questions WHERE status = ?", (status,))
    else:
        await database.execute("DELETE FROM viva_questions", ())
    return {"status": "ok"}


@app.post("/api/viva/auto-tag")
async def api_viva_auto_tag(
    background_tasks: BackgroundTasks,
    question_ids: str = Form(...),
    subject: str = Form(...),
):
    """Auto-tag explainedTerms for selected viva questions."""
    ids = [qid.strip() for qid in question_ids.split(",") if qid.strip()]
    if not ids:
        raise HTTPException(400, "No question IDs provided")

    task_id = f"tag_{uuid.uuid4().hex[:8]}"

    async def process():
        await asyncio.sleep(0.3)
        try:
            async def progress_cb(step, current, total, message):
                await send_progress(task_id, step, current, total, message)

            result = await auto_tag_batch(ids, subject, progress_cb)
            await send_progress(task_id, "done", 1, 1, json.dumps(result))
        except Exception as e:
            await send_progress(task_id, "error", 0, 0, str(e))

    background_tasks.add_task(process)
    return {"task_id": task_id, "count": len(ids)}


@app.post("/api/viva/push-to-firestore")
async def api_viva_push(
    background_tasks: BackgroundTasks,
    question_ids: str = Form(...),
):
    """Push selected viva questions directly to Firestore."""
    ids = [qid.strip() for qid in question_ids.split(",") if qid.strip()]
    if not ids:
        raise HTTPException(400, "No question IDs provided")

    task_id = f"push_{uuid.uuid4().hex[:8]}"

    async def process():
        await asyncio.sleep(0.3)
        try:
            async def progress_cb(step, current, total, message):
                await send_progress(task_id, step, current, total, message)

            result = await push_to_firestore(ids, progress_cb)
            await send_progress(task_id, "done", 1, 1, json.dumps(result))
        except Exception as e:
            logger.exception("Firestore push failed")
            await send_progress(task_id, "error", 0, 0, str(e))

    background_tasks.add_task(process)
    return {"task_id": task_id, "count": len(ids)}


@app.get("/api/viva/firestore-status")
async def api_viva_firestore_status():
    """Check Firestore connection status."""
    return check_firestore_connection()


# ── Knowledge Graph ─────────────────────────────────────────────

@app.post("/api/knowledge/build/{textbook_id}")
async def api_kg_build(
    textbook_id: str,
    background_tasks: BackgroundTasks,
):
    """Build the knowledge graph for a textbook (submits Claude batch)."""
    textbook = await database.fetch_one("textbooks", textbook_id)
    if not textbook:
        raise HTTPException(404, "Textbook not found")

    task_id = f"kg_{uuid.uuid4().hex[:8]}"

    async def process():
        await asyncio.sleep(0.5)
        try:
            async def progress_cb(step, current, total, message):
                await send_progress(task_id, step, current, total, message)

            result = await build_knowledge_graph(
                textbook_id=textbook_id,
                progress_callback=progress_cb,
            )
            await send_progress(task_id, "done", 1, 1, json.dumps(result))
        except Exception as e:
            logger.exception("Knowledge graph build failed")
            await send_progress(task_id, "error", 0, 0, str(e))

    background_tasks.add_task(process)
    return {"task_id": task_id, "message": "Knowledge graph build started."}


@app.post("/api/knowledge/batch/{textbook_id}")
async def api_kg_process_batch(textbook_id: str):
    """Process Claude batch results for knowledge graph extraction."""
    textbook = await database.fetch_one("textbooks", textbook_id)
    if not textbook:
        raise HTTPException(404, "Textbook not found")

    # Load batch ID from checkpoint — try multiple locations
    from state.checkpoint import Checkpoint

    batch_id = None
    # 1. Standalone KG checkpoint
    cp1 = Checkpoint("build_kg", textbook_id)
    batch_id = cp1.get_meta("kg_batch_id")

    if not batch_id:
        # 2. Integrated ingestion checkpoint
        cp2 = Checkpoint("ingest_textbook", f"{textbook['subject']}_{textbook_id}")
        batch_id = cp2.get_meta("kg_batch_id")

    if not batch_id:
        raise HTTPException(404, "No KG batch ID found. The KG batch may not have been submitted for this textbook.")

    result = await process_kg_batch_results(textbook_id, batch_id)
    return result



@app.post("/api/knowledge/extract-relations/{textbook_id}")
async def api_kg_extract_relations(textbook_id: str):
    """Submit a batch to extract relations between existing concepts."""
    textbook = await database.fetch_one("textbooks", textbook_id)
    if not textbook:
        raise HTTPException(404, "Textbook not found")

    result = await extract_relations_for_textbook(textbook_id)
    return result


@app.post("/api/knowledge/process-relations/{textbook_id}")
async def api_kg_process_relations(textbook_id: str):
    """Process Claude batch results for relations extraction."""
    textbook = await database.fetch_one("textbooks", textbook_id)
    if not textbook:
        raise HTTPException(404, "Textbook not found")

    from state.checkpoint import Checkpoint
    batch_id = None

    cp = Checkpoint("extract_relations", textbook_id)
    batch_id = cp.get_meta("relations_batch_id")

    if not batch_id:
        raise HTTPException(404, "No relations batch ID found.")

    result = await process_relations_batch_results(textbook_id, batch_id)
    return result


@app.get("/api/knowledge/stats")
async def api_kg_stats(subject: str = None):
    """Get knowledge graph statistics, optionally filtered by subject."""
    stats = await get_graph_stats(subject)

    # Attach per-textbook KG status
    textbooks = await database.fetch_all("textbooks")
    kg_statuses = [
        {
            "id": tb["id"],
            "name": tb["name"],
            "subject": tb["subject"],
            "kg_status": tb.get("kg_status", "not_built"),
        }
        for tb in textbooks
        if not subject or tb["subject"] == subject
    ]

    return {**stats, "textbooks": kg_statuses}


@app.get("/api/knowledge/search")
async def api_kg_search(
    q: str = "",
    subject: str = None,
    concept_type: str = None,
    importance: str = None,
    limit: int = 50,
):
    """Search the knowledge graph for concepts."""
    concepts = await search_concepts(
        query=q,
        subject=subject,
        concept_type=concept_type,
        importance=importance,
        limit=min(limit, 200),
    )
    return {"concepts": concepts, "count": len(concepts)}


@app.get("/api/knowledge/concepts/{concept_id}")
async def api_kg_get_concept(concept_id: str):
    """Get a concept with all its relations and sources."""
    concept = await get_concept_with_relations(concept_id)
    if not concept:
        raise HTTPException(404, "Concept not found")
    return concept


@app.get("/api/knowledge/graph")
async def api_kg_graph(
    subject: str = None,
    concept_type: str = None,
    limit: int = 5000,
):
    """Get graph data (nodes + edges) for visualization."""
    if subject == "":
        subject = None

    graph = await get_graph_for_subject(
        subject=subject,
        concept_type=concept_type,
        limit=limit,
    )
    return graph


@app.get("/api/knowledge/neighbors/{concept_id}")
async def api_kg_neighbors(concept_id: str, depth: int = 1):
    """Get a concept and its immediate neighbors in the graph."""
    concept = await get_concept_with_relations(concept_id)
    if not concept:
        raise HTTPException(404, "Concept not found")

    # Collect neighbor IDs
    neighbor_ids = set()
    for rel in concept.get("outgoing_relations", []):
        neighbor_ids.add(rel["target_id"])
    for rel in concept.get("incoming_relations", []):
        neighbor_ids.add(rel["source_id"])

    # Build subgraph
    nodes = [{"id": concept["id"], "name": concept["name"],
               "concept_type": concept["concept_type"],
               "importance": concept["importance"],
               "frequency": concept["frequency"], "is_center": True}]

    edges = []
    for rel in concept.get("outgoing_relations", []):
        neighbor = await database.fetch_one("concepts", rel["target_id"])
        if neighbor:
            nodes.append({"id": neighbor["id"], "name": neighbor["name"],
                          "concept_type": neighbor["concept_type"],
                          "importance": neighbor["importance"],
                          "frequency": neighbor["frequency"], "is_center": False})
            edges.append({"source": concept["id"], "target": rel["target_id"],
                          "relation_type": rel["relation_type"], "confidence": rel["confidence"]})

    for rel in concept.get("incoming_relations", []):
        neighbor = await database.fetch_one("concepts", rel["source_id"])
        if neighbor and not any(n["id"] == neighbor["id"] for n in nodes):
            nodes.append({"id": neighbor["id"], "name": neighbor["name"],
                          "concept_type": neighbor["concept_type"],
                          "importance": neighbor["importance"],
                          "frequency": neighbor["frequency"], "is_center": False})
        if neighbor:
            edges.append({"source": rel["source_id"], "target": concept["id"],
                          "relation_type": rel["relation_type"], "confidence": rel["confidence"]})

    return {"center": concept, "nodes": nodes, "edges": edges}


@app.get("/api/knowledge/for-question")
async def api_kg_for_question(question: str, subject: str):
    """Find concepts relevant to a question (for GraphRAG context)."""
    concepts = await get_concepts_for_question(question, subject)
    return {"concepts": concepts, "count": len(concepts)}


@app.delete("/api/knowledge/concepts/{concept_id}")
async def api_kg_delete_concept(concept_id: str):
    """Delete a concept and its relations/sources."""
    await database.execute("DELETE FROM concept_sources WHERE concept_id = ?", (concept_id,))
    await database.execute(
        "DELETE FROM concept_relations WHERE source_id = ? OR target_id = ?",
        (concept_id, concept_id)
    )
    await database.execute("DELETE FROM concepts WHERE id = ?", (concept_id,))
    return {"status": "ok", "message": "Concept deleted"}


@app.delete("/api/knowledge")
async def api_kg_delete_all(subject: str = None):
    """Delete all knowledge graph data for a subject (or all)."""
    if subject:
        # Get concept IDs for this subject
        concepts = await database.fetch_all("concepts", "subject = ?", (subject,))
        for c in concepts:
            await database.execute("DELETE FROM concept_sources WHERE concept_id = ?", (c["id"],))
            await database.execute(
                "DELETE FROM concept_relations WHERE source_id = ? OR target_id = ?",
                (c["id"], c["id"])
            )
        await database.execute("DELETE FROM concepts WHERE subject = ?", (subject,))
        # Reset KG status for textbooks in this subject
        await database.execute(
            "UPDATE textbooks SET kg_status = 'not_built' WHERE subject = ?", (subject,)
        )
    else:
        await database.execute("DELETE FROM concept_sources", ())
        await database.execute("DELETE FROM concept_relations", ())
        await database.execute("DELETE FROM concepts", ())
        await database.execute("UPDATE textbooks SET kg_status = 'not_built'", ())

    return {"status": "ok", "message": "Knowledge graph data deleted"}


@app.post("/api/knowledge/concepts")
async def api_kg_add_concept(data: dict):
    """Manually add a concept to the knowledge graph."""
    required = ["name", "concept_type", "subject"]
    for field in required:
        if not data.get(field):
            raise HTTPException(400, f"Missing required field: {field}")

    from knowledge.graph_builder import _find_or_create_concept
    concept_id, is_new = await _find_or_create_concept(
        name=data["name"],
        concept_type=data.get("concept_type", "other"),
        definition=data.get("definition", ""),
        aliases=data.get("aliases", []),
        importance=data.get("importance", "standard"),
        subject=data["subject"],
    )
    return {"id": concept_id, "is_new": is_new, "message": "Concept added" if is_new else "Concept merged"}


@app.post("/api/knowledge/relations")
async def api_kg_add_relation(data: dict):
    """Manually add a relation between two concepts."""
    required = ["source_id", "target_id", "relation_type"]
    for field in required:
        if not data.get(field):
            raise HTTPException(400, f"Missing required field: {field}")

    from knowledge.graph_builder import _upsert_relation
    added = await _upsert_relation(
        source_id=data["source_id"],
        target_id=data["target_id"],
        relation_type=data["relation_type"],
        confidence=float(data.get("confidence", 0.9)),
        extracted_by="manual",
    )
    if not added:
        return {"status": "exists", "message": "Relation already exists or invalid"}
    return {"status": "ok", "message": "Relation added"}


@app.get("/api/knowledge/concept-types")
async def api_kg_concept_types():
    """Return valid concept types."""
    return {"types": sorted(VALID_CONCEPT_TYPES)}


# ── Run ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=settings.backend_port, log_level="info")
