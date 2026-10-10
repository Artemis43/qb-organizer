"""Unit tests for ``answers.intent.classify_question`` (R2.1, R2.2, R2.3, R2.4, R2.5).

Exercises the four documented response paths through the Haiku-calling
classifier and the process-local cache:

1. Valid Claude response → ``QuestionIntent`` with the parsed intent label.
2. Invalid label (e.g. ``"explain_in_depth"``) → coerced to ``"discuss"`` with
   ``intent_confidence_low=True`` per design §Failure-Mode Matrix row
   "Intent classifier returns invalid label".
3. Malformed (non-JSON / non-dict) Claude response → same fallback as (2).
4. Same ``question_text`` called twice → second call hits the
   ``_INTENT_CACHE`` and does NOT invoke the fake client a second time.

All tests use the ``fake_claude`` fixture from ``tests/answers/conftest.py``
so we never make real API calls.
"""
from __future__ import annotations

import pytest

from answers import intent as intent_module
from answers.intent import classify_question
from answers.prompts import INTENT_CLASSIFIER_PROMPT
from answers.schema import QuestionIntent


# ──────────────────────────────────────────────────────────────────────────
# Test setup: clear the process-local cache between tests so each scenario
# starts with a clean slate.
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_intent_cache():
    intent_module._INTENT_CACHE.clear()
    yield
    intent_module._INTENT_CACHE.clear()


# ──────────────────────────────────────────────────────────────────────────
# (1) Valid response path
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_question_valid_response_returns_parsed_intent(fake_claude):
    """A well-formed Claude response → parsed ``QuestionIntent`` (R2.1, R2.2)."""
    fake_claude.register_default(
        {
            "intent": "mechanism_of",
            "focus_topic": "diabetic ketoacidosis",
            "requested_item_count": None,
            "intent_confidence_low": False,
        }
    )

    result = await classify_question(
        question_text="Discuss the pathophysiology of diabetic ketoacidosis.",
        question_type="LAQ",
        claude_client=fake_claude,
    )

    assert isinstance(result, QuestionIntent)
    assert result.intent == "mechanism_of"
    assert result.focus_topic == "diabetic ketoacidosis"
    assert result.requested_item_count is None
    assert result.intent_confidence_low is False

    # The classifier is invoked exactly once with the verbatim system prompt.
    assert fake_claude.call_count == 1
    call = fake_claude.calls[0]
    assert call["system"] == INTENT_CLASSIFIER_PROMPT
    # The user message should carry both the question stem and its type so
    # the classifier can disambiguate VSAQ vs LAQ phrasings.
    assert "diabetic ketoacidosis" in call["user_text"]
    assert "LAQ" in call["user_text"]


@pytest.mark.asyncio
async def test_classify_question_valid_response_preserves_requested_count(fake_claude):
    """Numeric ``requested_item_count`` propagates intact (R2.3)."""
    fake_claude.register_default(
        {
            "intent": "enumerate",
            "focus_topic": "high anion-gap metabolic acidosis",
            "requested_item_count": 4,
            "intent_confidence_low": False,
        }
    )

    result = await classify_question(
        question_text="Enumerate 4 causes of high anion-gap metabolic acidosis.",
        question_type="VSAQ",
        claude_client=fake_claude,
    )

    assert result.intent == "enumerate"
    assert result.requested_item_count == 4
    assert result.intent_confidence_low is False


# ──────────────────────────────────────────────────────────────────────────
# (2) Invalid label path
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_question_invalid_label_falls_back_to_discuss(fake_claude):
    """Unknown intent label → coerced to ``discuss`` with low-confidence flag (R2.4)."""
    fake_claude.register_default(
        {
            "intent": "explain_in_depth",  # NOT in INTENT_LABELS_13
            "focus_topic": "asthma",
            "requested_item_count": None,
            "intent_confidence_low": False,
        }
    )

    result = await classify_question(
        question_text="Explain in depth the management of asthma.",
        question_type="LAQ",
        claude_client=fake_claude,
    )

    assert result.intent == "discuss"
    assert result.intent_confidence_low is True
    # focus_topic still propagates from the (otherwise-valid) JSON response.
    assert result.focus_topic == "asthma"


# ──────────────────────────────────────────────────────────────────────────
# (3) Malformed JSON / non-dict path
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_question_malformed_text_response_falls_back(fake_claude):
    """Claude returns wrapped raw text → coerced to ``discuss`` (R2.4)."""
    # ``ClaudeClient.request`` wraps non-JSON responses as ``{"text": "<raw>"}``.
    fake_claude.register_default({"text": "this is not JSON, just prose"})

    result = await classify_question(
        question_text="What is sepsis?",
        question_type="VSAQ",
        claude_client=fake_claude,
    )

    assert result.intent == "discuss"
    assert result.intent_confidence_low is True


@pytest.mark.asyncio
async def test_classify_question_non_dict_response_falls_back(fake_claude):
    """A non-dict, non-string response shape → coerced to ``discuss`` (R2.4)."""
    fake_claude.register_default(["not", "a", "dict"])

    result = await classify_question(
        question_text="Define hypertension.",
        question_type="VSAQ",
        claude_client=fake_claude,
    )

    assert result.intent == "discuss"
    assert result.intent_confidence_low is True


@pytest.mark.asyncio
async def test_classify_question_claude_exception_falls_back_to_discuss(fake_claude):
    """An exception bubbling out of the Claude call → fallback ``discuss`` with truncated focus."""
    # ``FakeClaudeClient.request`` raises ``KeyError`` when no response is
    # registered — exercise the broad except path in ``classify_question``.
    long_stem = "A" * 200
    result = await classify_question(
        question_text=long_stem,
        question_type="LAQ",
        claude_client=fake_claude,
    )

    assert result.intent == "discuss"
    assert result.intent_confidence_low is True
    # The fallback truncates the focus topic to the first 60 chars of the stem.
    assert result.focus_topic == long_stem[:60]


# ──────────────────────────────────────────────────────────────────────────
# (4) Cache behaviour
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_question_caches_by_question_text(fake_claude):
    """Second call with the same question_text hits the cache (R2.5)."""
    fake_claude.register_default(
        {
            "intent": "define",
            "focus_topic": "myocardial infarction",
            "requested_item_count": None,
            "intent_confidence_low": False,
        }
    )

    stem = "Define myocardial infarction."

    first = await classify_question(stem, "VSAQ", claude_client=fake_claude)
    second = await classify_question(stem, "VSAQ", claude_client=fake_claude)

    assert first == second
    # The cache hit means the fake client was invoked exactly once, not twice.
    assert fake_claude.call_count == 1


@pytest.mark.asyncio
async def test_classify_question_cache_does_not_collide_across_distinct_stems(fake_claude):
    """Distinct ``question_text`` values produce distinct cache entries."""
    fake_claude.register_default(
        {
            "intent": "discuss",
            "focus_topic": "topic",
            "requested_item_count": None,
            "intent_confidence_low": False,
        }
    )

    await classify_question("Question one.", "SAQ", claude_client=fake_claude)
    await classify_question("Question two.", "SAQ", claude_client=fake_claude)

    # Two different stems → two cache slots → two Claude invocations.
    assert fake_claude.call_count == 2
