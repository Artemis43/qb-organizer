"""Examiner critic loop: full SAQ/LAQ pass and VSAQ-lite pass (R8).

Implements the two Claude-calling critic functions documented in design §A.3
and §A.4:

- :func:`evaluate` — full critic. Scores ``structure``, ``depth``,
  ``traceability``, and ``mandatory_data`` and emits ``verdict="pass"`` only
  when all four sub-scores are ≥ 70 (R8.3). The verdict logic is enforced
  inside the system prompt; this module trusts and round-trips the parsed
  :class:`CriticVerdict` rather than re-judging in Python.
- :func:`evaluate_lite` — VSAQ-lite critic. Uses §A.4 to score only
  ``structure`` and ``mandatory_data`` (R8.7); the ``depth`` and
  ``traceability`` sub-scores are filled with ``100`` after parsing so any
  downstream consumer that computes ``score = min(sub_scores)`` behaves
  consistently across full / lite verdicts.

Both helpers reuse :class:`answers.draft.DraftContext` for their inputs and
the :func:`answers.draft._coerce_response_to_dict` pattern for response
shape coercion. Validation failures (parse error or
``pydantic.ValidationError``) raise :class:`CriticFailure` with an empty
``mapping_id`` — the orchestrator fills in the mapping id when re-raising.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import ValidationError

from answers.draft import DraftContext, _coerce_response_to_dict
from answers.errors import CriticFailure
from answers.prompts import (
    EXAMINER_CRITIC_FULL_SYSTEM_PROMPT,
    EXAMINER_CRITIC_VSAQ_LITE_SYSTEM_PROMPT,
)
from answers.schema import AnswerSections, CriticVerdict

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# User-message builder (shared by full and lite)
# ──────────────────────────────────────────────────────────────────────────


def _build_critic_user_prompt(ctx: DraftContext, draft: AnswerSections) -> str:
    """Assemble the user message that goes alongside the critic system prompt.

    The system prompts in §A.3 / §A.4 promise the critic four pieces of
    context: (a) the question, (b) the active Coverage_Mode summary, (c) the
    AnswerSections JSON draft, and (d) the must-know concept list pulled
    from the KG_Skeleton_Block. This builder lays them out under labelled
    headers so Claude can latch onto each block without ambiguity.
    """
    parts: list[str] = []

    # (a) Question + intent header — keeps the critic anchored to the same
    # focus topic the draft stage worked from.
    header_lines: list[str] = [
        "Question:",
        ctx.question_text,
        "",
        f"Question_Type: {ctx.question_type}",
        f"Intent: {ctx.intent}",
        f"Focus_Topic: {ctx.focus_topic}",
    ]
    if ctx.requested_item_count is not None:
        header_lines.append(f"Requested_Item_Count: {ctx.requested_item_count}")
    parts.append("\n".join(header_lines))

    # (b) Coverage mode summary — drives the structure_score numerator.
    coverage = ctx.coverage_mode
    sections_to_populate = list(coverage.sections_to_populate or [])
    coverage_lines: list[str] = [
        "Coverage_Mode:",
        f"  mode: {coverage.mode}",
        f"  depth_target: {coverage.depth_target}",
        f"  sections_to_populate: {sections_to_populate}",
    ]
    if coverage.per_section_caps:
        cap_items = ", ".join(
            f"{k}={v}" for k, v in coverage.per_section_caps.items()
        )
        coverage_lines.append(f"  per_section_caps: {{{cap_items}}}")
    if coverage.vsaq_disambiguation_fallback:
        coverage_lines.append("  vsaq_disambiguation_fallback: true")
    parts.append("\n".join(coverage_lines))

    # (c) Draft AnswerSections JSON — emit the canonical compact shape with
    # ``exclude_none=True`` so omitted sections do not show up as ``null``.
    parts.append(
        "AnswerSections_Draft:\n" + draft.model_dump_json(exclude_none=True)
    )

    # (d) Must-know concept list — the missing_must_know array in the verdict
    # is populated by checking each of these against bullet text. Render as a
    # simple bullet list. We always emit the header so the prompt schema is
    # consistent across calls (empty list → "  (none)").
    must_know = list(ctx.kg_skeleton.must_know_concept_ids) if ctx.kg_skeleton else []
    if must_know:
        must_lines: list[str] = ["Must_Know_Concepts:"]
        for cid in must_know:
            must_lines.append(f"  - {cid}")
        parts.append("\n".join(must_lines))
    else:
        parts.append("Must_Know_Concepts:\n  (none)")

    return "\n\n".join(parts)


# ──────────────────────────────────────────────────────────────────────────
# Validation helper
# ──────────────────────────────────────────────────────────────────────────


def _validate_as_critic_verdict(parsed: Optional[dict[str, Any]]) -> Optional[CriticVerdict]:
    """Validate a parsed dict as :class:`CriticVerdict`.

    Returns ``None`` on missing input or ``pydantic.ValidationError``. The
    callers translate that ``None`` into a :class:`CriticFailure`.
    """
    if parsed is None:
        return None
    try:
        return CriticVerdict.model_validate(parsed)
    except ValidationError as exc:
        logger.debug("critic.evaluate validation failed: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────
# Public API — full critic (§A.3)
# ──────────────────────────────────────────────────────────────────────────


async def evaluate(
    ctx: DraftContext,
    draft: AnswerSections,
    claude_client: Any = None,
    model: Optional[str] = None,
) -> CriticVerdict:
    """Run the full SAQ/LAQ examiner critic over a draft (R8.3).

    Calls Claude with :data:`EXAMINER_CRITIC_FULL_SYSTEM_PROMPT`, parses the
    JSON response, and validates it as :class:`CriticVerdict`. On JSON parse
    error or ``pydantic.ValidationError`` raises :class:`CriticFailure` with
    an empty ``mapping_id`` (the orchestrator fills it in).

    The verdict-vs-score logic (verdict="pass" iff all four sub-scores ≥ 70)
    is enforced inside the system prompt. This function trusts and
    round-trips whatever the parsed verdict says rather than re-judging in
    Python — keeping the rubric in ONE place (the prompt) avoids drift.
    """
    if claude_client is None:
        # Lazy import so importing ``answers.critic`` does not require a
        # configured ``ANTHROPIC_API_KEY``; only ``evaluate`` does.
        from claude.client import get_claude_client  # noqa: WPS433
        claude_client = get_claude_client()

    if not model:
        from config import settings  # noqa: WPS433
        model = settings.haiku_model

    user_message = _build_critic_user_prompt(ctx, draft)

    raw_response = await claude_client.request(
        messages=[{"role": "user", "content": user_message}],
        system=EXAMINER_CRITIC_FULL_SYSTEM_PROMPT,
        model=model,
        max_tokens=2048,
        task_type="answer_critic",
        subject=ctx.subject,
    )
    parsed, _ = _coerce_response_to_dict(raw_response)
    verdict = _validate_as_critic_verdict(parsed)
    if verdict is None:
        raise CriticFailure(
            mapping_id="",
            detail="Critic returned malformed verdict JSON",
        )
    return verdict


# ──────────────────────────────────────────────────────────────────────────
# Public API — VSAQ-lite critic (§A.4)
# ──────────────────────────────────────────────────────────────────────────


async def evaluate_lite(
    ctx: DraftContext,
    draft: AnswerSections,
    claude_client: Any = None,
    model: Optional[str] = None,
) -> CriticVerdict:
    """Run the VSAQ-lite examiner critic over a draft (R8.7).

    The lite prompt scores only ``structure`` and ``mandatory_data`` and is
    instructed to emit ``depth_score`` and ``traceability_score`` as JSON
    ``null``. After the JSON parse step those two fields are filled with
    ``100`` so any downstream code computing ``score = min(sub_scores)``
    treats the verdict consistently with the full critic shape.

    Verdict logic (``"pass"`` iff structure ≥ 70 AND mandatory_data ≥ 70) is
    enforced inside the prompt; the parsed verdict is round-tripped as-is.
    """
    if claude_client is None:
        from claude.client import get_claude_client  # noqa: WPS433
        claude_client = get_claude_client()

    if not model:
        from config import settings  # noqa: WPS433
        model = settings.haiku_model

    user_message = _build_critic_user_prompt(ctx, draft)

    raw_response = await claude_client.request(
        messages=[{"role": "user", "content": user_message}],
        system=EXAMINER_CRITIC_VSAQ_LITE_SYSTEM_PROMPT,
        model=model,
        max_tokens=2048,
        task_type="answer_critic",
        subject=ctx.subject,
    )
    parsed, _ = _coerce_response_to_dict(raw_response)

    # Lite normalization — fill the prompt's intentionally-null sub-scores
    # with 100 BEFORE pydantic validation, since CriticVerdict declares them
    # as required ints. This preserves the design AC that downstream callers
    # using ``score = min(sub_scores)`` still see a sensible value.
    if isinstance(parsed, dict):
        if parsed.get("depth_score") is None:
            parsed["depth_score"] = 100
        if parsed.get("traceability_score") is None:
            parsed["traceability_score"] = 100

    verdict = _validate_as_critic_verdict(parsed)
    if verdict is None:
        raise CriticFailure(
            mapping_id="",
            detail="Critic returned malformed verdict JSON",
        )
    return verdict


__all__ = [
    "evaluate",
    "evaluate_lite",
]
