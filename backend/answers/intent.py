"""Question intent classifier and pure label-coercion helpers (R2).

This module exposes:

- ``INTENT_LABELS_13``: the closed 13-label set defined in R2.1.
- ``coerce_intent_label``: a pure function that maps raw classifier output
  (which may be malformed JSON shape, contain unknown labels, or be missing
  fields) into a valid :class:`~answers.schema.QuestionIntent` per the
  Failure-Mode Matrix row "Intent classifier returns invalid label" in
  ``design.md``.
- ``classify_question``: the async Haiku-calling classifier (task 23). Calls
  Claude with ``INTENT_CLASSIFIER_PROMPT`` from ``answers.prompts``, parses
  the JSON response, validates it as ``QuestionIntent``, and routes every
  failure path through ``coerce_intent_label``. Results are cached for the
  process lifetime keyed by SHA-256 of the question stem.

Design references
-----------------
- design.md §A.1 (intent classifier prompt)
- design.md §Failure-Mode Matrix → "Intent classifier returns invalid label"
- design.md §Failure-Mode Matrix → "Intent classifier total failure"
- design.md §Property 6: Intent label closure
- requirements.md R2.1, R2.2, R2.3, R2.4, R2.5
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from answers.schema import QuestionIntent

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Closed label set (R2.1)
# ──────────────────────────────────────────────────────────────────────────

#: The 13 valid intent labels per R2.1. The coverage resolver and downstream
#: prompt builders index into label-keyed tables; any label outside this set
#: is coerced to ``"discuss"`` with ``intent_confidence_low=True``.
INTENT_LABELS_13: frozenset[str] = frozenset(
    {
        "define",
        "enumerate",
        "classify",
        "discuss",
        "mechanism_of",
        "management_of",
        "complications_of",
        "features_of",
        "investigations_of",
        "etiology_of",
        "prognosis_of",
        "differentiate",
        "compare",
    }
)


# ──────────────────────────────────────────────────────────────────────────
# Pure coercion helper
# ──────────────────────────────────────────────────────────────────────────


def coerce_intent_label(raw: dict[str, Any]) -> QuestionIntent:
    """Coerce raw classifier output into a valid :class:`QuestionIntent`.

    Rules (kept pure — no I/O, no logging side-effects):

    - ``intent``: if ``raw["intent"]`` is a string in :data:`INTENT_LABELS_13`,
      use it as-is. Otherwise (missing, ``None``, non-string, or unknown label)
      coerce to ``"discuss"`` and force ``intent_confidence_low = True`` per
      design §Failure-Mode Matrix row "Intent classifier returns invalid label".
    - ``focus_topic``: read ``raw["focus_topic"]`` if it is a non-empty string,
      else default to ``""``.
    - ``requested_item_count``: read ``raw["requested_item_count"]`` only if it
      is a positive ``int`` (booleans are rejected — Python's ``bool`` is an
      ``int`` subclass and we want a real integer). Otherwise ``None``.
    - ``intent_confidence_low``: ``True`` if we coerced an invalid label.
      Otherwise, if ``raw["intent_confidence_low"]`` is a real ``bool``, use
      its value (so an explicit ``True`` propagates and an explicit ``False``
      stays ``False``). Anything else → ``False``.

    The function never raises on shape mismatches — that is the whole point of
    the coercion layer. ``classify_question`` (task 23) will route malformed
    JSON, ``pydantic.ValidationError`` and unknown labels through this helper.
    """
    # ── intent label ────────────────────────────────────────────────────
    raw_intent = raw.get("intent")
    if isinstance(raw_intent, str) and raw_intent in INTENT_LABELS_13:
        intent = raw_intent
        coerced_invalid_label = False
    else:
        intent = "discuss"
        coerced_invalid_label = True

    # ── focus_topic ─────────────────────────────────────────────────────
    raw_focus = raw.get("focus_topic")
    focus_topic = raw_focus if isinstance(raw_focus, str) and raw_focus else ""

    # ── requested_item_count ────────────────────────────────────────────
    raw_count = raw.get("requested_item_count")
    if (
        isinstance(raw_count, int)
        and not isinstance(raw_count, bool)
        and raw_count > 0
    ):
        requested_item_count: int | None = raw_count
    else:
        requested_item_count = None

    # ── intent_confidence_low ───────────────────────────────────────────
    if coerced_invalid_label:
        intent_confidence_low = True
    else:
        raw_flag = raw.get("intent_confidence_low")
        intent_confidence_low = raw_flag if isinstance(raw_flag, bool) else False

    return QuestionIntent(
        intent=intent,
        focus_topic=focus_topic,
        requested_item_count=requested_item_count,
        intent_confidence_low=intent_confidence_low,
    )


# ──────────────────────────────────────────────────────────────────────────
# Stateful Claude-calling classifier (Task 23)
# ──────────────────────────────────────────────────────────────────────────

#: Process-local cache keyed by SHA-256(question_text) → QuestionIntent.
#: A second classification of the same stem within a process lifetime returns
#: the cached result without invoking Claude (R2 cost / latency optimization).
_INTENT_CACHE: dict[str, QuestionIntent] = {}


def _cache_key(question_text: str) -> str:
    """Compute the SHA-256 cache key for a question stem."""
    return hashlib.sha256(question_text.encode("utf-8")).hexdigest()


def _coerce_response_to_dict(raw: Any) -> dict[str, Any]:
    """Best-effort conversion of Claude's response into a JSON-shaped dict.

    The production ``ClaudeClient.request`` returns either a parsed ``dict``
    (when the response body parsed as JSON) or ``{"text": "<raw>"}`` when JSON
    parsing failed. The fake client may return strings directly. This helper
    flattens those shapes back to a plain ``dict`` whose keys match the
    intent classifier's JSON schema; anything else returns ``{}`` so the
    coercion layer falls back to ``discuss``.
    """
    if isinstance(raw, dict):
        # ``ClaudeClient.request`` wraps non-JSON responses as {"text": "..."}.
        # Try parsing that text as JSON before giving up.
        if set(raw.keys()) == {"text"} and isinstance(raw.get("text"), str):
            try:
                parsed = json.loads(raw["text"])
            except (TypeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def classify_question(
    question_text: str,
    question_type: str,
    claude_client: Any = None,
) -> QuestionIntent:
    """Classify a question stem to extract intent, focus_topic, count, and confidence flag.

    Calls Haiku with :data:`answers.prompts.INTENT_CLASSIFIER_PROMPT` as the
    system prompt and a short user message containing the question stem and
    question type. Parses the JSON response and validates it as a
    :class:`QuestionIntent`. On any failure path — malformed JSON, missing
    fields, ``pydantic.ValidationError``, unknown label, or any exception
    bubbling out of the Claude call — routes the result through
    :func:`coerce_intent_label`, which falls back to ``intent="discuss"`` and
    ``intent_confidence_low=True`` per design §Failure-Mode Matrix rows
    "Intent classifier returns invalid label" and "Intent classifier total
    failure".

    Results are cached for the lifetime of the process in :data:`_INTENT_CACHE`,
    keyed by ``hashlib.sha256(question_text.encode("utf-8")).hexdigest()``. A
    second call with the same ``question_text`` returns the cached
    ``QuestionIntent`` without invoking Claude.

    Parameters
    ----------
    question_text:
        The raw exam question stem.
    question_type:
        One of ``"VSAQ" | "SAQ" | "LAQ"`` — included in the user message so the
        classifier can disambiguate stems whose intent depends on the question
        format.
    claude_client:
        Optional client override (used in tests). Defaults to
        :func:`claude.client.get_claude_client`.

    Returns
    -------
    QuestionIntent
        A validated intent record. Always returns — never raises.
    """
    # ── cache lookup ────────────────────────────────────────────────────
    key = _cache_key(question_text)
    cached = _INTENT_CACHE.get(key)
    if cached is not None:
        return cached

    # ── client default ──────────────────────────────────────────────────
    if claude_client is None:
        # Imported lazily so importing ``answers.intent`` does not require a
        # configured ANTHROPIC_API_KEY (only ``classify_question`` does).
        from claude.client import get_claude_client  # noqa: WPS433
        claude_client = get_claude_client()

    # ── lazy imports for prompt + model id ──────────────────────────────
    from answers.prompts import INTENT_CLASSIFIER_PROMPT  # noqa: WPS433
    from config import settings  # noqa: WPS433

    user_message = (
        f"Question_Type: {question_type}\n"
        f"Question:\n{question_text}"
    )

    # ── Claude call (wrapped) ───────────────────────────────────────────
    try:
        raw_response = await claude_client.request(
            messages=[{"role": "user", "content": user_message}],
            system=INTENT_CLASSIFIER_PROMPT,
            model=settings.haiku_model,
            max_tokens=512,
            task_type="intent_classify",
        )
        parsed = _coerce_response_to_dict(raw_response)
        result = coerce_intent_label(parsed)
    except Exception as exc:  # noqa: BLE001 — Failure-Mode Matrix row "total failure"
        logger.warning(
            "intent.classify_question fell back to discuss (reason=%s, type=%s)",
            type(exc).__name__,
            question_type,
        )
        result = coerce_intent_label(
            {
                "intent": "discuss",
                "focus_topic": question_text[:60],
                "intent_confidence_low": True,
            }
        )

    # ── cache + return ──────────────────────────────────────────────────
    _INTENT_CACHE[key] = result
    return result


__all__ = [
    "INTENT_LABELS_13",
    "coerce_intent_label",
    "classify_question",
]
