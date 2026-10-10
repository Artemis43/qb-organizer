"""Orchestrator entry point for the medical-grade answer pipeline (Task 28).

Implements the per-mapping flow from design.md §Architecture diagram:

    mapping_id
      ├─ load mapping → chapter → textbook
      ├─ intent.classify_question                              (R2)
      ├─ coverage.resolve_coverage_mode                         (R3, R13)
      ├─ kg_skeleton.build_kg_skeleton  (KGUnavailable→empty)   (R6, R7)
      ├─ retrieval.retrieve                                     (R6, R15)
      ├─ draft.generate_draft (Haiku)                           (R4)
      ├─ validators.validate_mandatory_data → 1× simple retry   (R5)
      ├─ critic.evaluate / evaluate_lite                        (R8)
      ├─ regen Haiku once on fail                               (R8.4)
      ├─ cost-gated Sonnet escalation on second fail            (R8.5, R9)
      ├─ provenance + markdown serialization                    (R11, R12)
      └─ insert into `answers` (DELETE-then-INSERT by mapping_id)

The Sonnet escalation + cost-gate branch (R8.5, R9.5–R9.6) is wired by
task 33: a final critic ``fail`` after the Haiku regen consults
``cost_gate.check`` and either escalates to ``settings.sonnet_model`` or
emits a ``cost_blocked`` SSE event and persists the latest Haiku draft
with ``provenance.cost_ceiling_blocked=True``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from answers import (
    cost_gate as cost_gate_module,
    coverage as coverage_module,
    critic as critic_module,
    draft as draft_module,
    intent as intent_module,
    kg_skeleton as kg_skeleton_module,
    prompts as prompts_module,
    retrieval as retrieval_module,
    validators as validators_module,
)
from answers.draft import DraftContext
from answers.errors import (
    AnswerPipelineError,
    CriticFailure,
    KGUnavailable,
    SchemaViolation,
)
from answers.markdown import serialize_to_markdown
from answers.schema import (
    AnswerRecord,
    AnswerSections,
    GenerateBatchResult,
    KGSkeleton,
    Provenance,
    PurgeReport,
)
from config import settings
from state import db as database

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# In-memory per-mapping concurrency lock (Task 29)
# ──────────────────────────────────────────────────────────────────────────
#
# Per design.md §Failure-Mode Matrix → "Concurrent generate request for same
# mapping": a second invocation for a ``mapping_id`` already in flight must be
# rejected with a 409-style error rather than enqueued. The mapping to an HTTP
# 409 status code happens in the API layer (Pass 2). Different mapping ids run
# concurrently without contention because each gets its own ``asyncio.Lock``.

_inflight_locks: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def _acquire_inflight(mapping_id: str):
    """Async context manager that guarantees at most one in-flight ``_process_one``
    per ``mapping_id``.

    Behaviour:

    1. Look up (or create) an :class:`asyncio.Lock` keyed by ``mapping_id``.
    2. If the lock is already held by another task, raise
       :class:`AnswerPipelineError` with a ``409 Conflict`` detail string.
       The API layer translates the 409 prefix into an HTTP status (Pass 2).
    3. Otherwise, acquire the lock for the duration of the block.
    4. On exit, drop the entry from ``_inflight_locks`` if no waiter holds it
       anymore so the dict does not grow unbounded as mapping ids accumulate.
    """
    lock = _inflight_locks.setdefault(mapping_id, asyncio.Lock())
    if lock.locked():
        raise AnswerPipelineError(
            mapping_id,
            f"409 Conflict: generation already in flight for mapping_id={mapping_id}",
        )
    try:
        async with lock:
            yield
    finally:
        # Best-effort cleanup so the dict does not grow unbounded.
        # Runs *after* ``async with lock`` exits, so the lock is already
        # released. The lock may have been re-acquired by another concurrent
        # caller in the meantime, so guard with ``locked()`` and an identity
        # check before removing.
        existing = _inflight_locks.get(mapping_id)
        if existing is lock and not lock.locked():
            _inflight_locks.pop(mapping_id, None)


# ──────────────────────────────────────────────────────────────────────────
# Helper: load mapping → chapter → textbook joined context
# ──────────────────────────────────────────────────────────────────────────


async def _load_mapping_context(mapping_id: str) -> dict:
    """Returns ``{mapping, chapter, textbook, subject, chapter_name, textbook_name, chapter_id}``.

    Raises :class:`AnswerPipelineError` when the mapping row is missing — the
    orchestrator boundary (task 30) catches this and records it in
    ``GenerateBatchResult.errors``. Missing chapter / textbook rows degrade
    gracefully: ``subject`` and ``textbook_name`` come back as empty strings
    so the rest of the pipeline can still run with vector-only retrieval.
    """
    mapping = await database.fetch_one("mappings", mapping_id)
    if not mapping:
        raise AnswerPipelineError(mapping_id, "mapping not found")

    chapter_id = mapping.get("final_chapter_id") or ""
    chapter_name = mapping.get("final_chapter_name") or ""
    chapter: Optional[dict] = None
    textbook: Optional[dict] = None
    subject = ""
    textbook_name = ""

    if chapter_id:
        chapter = await database.fetch_one("chapters", chapter_id)
        if chapter:
            if not chapter_name:
                chapter_name = chapter.get("name", "") or ""
            textbook_id = chapter.get("textbook_id", "") or ""
            if textbook_id:
                textbook = await database.fetch_one("textbooks", textbook_id)
                if textbook:
                    subject = textbook.get("subject", "") or ""
                    textbook_name = textbook.get("name", "") or ""

    return {
        "mapping": mapping,
        "chapter": chapter,
        "textbook": textbook,
        "subject": subject,
        "chapter_name": chapter_name,
        "textbook_name": textbook_name,
        "chapter_id": chapter_id,
    }


# ──────────────────────────────────────────────────────────────────────────
# Helper: KG skeleton with KGUnavailable fallback
# ──────────────────────────────────────────────────────────────────────────


async def _maybe_build_kg(question_text: str, subject: str) -> KGSkeleton:
    """Wraps :func:`kg_skeleton.build_kg_skeleton` with a ``KGUnavailable``
    → empty :class:`KGSkeleton` (``used=False``) fallback per R6.5 and the
    design §Failure-Mode Matrix row "KG matched zero concepts"."""
    try:
        return await kg_skeleton_module.build_kg_skeleton(question_text, subject)
    except KGUnavailable as exc:
        logger.warning(
            "kg_skeleton unavailable, falling back to vector-only: %s", exc.detail
        )
    except Exception as exc:  # pragma: no cover — defensive, KGUnavailable is preferred
        logger.warning(
            "kg_skeleton.build raised unexpected %s: %s", type(exc).__name__, exc
        )

    return KGSkeleton(
        concept_ids=[],
        relation_ids=[],
        must_know_concept_ids=[],
        skeleton_block_text="",
        used=False,
    )


# ──────────────────────────────────────────────────────────────────────────
# Helpers: draft introspection (must_know_covered + sections_populated)
# ──────────────────────────────────────────────────────────────────────────


def _walk_bullet_texts(draft: AnswerSections):
    """Yield the ``text`` of every leaf bullet across all sections."""
    if draft.classification:
        for b in draft.classification:
            yield b.text
    if draft.etiology:
        for b in draft.etiology:
            yield b.text
    if draft.pathophysiology:
        for b in draft.pathophysiology:
            yield b.text
    if draft.clinical_features is not None:
        for sub in ("symptoms", "signs", "hallmarks"):
            for b in (getattr(draft.clinical_features, sub) or []):
                yield b.text
    if draft.investigations is not None:
        for sub in ("bedside", "lab", "imaging"):
            for b in (getattr(draft.investigations, sub) or []):
                yield b.text
    if draft.management is not None:
        for sub in ("non_pharm", "pharm", "surgical", "targets"):
            for b in (getattr(draft.management, sub) or []):
                yield b.text
    if draft.complications is not None:
        for sub in ("acute", "chronic"):
            for b in (getattr(draft.complications, sub) or []):
                yield b.text
    if draft.prognosis:
        for b in draft.prognosis:
            yield b.text


def _compute_must_know_covered(
    must_know_ids: list[str],
    must_know_concepts: list[dict],
    draft: AnswerSections,
) -> list[str]:
    """Return must-know concept IDs whose name appears (case-insensitive
    substring) in any bullet text of the final answer.

    ``must_know_concepts`` is a list of ``{"id": ..., "name": ...}`` dicts
    looked up from the ``concepts`` table; missing entries fall back to
    matching the concept ID itself.
    """
    if not must_know_ids:
        return []

    name_by_id = {
        c.get("id"): (c.get("name") or "")
        for c in (must_know_concepts or [])
    }

    parts = list(_walk_bullet_texts(draft))
    if draft.definition:
        parts.append(draft.definition)
    blob = " ".join(parts).lower()

    covered: list[str] = []
    for mk_id in must_know_ids:
        name = (name_by_id.get(mk_id) or mk_id) or ""
        if name and name.lower() in blob:
            covered.append(mk_id)
    return covered


def _compute_sections_populated(draft: AnswerSections) -> list[str]:
    """Return the canonical-9 section keys whose value is non-None and non-empty.

    A composite section (e.g. ``clinical_features``) counts as populated when
    its container is non-None — the schema's ``_collapse_empty`` validator
    has already nulled out fully-empty composites.
    """
    populated: list[str] = []
    if draft.definition and draft.definition.strip():
        populated.append("definition")
    if draft.classification:
        populated.append("classification")
    if draft.etiology:
        populated.append("etiology")
    if draft.pathophysiology:
        populated.append("pathophysiology")
    if draft.clinical_features is not None:
        populated.append("clinical_features")
    if draft.investigations is not None:
        populated.append("investigations")
    if draft.management is not None:
        populated.append("management")
    if draft.complications is not None:
        populated.append("complications")
    if draft.prognosis:
        populated.append("prognosis")
    return populated


async def _lookup_must_know_concepts(must_know_ids: list[str]) -> list[dict]:
    """Best-effort fetch of concept names from the ``concepts`` table.

    Concepts that do not resolve (DB row missing, table empty, fetch errors)
    fall back to ``{"id": mk_id, "name": mk_id}`` so the substring match in
    :func:`_compute_must_know_covered` still has something to compare.
    """
    out: list[dict] = []
    for mk_id in must_know_ids:
        row: Optional[dict] = None
        try:
            row = await database.fetch_one("concepts", mk_id)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug("concept fetch failed for %s: %s", mk_id, exc)
        if row:
            out.append({"id": row["id"], "name": row.get("name", "")})
        else:
            out.append({"id": mk_id, "name": mk_id})
    return out


# ──────────────────────────────────────────────────────────────────────────
# Public — per-mapping orchestrator
# ──────────────────────────────────────────────────────────────────────────


async def _process_one(
    mapping_id: str,
    claude_client: Any = None,
    progress_callback=None,
) -> AnswerRecord:
    """The full per-mapping flow. See module docstring for the stage list.

    Returns a fully-populated :class:`AnswerRecord` and persists a row in
    the ``answers`` table. Existing answers for the same ``mapping_id`` are
    deleted first so a regenerate doesn't accumulate stale rows.

    Holds the per-mapping in-memory lock from :func:`_acquire_inflight` for
    the duration of the flow so a concurrent call for the same ``mapping_id``
    is rejected with a 409-style :class:`AnswerPipelineError` (task 29 /
    design §Failure-Mode Matrix → "Concurrent generate for same mapping_id").
    """
    async with _acquire_inflight(mapping_id):
        return await _process_one_locked(
            mapping_id=mapping_id,
            claude_client=claude_client,
            progress_callback=progress_callback,
        )


async def _process_one_locked(
    mapping_id: str,
    claude_client: Any = None,
    progress_callback=None,
) -> AnswerRecord:
    """Body of :func:`_process_one`, executed while the inflight lock is held."""
    # 1. Mapping → chapter → textbook context.
    ctx = await _load_mapping_context(mapping_id)
    mapping = ctx["mapping"]
    question_text: str = mapping.get("question_text") or ""
    question_type: str = (mapping.get("question_type") or "SAQ").upper()
    chapter_id: str = ctx["chapter_id"]
    chapter_name: str = ctx["chapter_name"]
    subject: str = ctx["subject"]
    textbook_name: str = ctx["textbook_name"]

    # 2. Intent classification (R2).
    intent_obj = await intent_module.classify_question(
        question_text, question_type, claude_client=claude_client
    )

    # 3. Coverage mode (R3, R13).
    cm = coverage_module.resolve_coverage_mode(
        question_type,
        intent_obj.intent,
        intent_obj.focus_topic,
        intent_obj.requested_item_count,
    )

    # 4. KG skeleton with KGUnavailable→empty fallback (R6.5).
    kg = await _maybe_build_kg(question_text, subject)
    kg_skeleton_used = bool(kg.used)

    # 5. Retrieval (vector + graph, R6/R15).
    retrieved = await retrieval_module.retrieve(
        question_text, chapter_id, subject, cm
    )

    # 6. Mnemonic hint for the most likely populated section (R14).
    primary_section = (cm.sections_to_populate or ["definition"])[0]
    mnemonic_hint = prompts_module.find_mnemonic(intent_obj.focus_topic, primary_section)

    # 7. Build the DraftContext shared by draft + critic stages.
    draft_ctx = DraftContext(
        question_text=question_text,
        question_type=question_type,
        intent=intent_obj.intent,
        focus_topic=intent_obj.focus_topic,
        requested_item_count=intent_obj.requested_item_count,
        coverage_mode=cm,
        kg_skeleton=kg,
        retrieved_context=retrieved,
        mnemonic_hint=mnemonic_hint,
        subject=subject,
    )

    # 8. Generate the first draft (Haiku).
    draft = await draft_module.generate_draft(
        ctx=draft_ctx,
        model=settings.haiku_model,
        claude_client=claude_client,
    )
    draft_model = settings.haiku_model

    # 9. Mandatory-data validation; on violations, do ONE simple retry.
    #
    # NOTE(future): the validator-aware re-prompt that targets only the
    # failing section is a future refinement. For now we do a single regen
    # and fall through with the imperfect draft if it still fails (R5.7).
    initial_report = validators_module.validate_mandatory_data(draft)
    if not initial_report.ok:
        try:
            draft = await draft_module.generate_draft(
                ctx=draft_ctx,
                model=settings.haiku_model,
                claude_client=claude_client,
            )
        except SchemaViolation as exc:
            logger.warning(
                "[%s] validator-retry generate_draft failed: %s",
                mapping_id, exc.detail,
            )

    # 10. Examiner critic — full or VSAQ-lite (R8).
    critic_model = settings.haiku_model
    critic_fn = (
        critic_module.evaluate_lite
        if question_type == "VSAQ"
        else critic_module.evaluate
    )
    verdict = await critic_fn(
        ctx=draft_ctx,
        draft=draft,
        claude_client=claude_client,
        model=critic_model,
    )

    regen_model: Optional[str] = None
    escalation_model: Optional[str] = None
    cost_ceiling_blocked: bool = False

    # 11. Critic failure → regenerate once with Haiku (R8.4).
    if verdict.verdict == "fail":
        try:
            draft_regen = await draft_module.generate_draft(
                ctx=draft_ctx,
                model=settings.haiku_model,
                claude_client=claude_client,
            )
            verdict_regen = await critic_fn(
                ctx=draft_ctx,
                draft=draft_regen,
                claude_client=claude_client,
                model=critic_model,
            )
            # Persist the latest verdict regardless of pass/fail (per task 28).
            draft = draft_regen
            verdict = verdict_regen
            regen_model = settings.haiku_model
        except (SchemaViolation, CriticFailure) as exc:
            logger.warning(
                "[%s] regen attempt failed: %s",
                mapping_id, exc.detail,
            )

    # 12. Cost-gated Sonnet escalation when the verdict is still "fail" after
    #     the Haiku regen (task 33, R9.4–R9.6, design §Cost Ceiling State
    #     Machine). The gate consults the in-memory batch spend (proxy via
    #     ``claude_client.cost_tracker._total``) and the persisted daily
    #     spend in ``daily_cost_log`` keyed by UTC date. On ``allow`` we
    #     regenerate with Sonnet and re-run the critic (which stays on Haiku
    #     per R9.2). On ``block`` we keep the latest Haiku draft, set
    #     ``cost_ceiling_blocked=True``, and emit a ``cost_blocked`` SSE event.
    if verdict.verdict == "fail":
        # Read ceilings defensively — task 35 adds the fields to ``settings``.
        batch_ceiling = getattr(settings, "batch_cost_ceiling_usd", 0.50)
        daily_ceiling = getattr(settings, "daily_cost_ceiling_usd", 5.00)

        today = _today_utc()
        daily_row = await _read_or_init_daily_cost_log(today)
        batch_spend = _client_total_spend(claude_client) or 0.0
        daily_spend = float(daily_row.get("total_usd") or 0.0)
        projected = _projected_sonnet_cost_usd()

        decision = cost_gate_module.check(
            "escalation_sonnet",
            projected,
            batch_spend,
            daily_spend,
            batch_ceiling,
            daily_ceiling,
        )

        if decision == "allow":
            try:
                draft_sonnet = await draft_module.generate_draft(
                    ctx=draft_ctx,
                    model=settings.sonnet_model,
                    claude_client=claude_client,
                )
                verdict_sonnet = await critic_fn(
                    ctx=draft_ctx,
                    draft=draft_sonnet,
                    claude_client=claude_client,
                    model=critic_model,  # critic stays on Haiku per R9.2
                )
                draft = draft_sonnet
                verdict = verdict_sonnet
                escalation_model = settings.sonnet_model
            except (SchemaViolation, CriticFailure) as exc:
                logger.warning(
                    "[%s] Sonnet escalation failed: %s",
                    mapping_id, exc.detail,
                )
                # Fall through with the latest Haiku draft and verdict.
        else:  # decision == "block"
            cost_ceiling_blocked = True
            await _emit(
                progress_callback,
                "cost_blocked",
                {
                    "mapping_id": mapping_id,
                    "stage": "escalation_sonnet",
                    "projected_usd": projected,
                    "batch_ceiling_usd": batch_ceiling,
                    "daily_ceiling_usd": daily_ceiling,
                },
            )

    # 13. Final mandatory-data assessment for the persisted draft.
    final_report = validators_module.validate_mandatory_data(draft)
    mandatory_data_complete = bool(final_report.ok)

    # 14. Provenance computation (R12).
    must_know_concepts = await _lookup_must_know_concepts(
        list(kg.must_know_concept_ids)
    )
    must_know_covered = _compute_must_know_covered(
        list(kg.must_know_concept_ids), must_know_concepts, draft
    )
    sections_populated = _compute_sections_populated(draft)

    provenance = Provenance(
        draft_model=draft_model,
        critic_model=critic_model,
        regen_model=regen_model,
        escalation_model=escalation_model,
        critic_verdict=verdict.verdict,
        critic_score=verdict.score,
        must_know_presented=list(kg.must_know_concept_ids),
        must_know_covered=must_know_covered,
        sections_populated=sections_populated,
        retrieval_mode=retrieved.retrieval_mode,
        kg_concept_ids_used=list(kg.concept_ids),
        kg_relation_ids_used=list(kg.relation_ids),
        cited_textbook_pages=list(retrieved.cited_textbook_pages),
        cost_ceiling_blocked=cost_ceiling_blocked,
    )

    # 15. Markdown serialization (R11).
    md = serialize_to_markdown(draft)

    # 16. Persist — DELETE first so a regenerate doesn't keep a stale row.
    await database.execute(
        "DELETE FROM answers WHERE mapping_id = ?", (mapping_id,)
    )

    answer_id = f"ans_{uuid.uuid4().hex[:12]}"
    sections_json = draft.model_dump_json(exclude_none=True)
    provenance_json = provenance.model_dump_json()

    row = {
        "id": answer_id,
        "mapping_id": mapping_id,
        "question_text": question_text,
        "chapter_id": chapter_id,
        "chapter_name": chapter_name,
        # ── New medical-grade columns (R1, R2, R3, R5, R6, R11, R12, R13) ──
        "sections": sections_json,
        "markdown": md,
        "coverage_mode": cm.mode,
        "question_intent": intent_obj.intent,
        "focus_topic": intent_obj.focus_topic,
        "requested_item_count": intent_obj.requested_item_count,
        "intent_confidence_low": 1 if intent_obj.intent_confidence_low else 0,
        "provenance": provenance_json,
        "mandatory_data_complete": 1 if mandatory_data_complete else 0,
        "vsaq_disambiguation_fallback": 1 if cm.vsaq_disambiguation_fallback else 0,
        "kg_skeleton_used": 1 if kg_skeleton_used else 0,
        # ── Legacy columns kept for backwards compatibility with existing reads ──
        "prologue": "",
        "bullets": "[]",
        "epilogue": "",
        "bullet_count": 0,
        "bullet_style": "detailed",
        "preset": "",
        "source_chunks": json.dumps(list(retrieved.chunk_ids)),
        "source_pages": json.dumps({}),
        "textbook_name": textbook_name,
        "model_used": draft_model,
        "status": "generated",
        "retrieval_mode": retrieved.retrieval_mode,
        "retrieval_metadata": json.dumps({}),
    }

    await database.insert("answers", row)

    return AnswerRecord(
        id=answer_id,
        mapping_id=mapping_id,
        sections=draft,
        markdown=md,
        coverage_mode=cm.mode,
        question_intent=intent_obj.intent,
        focus_topic=intent_obj.focus_topic,
        requested_item_count=intent_obj.requested_item_count,
        intent_confidence_low=intent_obj.intent_confidence_low,
        mandatory_data_complete=mandatory_data_complete,
        vsaq_disambiguation_fallback=cm.vsaq_disambiguation_fallback,
        kg_skeleton_used=kg_skeleton_used,
        provenance=provenance,
    )


# ──────────────────────────────────────────────────────────────────────────
# Public — batch driver (Task 30)
# ──────────────────────────────────────────────────────────────────────────
#
# Per design.md §Pipeline orchestrator + §SSE Event Extensions:
#
#   * Iterates ``mapping_ids``; calls ``_process_one`` for each inside a
#     ``try/except AnswerPipelineError`` so a single failure never aborts
#     the batch.
#   * Tracks per-batch spend in a local variable. Real cost wiring lands in
#     task 33 (cost-gate escalation); for now we surface ``cost_tracker``
#     numbers when the supplied client carries one, otherwise fall back to
#     the per-call counter.
#   * Reads / refreshes the per-day spend row in ``daily_cost_log`` keyed by
#     UTC date. Row creation here is a placeholder; task 33 will mutate
#     ``total_usd`` / ``calls_count`` after each Claude call.
#   * Emits an ``persisted`` SSE event after each successful mapping (the
#     full per-stage event list — ``intent_classified``, ``draft_done``, … —
#     is wired in Phase 5 task 40 once ``_process_one`` is instrumented).
#   * ``force_regenerate`` is documented for API symmetry. ``_process_one``
#     already DELETEs any existing answer for the mapping before re-inserting,
#     so regeneration is the implicit default; the flag is forwarded
#     unchanged for future use.

# Stages whose cost contributes to the per-batch / per-day spend snapshots.
# ``draft`` and ``critic`` always allowed by the gate; ``regen_haiku`` and
# ``escalation_sonnet`` plug in via task 33.


def _today_utc() -> str:
    """ISO-format UTC date used as the ``daily_cost_log.date`` primary key."""
    return datetime.now(timezone.utc).date().isoformat()


async def _read_or_init_daily_cost_log(date_iso: str) -> dict:
    """Return ``{'date', 'total_usd', 'calls_count'}`` for the given UTC date.

    Inserts a fresh ``(date, 0.0, 0)`` row if none exists so the orchestrator
    always sees a consistent shape. Task 33 wires the actual updates after
    each Claude call.
    """
    rows = await database.fetch_all(
        "daily_cost_log", where="date = ?", params=(date_iso,)
    )
    if rows:
        row = rows[0]
        return {
            "date": row.get("date") or date_iso,
            "total_usd": float(row.get("total_usd") or 0.0),
            "calls_count": int(row.get("calls_count") or 0),
        }

    # Fresh row — best-effort insert. ``INSERT OR REPLACE`` is safe under
    # concurrent batches because the primary key is the date string.
    try:
        await database.execute(
            "INSERT OR IGNORE INTO daily_cost_log (date, total_usd, calls_count) "
            "VALUES (?, 0, 0)",
            (date_iso,),
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("daily_cost_log seed failed for %s: %s", date_iso, exc)
    return {"date": date_iso, "total_usd": 0.0, "calls_count": 0}


def _client_total_spend(claude_client: Any) -> Optional[float]:
    """Return the running USD total from ``claude_client.cost_tracker._total``
    if the supplied client exposes one; otherwise ``None``.

    Used as a best-effort proxy for batch spend until task 33 wires per-stage
    cost projections through the cost gate. We read the private ``_total``
    attribute directly because the public ``total_spent`` coroutine would
    require an ``await`` and a DB roundtrip.
    """
    tracker = getattr(claude_client, "cost_tracker", None)
    if tracker is None:
        return None
    total = getattr(tracker, "_total", None)
    if total is None:
        return None
    try:
        return float(total)
    except (TypeError, ValueError):  # pragma: no cover — defensive
        return None


def _client_call_count(claude_client: Any) -> int:
    """Return a call-count proxy from the supplied client.

    ``ClaudeClient`` exposes ``_request_count``; ``FakeClaudeClient`` (and
    the test ``StageFake``) expose ``call_count``. Returns ``0`` when neither
    is available.
    """
    for attr in ("call_count", "_request_count"):
        value = getattr(claude_client, attr, None)
        if isinstance(value, int):
            return value
    return 0


async def _emit(progress_callback, event_name: str, data: dict) -> None:
    """Invoke ``progress_callback(event_name, data)`` if provided.

    The callback is expected to be ``async``; sync callbacks are tolerated
    so test fakes can register a plain function. Errors raised by the
    callback are logged and swallowed — SSE delivery must never break the
    pipeline.
    """
    if progress_callback is None:
        return
    try:
        result = progress_callback(event_name, data)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "progress_callback raised for event %s: %s", event_name, exc
        )


def _projected_sonnet_cost_usd() -> float:
    """Estimated USD cost of one Sonnet draft+critic pair (task 33).

    Sonnet pricing roughly: $3/MTok input, $15/MTok output. A draft is
    ~3000 input + ~2500 output; a critic is ~5000 input + ~600 output. So
    one full escalation round comes out to roughly $0.024. We return a
    conservative upper bound so the cost gate trips slightly early rather
    than late.
    """
    return 0.05  # conservative upper bound


async def generate_answers(
    mapping_ids: list[str],
    force_regenerate: bool = False,
    progress_callback=None,
    claude_client: Any = None,
) -> GenerateBatchResult:
    """Batch driver — iterates ``mapping_ids`` and produces a
    :class:`GenerateBatchResult`.

    Per-mapping behaviour
    ---------------------
    Each mapping runs through :func:`_process_one` inside a
    ``try/except AnswerPipelineError``: success appends the new answer id to
    ``answer_ids`` and increments ``generated``; ``CostBlocked`` increments
    ``cost_blocked`` and the latest Haiku draft is persisted by the gate
    (task 33 wires the gate); any other ``AnswerPipelineError`` increments
    ``failed`` and appends ``f"{mapping_id}: {detail}"`` to ``errors``.
    The batch never aborts on a per-mapping failure (R5.7, R6.5).

    Spend tracking
    --------------
    * ``batch_spend_usd`` snapshots the supplied client's
      ``cost_tracker._total`` if available, otherwise stays ``0.0`` until
      task 33 wires real cost projections.
    * ``daily_spend_usd`` reflects the ``daily_cost_log`` row for the current
      UTC date. The row is auto-inserted with zeros if missing so callers
      always see the canonical shape.

    SSE events
    ----------
    For task 30 we emit the ``persisted`` event after each successful mapping.
    The complete per-stage event list (``intent_classified``, ``draft_done``,
    …) is plumbed through ``_process_one`` in Phase 5 task 40.

    Parameters
    ----------
    mapping_ids:
        Mappings to generate. Empty list short-circuits to a zero-result
        ``GenerateBatchResult``.
    force_regenerate:
        Forwarded for API symmetry — :func:`_process_one` already deletes
        any existing answer row for the mapping before re-inserting, so the
        flag has no behavioural effect today.
    progress_callback:
        Optional ``(event_name, data_dict)`` callable invoked once per
        persisted mapping. Sync or async callables are accepted.
    claude_client:
        Optional override (used by tests). Falls back to
        :func:`claude.client.get_claude_client` only for the spend-tracking
        snapshot — ``_process_one`` resolves its own client when needed.
    """
    # Resolve the spend-tracking client once. We deliberately do *not*
    # construct a real client when the caller leaves it ``None`` so unit
    # tests that pass an explicit ``FakeClaudeClient`` to ``_process_one``
    # never trigger a credential check on import.
    tracker_client = claude_client

    success_count = 0
    fail_count = 0
    cost_blocked_count = 0
    answer_ids: list[str] = []
    errors: list[str] = []

    # Snapshot the daily spend row at batch entry. Even when ``mapping_ids``
    # is empty we want callers to see a populated ``daily_spend_usd`` field
    # so they can render the cost-ceiling chip without an extra round-trip.
    today = _today_utc()
    daily_row = await _read_or_init_daily_cost_log(today)
    initial_daily_total = float(daily_row.get("total_usd") or 0.0)

    initial_batch_total = (
        _client_total_spend(tracker_client) if tracker_client is not None else 0.0
    ) or 0.0

    for mapping_id in mapping_ids:
        try:
            record = await _process_one(
                mapping_id=mapping_id,
                claude_client=tracker_client,
                progress_callback=progress_callback,
            )
        except AnswerPipelineError as exc:
            # ``CostBlocked`` is treated as a soft failure per R9.6; the
            # latest Haiku draft is still persisted by the gate (task 33).
            from answers.errors import CostBlocked  # local import to avoid cycle

            if isinstance(exc, CostBlocked):
                cost_blocked_count += 1
                errors.append(f"{mapping_id}: cost_blocked: {exc.detail}")
                await _emit(
                    progress_callback,
                    "cost_blocked",
                    {"mapping_id": mapping_id, "detail": exc.detail},
                )
                continue

            fail_count += 1
            errors.append(f"{mapping_id}: {exc.detail}")
            logger.warning(
                "[%s] generate_answers per-mapping failure: %s",
                mapping_id, exc.detail,
            )
            continue
        except Exception as exc:
            # Unexpected errors are treated like AnswerPipelineError so a
            # bug in one mapping does not abort the batch. The full traceback
            # still goes to logs for diagnosis.
            fail_count += 1
            errors.append(f"{mapping_id}: {type(exc).__name__}: {exc}")
            logger.exception(
                "[%s] generate_answers unexpected failure", mapping_id
            )
            continue

        success_count += 1
        answer_ids.append(record.id)
        await _emit(
            progress_callback,
            "persisted",
            {"mapping_id": mapping_id, "answer_id": record.id},
        )

    # Compute final spend snapshots.
    final_batch_total = (
        _client_total_spend(tracker_client) if tracker_client is not None else None
    )
    if final_batch_total is None:
        # No real cost tracker — fall back to call-count-based proxy of 0.0
        # per task 30's documented behaviour. Task 33 replaces this with the
        # real per-call USD totals once the cost gate is wired.
        batch_spend_usd = 0.0
    else:
        batch_spend_usd = max(0.0, final_batch_total - initial_batch_total)

    # Refresh the daily row so any cost-gate-driven update lands in the
    # response. The row is guaranteed to exist after the seed at batch entry.
    refreshed_daily = await _read_or_init_daily_cost_log(today)
    daily_spend_usd = float(refreshed_daily.get("total_usd") or 0.0)
    if daily_spend_usd < initial_daily_total:  # pragma: no cover — defensive
        daily_spend_usd = initial_daily_total

    return GenerateBatchResult(
        generated=success_count,
        failed=fail_count,
        cost_blocked=cost_blocked_count,
        batch_spend_usd=round(batch_spend_usd, 6),
        daily_spend_usd=round(daily_spend_usd, 6),
        answer_ids=answer_ids,
        errors=errors,
    )


# ──────────────────────────────────────────────────────────────────────────
# Public — single-mapping regenerate wrapper (Task 31)
# ──────────────────────────────────────────────────────────────────────────


async def regenerate_answer(
    answer_id: str,
    progress_callback=None,
    claude_client: Any = None,
) -> GenerateBatchResult:
    """Re-run the medical-grade answer pipeline for an existing ``answers`` row.

    Looks up the answer row by ``answer_id``, extracts its ``mapping_id``, and
    delegates to :func:`generate_answers` with a single-element ``mapping_ids``
    list and ``force_regenerate=True``. The orchestrator's ``_process_one``
    already DELETEs any existing rows for the mapping before inserting the
    fresh draft (see step 16 in :func:`_process_one_locked`), so the previous
    answer row is automatically cleaned up — no separate delete is needed
    here.

    Parameters
    ----------
    answer_id:
        Primary-key id of the existing row in the ``answers`` table.
    progress_callback:
        Optional ``(event_name, data_dict)`` callable forwarded unchanged to
        :func:`generate_answers`. See its docstring for the SSE event list.
    claude_client:
        Optional override for the Claude transport (used by tests).

    Returns
    -------
    GenerateBatchResult
        Single-mapping batch result. ``generated == 1`` on success;
        ``failed == 1`` with the per-mapping error in ``errors`` on a soft
        failure inside ``_process_one``.

    Raises
    ------
    AnswerPipelineError
        When ``answer_id`` does not resolve to an existing ``answers`` row.
        The message is prefixed ``"answer not found: "`` per the design.
    """
    row = await database.fetch_one("answers", answer_id)
    if not row:
        raise AnswerPipelineError(
            answer_id, f"answer not found: {answer_id}"
        )

    mapping_id = row.get("mapping_id") or ""
    if not mapping_id:
        # Defensive — every persisted answer row must carry its mapping_id.
        raise AnswerPipelineError(
            answer_id,
            f"answer row {answer_id} is missing mapping_id; cannot regenerate",
        )

    return await generate_answers(
        [mapping_id],
        force_regenerate=True,
        progress_callback=progress_callback,
        claude_client=claude_client,
    )


# ──────────────────────────────────────────────────────────────────────────
# Public — admin purge action (Task 32, R10)
# ──────────────────────────────────────────────────────────────────────────
#
# Per design.md §Pipeline orchestrator → purge_answers and R10:
#
#   * Refuses unless ``confirm=True`` is passed (R10.7) — an unconfirmed
#     purge raises ``AnswerPipelineError`` with a ``"400: ..."`` detail
#     prefix that the API layer translates into HTTP 400 (Pass 2 §38).
#   * Without a subject filter, deletes every row in ``answers`` (R10.2).
#     With a subject filter, the scope is narrowed to mappings whose
#     question_paper has the matching ``subject`` via the
#     ``mappings → questions → question_papers`` join (R10.3).
#   * ``mappings_reset`` counts every mapping in scope, regardless of
#     whether an answer row existed for it (R10.4). The R10.4 update of
#     ``mappings.is_answered = 0`` is intentionally skipped here because
#     the legacy ``mappings`` schema does not carry an ``is_answered``
#     column today; the count still reflects the design contract so the
#     admin UI can render the "X mappings cleared" badge accurately.
#     TODO(R10.4-schema): add an ``is_answered INTEGER DEFAULT 0`` column
#     to ``mappings`` and switch the count to an actual UPDATE...SET when
#     the migration lands.
#   * Firestore deletion (R10.5) runs in batches of ≤500 ``mappingId``
#     values per the Firestore Admin SDK delete-batch limit. When the
#     service-account file is missing, the SQLite-side purge still runs
#     and ``firestore_documents_deleted`` is reported as ``0`` per the
#     §Failure-Mode Matrix row "Firestore unavailable during purge".


# Maximum number of Firestore documents deleted in a single batch. Matches
# the firebase-admin SDK's documented per-batch cap.
_FIRESTORE_DELETE_BATCH_SIZE = 500


def _firestore_service_account_configured() -> bool:
    """Return ``True`` when a Firebase service-account file is present.

    Used as the gate for the Firestore-side delete in :func:`purge_answers`.
    A missing file degrades the purge to a SQLite-only operation per the
    design §Failure-Mode Matrix → "Firestore unavailable during purge".
    """
    from pathlib import Path
    sa_path = getattr(settings, "firebase_service_account_path", "") or ""
    if not sa_path:
        return False
    try:
        return Path(sa_path).exists()
    except Exception:  # pragma: no cover — defensive
        return False


async def _firestore_delete_answers_by_mapping_ids(
    mapping_ids: list[str],
) -> int:
    """Delete Firestore ``answers`` documents whose ``mappingId`` is in the
    provided list, in batches of ≤ :data:`_FIRESTORE_DELETE_BATCH_SIZE`.

    Returns the count of deleted documents. Returns ``0`` (without raising)
    when the service account is not configured or any Firestore call raises,
    so the SQLite-side purge always completes per design §Failure-Mode
    Matrix.

    Firestore's ``where('mappingId', 'in', batch)`` query caps the ``in``
    clause at 30 values per call, so we paginate through ``mapping_ids`` in
    chunks of 30 inside each 500-document delete batch. The 500 ceiling is
    enforced by counting deleted documents and flushing the batch when it
    reaches the cap.
    """
    if not mapping_ids:
        return 0
    if not _firestore_service_account_configured():
        logger.info(
            "purge_answers: Firestore service account not configured; "
            "skipping Firestore-side delete (firestore_documents_deleted=0)"
        )
        return 0

    try:
        from qb.firestore_pusher import _get_firestore
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "purge_answers: failed to import firestore_pusher (%s); "
            "skipping Firestore-side delete",
            exc,
        )
        return 0

    try:
        client = _get_firestore()
    except Exception as exc:
        logger.warning(
            "purge_answers: Firestore client init failed (%s); "
            "skipping Firestore-side delete",
            exc,
        )
        return 0

    deleted = 0
    # Firestore's "in" operator caps at 30 values per query — we paginate the
    # mapping_ids list in 30-value windows and accumulate doc references into
    # a 500-cap delete batch.
    QUERY_IN_CAP = 30
    pending_refs: list = []

    def _flush(refs: list) -> int:
        if not refs:
            return 0
        batch = client.batch()
        for ref in refs:
            batch.delete(ref)
        try:
            batch.commit()
        except Exception as exc:
            logger.warning(
                "purge_answers: Firestore batch.commit failed (%s); "
                "continuing with SQLite-side purge", exc,
            )
            return 0
        return len(refs)

    try:
        for start in range(0, len(mapping_ids), QUERY_IN_CAP):
            window = mapping_ids[start : start + QUERY_IN_CAP]
            try:
                docs = (
                    client.collection("answers")
                    .where("mappingId", "in", window)
                    .get()
                )
            except Exception as exc:
                logger.warning(
                    "purge_answers: Firestore query failed for window "
                    "starting at %d (%s); skipping window",
                    start, exc,
                )
                continue

            for doc in docs:
                pending_refs.append(doc.reference)
                if len(pending_refs) >= _FIRESTORE_DELETE_BATCH_SIZE:
                    deleted += _flush(pending_refs)
                    pending_refs = []

        # Flush any tail batch.
        deleted += _flush(pending_refs)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "purge_answers: unexpected Firestore failure (%s); "
            "returning partial firestore_documents_deleted=%d",
            exc, deleted,
        )

    return deleted


async def _collect_in_scope_mapping_ids(subject: Optional[str]) -> list[str]:
    """Return mapping ids in the purge scope.

    Subject-less scope returns every mapping id; subject-scoped scope follows
    the ``mappings → questions → question_papers`` join with a filter on
    ``question_papers.subject``. Returns an empty list when no rows match.
    """
    if subject is None:
        rows = await database.fetch_all("mappings")
        return [r.get("id") for r in rows if r.get("id")]

    db = await database.get_db()
    try:
        cursor = await db.execute(
            """
            SELECT m.id
            FROM mappings m
            JOIN questions q ON m.question_id = q.id
            JOIN question_papers qp ON q.qp_id = qp.id
            WHERE qp.subject = ?
            """,
            (subject,),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()
    return [row["id"] for row in rows if row["id"]]


async def purge_answers(
    subject: Optional[str],
    confirm: bool,
) -> PurgeReport:
    """Admin action — delete answer rows by subject scope (R10).

    Refuses with a ``"400: ..."`` :class:`AnswerPipelineError` when
    ``confirm`` is falsy (R10.7). The API layer (Pass 2 §38) translates the
    400 prefix into an HTTP 400 response.

    Parameters
    ----------
    subject:
        Optional subject filter. When ``None``, every row in ``answers`` is
        deleted (R10.2). When a string, only rows whose mapping joins
        through ``mappings → questions → question_papers → subject`` are
        deleted (R10.3).
    confirm:
        Must be ``True`` to perform any deletion (R10.7).

    Returns
    -------
    PurgeReport
        Summary with ``answers_deleted``, ``mappings_reset``, and
        ``firestore_documents_deleted`` (R10.6).

    Raises
    ------
    AnswerPipelineError
        When ``confirm`` is falsy. Detail starts with ``"400: "`` so the
        API layer can return HTTP 400.
    """
    if not confirm:
        # Per R10.7: any deletion requires an explicit confirmation flag.
        # We use ``mapping_id="admin"`` as a sentinel since this action is
        # not scoped to a specific mapping; the API layer surfaces the
        # ``detail`` text to the user.
        raise AnswerPipelineError("admin", "400: confirm flag required for purge")

    # 1. Capture the in-scope mapping ids up front. We need them for both
    #    the ``mappings_reset`` count (R10.4) and the Firestore delete
    #    (R10.5). Also count the answers that will be deleted.
    in_scope_mapping_ids = await _collect_in_scope_mapping_ids(subject)
    mappings_reset = len(in_scope_mapping_ids)

    # 2. Delete the answer rows. Use a single SQL DELETE so the operation
    #    is atomic and avoids an N-row Python loop.
    if subject is None:
        # Count, then delete every row in ``answers`` (R10.2).
        answers_deleted = int(
            await database.fetch_scalar("SELECT COUNT(*) FROM answers")
        )
        await database.execute("DELETE FROM answers")
    else:
        # Subject-scoped delete (R10.3) — narrow by the mapping → question →
        # question_paper → subject join.
        scope_sql = (
            "mapping_id IN (SELECT m.id FROM mappings m "
            "JOIN questions q ON m.question_id = q.id "
            "JOIN question_papers qp ON q.qp_id = qp.id "
            "WHERE qp.subject = ?)"
        )
        answers_deleted = int(
            await database.fetch_scalar(
                f"SELECT COUNT(*) FROM answers WHERE {scope_sql}",
                (subject,),
            )
        )
        await database.execute(
            f"DELETE FROM answers WHERE {scope_sql}",
            (subject,),
        )

    # 3. R10.4 — reset ``mappings.is_answered = 0`` for every mapping in
    #    scope. The current ``mappings`` schema does not carry this column,
    #    so the reset is a no-op today; the count still reflects the
    #    design contract so the admin UI sees an accurate badge. See the
    #    TODO(R10.4-schema) comment above for the migration follow-up.

    # 4. R10.5 — Firestore-side delete of ``answers`` documents whose
    #    ``mappingId`` is in scope. Returns 0 silently when the service
    #    account is missing or any Firestore call fails (per
    #    §Failure-Mode Matrix → "Firestore unavailable during purge").
    firestore_documents_deleted = await _firestore_delete_answers_by_mapping_ids(
        in_scope_mapping_ids
    )

    logger.info(
        "purge_answers complete: subject=%r answers_deleted=%d "
        "mappings_reset=%d firestore_documents_deleted=%d",
        subject, answers_deleted, mappings_reset, firestore_documents_deleted,
    )

    return PurgeReport(
        answers_deleted=answers_deleted,
        mappings_reset=mappings_reset,
        firestore_documents_deleted=firestore_documents_deleted,
    )


__all__ = [
    "_load_mapping_context",
    "_maybe_build_kg",
    "_compute_must_know_covered",
    "_compute_sections_populated",
    "_process_one",
    "_acquire_inflight",
    "_inflight_locks",
    "generate_answers",
    "regenerate_answer",
    "purge_answers",
]
