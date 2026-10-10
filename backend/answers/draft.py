"""Draft generator wrapping the Claude call and AnswerSections JSON parsing (R4).

Implements the draft stage of the medical-grade answer pipeline:

- :class:`DraftContext` — a dataclass bundling the inputs that
  :func:`build_draft_user_prompt` consumes (question stem, intent, focus
  topic, coverage mode, KG skeleton, retrieved context, optional mnemonic
  hint, subject).
- :func:`generate_draft` — async function that calls Claude with the verbatim
  ``DRAFT_GENERATOR_SYSTEM_PROMPT`` from §A.2, parses the JSON response, and
  validates it as an :class:`AnswerSections`. On parse OR validation failure
  it retries exactly once with the strict-JSON addendum from
  §Failure-Mode Matrix → "Malformed JSON from draft Claude call". If the
  Claude transport exposes ``_repair_truncated_json``, the second-call raw
  text is run through that helper before raising.

A second failure raises :class:`SchemaViolation` with
``mapping_id=""`` (the orchestrator fills the mapping id when re-raising).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import ValidationError

from answers.errors import SchemaViolation
from answers.prompts import DRAFT_GENERATOR_SYSTEM_PROMPT, build_draft_user_prompt
from answers.schema import AnswerSections, CoverageMode, KGSkeleton, RetrievedContext

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Strict-JSON addendum (design §Failure-Mode Matrix → Malformed JSON ...)
# ──────────────────────────────────────────────────────────────────────────

#: Appended to the user message on the retry attempt. Phrased as a hard
#: prohibition because the Failure-Mode Matrix entry calls for "no prose, no
#: markdown fences, no explanations" on the second call.
STRICT_JSON_ADDENDUM = (
    "\n\nOUTPUT ONLY JSON, NO PROSE. NO MARKDOWN FENCES. NO EXPLANATIONS."
)


# ──────────────────────────────────────────────────────────────────────────
# DraftContext (design §A.2 inputs to build_draft_user_prompt)
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class DraftContext:
    """Bundle of inputs the draft stage feeds into ``build_draft_user_prompt``.

    Mirrors the per-mapping pipeline state at the moment the orchestrator is
    ready to call Claude: question stem and type, intent classification
    output, the resolved coverage mode, the KG skeleton (or an empty one),
    optional retrieved context, and an optional mnemonic hint pulled from
    :func:`answers.prompts.find_mnemonic`. ``subject`` flows through to
    Claude cost tracking.
    """

    question_text: str
    question_type: str
    intent: str
    focus_topic: str
    requested_item_count: Optional[int]
    coverage_mode: CoverageMode
    kg_skeleton: KGSkeleton
    retrieved_context: Optional[RetrievedContext]
    mnemonic_hint: Optional[dict] = None
    subject: str = ""


# ──────────────────────────────────────────────────────────────────────────
# Response coercion helper (mirrors intent._coerce_response_to_dict)
# ──────────────────────────────────────────────────────────────────────────


def _coerce_response_to_dict(raw: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Best-effort conversion of Claude's response into a JSON-shaped dict.

    Returns a ``(parsed_dict_or_None, underlying_text_or_None)`` tuple:

    - If ``raw`` is a real ``dict`` whose keys go beyond ``{"text"}``, it is
      returned verbatim (``ClaudeClient.request`` returns parsed JSON in this
      shape on the happy path).
    - If ``raw`` is the wrapper shape ``{"text": "<raw>"}`` returned by
      ``ClaudeClient.request`` when the body did not parse as JSON, the
      embedded text is JSON-parsed. The wrapper text is also returned so the
      caller can hand it to ``_repair_truncated_json`` on the retry path.
    - If ``raw`` is a plain string, the same JSON-parse-or-bail logic applies.
    - Anything else returns ``(None, None)``.

    Returning ``None`` for the dict signals "malformed" — the caller routes
    that into the strict-JSON retry path.
    """
    if isinstance(raw, dict):
        if set(raw.keys()) == {"text"} and isinstance(raw.get("text"), str):
            text = raw["text"]
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                return None, text
            return (parsed if isinstance(parsed, dict) else None), text
        return raw, None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None, raw
        return (parsed if isinstance(parsed, dict) else None), raw
    return None, None


def _validate_as_answer_sections(parsed: Optional[dict[str, Any]]) -> Optional[AnswerSections]:
    """Validate a parsed dict as :class:`AnswerSections`.

    Returns ``None`` when validation fails OR when ``parsed`` is ``None``.
    Empty/structurally-blank dicts still produce an ``AnswerSections``
    (collapsed to all-None by ``_collapse_empty``); the orchestrator's
    mandatory-data validator and critic catch those downstream.
    """
    if parsed is None:
        return None
    try:
        return AnswerSections.model_validate(parsed)
    except ValidationError as exc:
        logger.debug("draft.generate_draft validation failed: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────


async def generate_draft(
    ctx: DraftContext,
    model: str,
    claude_client: Any = None,
) -> AnswerSections:
    """Call Claude with the draft system prompt, parse JSON, validate as ``AnswerSections``.

    Failure handling per design §Failure-Mode Matrix row "Malformed JSON from
    draft Claude call":

    1. First attempt fails (parse OR validation) → retry once with
       :data:`STRICT_JSON_ADDENDUM` appended to the user message.
    2. Second attempt also fails → if the Claude transport exposes
       ``_repair_truncated_json``, attempt to repair the underlying raw text
       and validate the repaired dict.
    3. Still failing → raise :class:`SchemaViolation` with empty
       ``mapping_id`` (the orchestrator re-raises with the mapping id filled
       in for structured logging).
    """
    if claude_client is None:
        # Imported lazily so importing ``answers.draft`` does not require a
        # configured ``ANTHROPIC_API_KEY`` (only ``generate_draft`` does).
        from claude.client import get_claude_client  # noqa: WPS433
        claude_client = get_claude_client()

    user_message = build_draft_user_prompt(
        question_text=ctx.question_text,
        question_type=ctx.question_type,
        intent=ctx.intent,
        focus_topic=ctx.focus_topic,
        requested_item_count=ctx.requested_item_count,
        coverage_mode=ctx.coverage_mode,
        kg_skeleton=ctx.kg_skeleton,
        retrieved_context=ctx.retrieved_context,
        mnemonic_hint=ctx.mnemonic_hint,
    )

    # ── Attempt 1 ───────────────────────────────────────────────────────
    raw_response = await claude_client.request(
        messages=[{"role": "user", "content": user_message}],
        system=DRAFT_GENERATOR_SYSTEM_PROMPT,
        model=model,
        max_tokens=8192,
        task_type="answer_draft",
        subject=ctx.subject,
    )
    parsed, _ = _coerce_response_to_dict(raw_response)
    sections = _validate_as_answer_sections(parsed)
    if sections is not None:
        return sections

    # ── Attempt 2 — strict-JSON retry ───────────────────────────────────
    retry_message = user_message + STRICT_JSON_ADDENDUM
    raw_response_2 = await claude_client.request(
        messages=[{"role": "user", "content": retry_message}],
        system=DRAFT_GENERATOR_SYSTEM_PROMPT,
        model=model,
        max_tokens=8192,
        task_type="answer_draft_retry",
        subject=ctx.subject,
    )
    parsed_2, raw_text_2 = _coerce_response_to_dict(raw_response_2)
    sections = _validate_as_answer_sections(parsed_2)
    if sections is not None:
        return sections

    # ── Last-ditch: try the truncated-JSON repair helper if available ──
    repair = getattr(claude_client, "_repair_truncated_json", None)
    if callable(repair) and raw_text_2:
        try:
            repaired = repair(raw_text_2)
        except Exception as exc:  # noqa: BLE001 — defensive; helper is best-effort
            logger.debug("draft.generate_draft repair helper raised: %s", exc)
            repaired = None
        sections = _validate_as_answer_sections(repaired)
        if sections is not None:
            return sections

    raise SchemaViolation(
        mapping_id="",
        detail="Claude returned malformed JSON twice",
    )


__all__ = [
    "DraftContext",
    "STRICT_JSON_ADDENDUM",
    "generate_draft",
]
