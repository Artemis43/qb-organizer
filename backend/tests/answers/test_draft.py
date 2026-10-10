"""Unit tests for ``answers.draft.generate_draft`` (R1.1, R1.9, R4.1–R4.5).

Exercises the four documented response paths through the draft stage:

1. Valid Claude JSON → returns parsed ``AnswerSections``.
2. First call malformed, second call valid → returns parsed
   ``AnswerSections`` and the fake client is invoked exactly twice.
3. Both calls malformed → raises :class:`SchemaViolation`.
4. The retry user message carries the strict-JSON addendum, so the second
   call's ``user_text`` contains ``"OUTPUT ONLY JSON"``.

All tests use the ``fake_claude`` fixture from ``tests/answers/conftest.py``
so we never make real API calls.
"""
from __future__ import annotations

import pytest

from answers.draft import DraftContext, generate_draft
from answers.errors import SchemaViolation
from answers.prompts import DRAFT_GENERATOR_SYSTEM_PROMPT, build_draft_user_prompt
from answers.schema import AnswerSections, CoverageMode, KGSkeleton


# ──────────────────────────────────────────────────────────────────────────
# Test fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def coverage_mode() -> CoverageMode:
    """Minimal LAQ-style coverage mode with two sections so the user prompt
    contains a non-empty `Populate ONLY these sections:` line."""
    return CoverageMode(
        mode="laq_full",
        sections_to_populate=["definition", "pathophysiology"],
        per_section_caps={"pathophysiology": 6},
        depth_target="laq",
        vsaq_disambiguation_fallback=False,
    )


@pytest.fixture
def empty_kg_skeleton() -> KGSkeleton:
    """An empty KG skeleton — exercises the path where the KG block is omitted
    from the user prompt (R6.5 vector-only fallback)."""
    return KGSkeleton(
        concept_ids=[],
        relation_ids=[],
        must_know_concept_ids=[],
        skeleton_block_text="",
        used=False,
    )


@pytest.fixture
def draft_ctx(coverage_mode, empty_kg_skeleton) -> DraftContext:
    return DraftContext(
        question_text="Discuss the pathophysiology of diabetic ketoacidosis.",
        question_type="LAQ",
        intent="mechanism_of",
        focus_topic="diabetic ketoacidosis",
        requested_item_count=None,
        coverage_mode=coverage_mode,
        kg_skeleton=empty_kg_skeleton,
        retrieved_context=None,
        mnemonic_hint=None,
        subject="General Medicine",
    )


@pytest.fixture
def valid_sections_payload() -> dict:
    """A minimal but valid ``AnswerSections`` JSON payload."""
    return {
        "definition": (
            "Diabetic ketoacidosis (DKA) is an acute metabolic emergency "
            "characterized by hyperglycemia, ketonemia, and high anion-gap "
            "metabolic acidosis."
        ),
        "pathophysiology": [
            {
                "step_index": 1,
                "text": "Insulin deficiency removes inhibition of hormone-sensitive lipase, triggering lipolysis",
                "source_chunk_ids": ["chunk_001"],
                "kg_concept_ids": ["c_insulin_deficiency"],
            },
            {
                "step_index": 2,
                "text": "Hepatic ketogenesis produces β-hydroxybutyrate, generating high anion-gap acidosis",
                "source_chunk_ids": ["chunk_002"],
                "kg_concept_ids": ["c_ketogenesis"],
            },
        ],
        "mnemonics": {},
    }


def _fingerprint_for(ctx: DraftContext, addendum: str = "") -> str:
    """Compute the FakeClaudeClient fingerprint for a given draft user message."""
    from tests.answers.fakes import FakeClaudeClient

    user_text = build_draft_user_prompt(
        question_text=ctx.question_text,
        question_type=ctx.question_type,
        intent=ctx.intent,
        focus_topic=ctx.focus_topic,
        requested_item_count=ctx.requested_item_count,
        coverage_mode=ctx.coverage_mode,
        kg_skeleton=ctx.kg_skeleton,
        retrieved_context=ctx.retrieved_context,
        mnemonic_hint=ctx.mnemonic_hint,
    ) + addendum
    return FakeClaudeClient.fingerprint(
        system=DRAFT_GENERATOR_SYSTEM_PROMPT,
        user_text=user_text,
    )


# ──────────────────────────────────────────────────────────────────────────
# (1) Valid first-call response
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_draft_valid_response_returns_parsed_sections(
    fake_claude, draft_ctx, valid_sections_payload
):
    """A well-formed Claude JSON response → parsed ``AnswerSections`` (R4.1)."""
    fake_claude.register_default(valid_sections_payload)

    result = await generate_draft(
        ctx=draft_ctx,
        model="claude-haiku-4-5-20251001",
        claude_client=fake_claude,
    )

    assert isinstance(result, AnswerSections)
    assert result.definition is not None and "DKA" in result.definition
    assert result.pathophysiology is not None
    assert len(result.pathophysiology) == 2
    assert result.pathophysiology[0].step_index == 1

    # Single Claude call on the happy path.
    assert fake_claude.call_count == 1
    call = fake_claude.calls[0]
    # The verbatim §A.2 prompt must reach the transport unchanged.
    assert call["system"] == DRAFT_GENERATOR_SYSTEM_PROMPT
    # The user message carries the question stem and the populate list.
    assert "diabetic ketoacidosis" in call["user_text"]
    assert "Populate ONLY these sections" in call["user_text"]
    # The strict-JSON addendum MUST NOT leak onto the first call.
    assert "OUTPUT ONLY JSON" not in call["user_text"]


# ──────────────────────────────────────────────────────────────────────────
# (2) First call malformed, second call valid
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_draft_retries_once_on_malformed_then_succeeds(
    fake_claude, draft_ctx, valid_sections_payload
):
    """Malformed first response → strict-JSON retry succeeds (R4.4 retry budget)."""
    first_fp = _fingerprint_for(draft_ctx, addendum="")
    second_fp = _fingerprint_for(
        draft_ctx,
        addendum="\n\nOUTPUT ONLY JSON, NO PROSE. NO MARKDOWN FENCES. NO EXPLANATIONS.",
    )

    # Wrapper shape returned by ``ClaudeClient.request`` when JSON parse fails.
    fake_claude.register(first_fp, {"text": "not json, just prose"})
    fake_claude.register(second_fp, valid_sections_payload)

    result = await generate_draft(
        ctx=draft_ctx,
        model="claude-haiku-4-5-20251001",
        claude_client=fake_claude,
    )

    assert isinstance(result, AnswerSections)
    assert result.definition is not None
    # Exactly two Claude calls — one malformed, one strict-JSON retry.
    assert fake_claude.call_count == 2


# ──────────────────────────────────────────────────────────────────────────
# (3) Both calls malformed → SchemaViolation
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_draft_raises_schema_violation_after_two_malformed(
    fake_claude, draft_ctx
):
    """Two malformed Claude responses → :class:`SchemaViolation` (R1.9)."""
    fake_claude.register_default({"text": "still not JSON"})

    with pytest.raises(SchemaViolation) as excinfo:
        await generate_draft(
            ctx=draft_ctx,
            model="claude-haiku-4-5-20251001",
            claude_client=fake_claude,
        )

    assert "malformed JSON twice" in excinfo.value.detail
    # Both attempts must run before the exception is raised.
    assert fake_claude.call_count == 2


# ──────────────────────────────────────────────────────────────────────────
# (4) Strict-JSON addendum lands in the second user message
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_draft_appends_strict_json_addendum_on_retry(
    fake_claude, draft_ctx
):
    """The retry user message carries the strict-JSON addendum verbatim."""
    fake_claude.register_default({"text": "still not JSON"})

    with pytest.raises(SchemaViolation):
        await generate_draft(
            ctx=draft_ctx,
            model="claude-haiku-4-5-20251001",
            claude_client=fake_claude,
        )

    # First call: clean user prompt without the addendum.
    assert "OUTPUT ONLY JSON" not in fake_claude.calls[0]["user_text"]
    # Second call: the addendum is appended verbatim.
    assert "OUTPUT ONLY JSON" in fake_claude.calls[1]["user_text"]
    assert "NO MARKDOWN FENCES" in fake_claude.calls[1]["user_text"]
    assert "NO EXPLANATIONS" in fake_claude.calls[1]["user_text"]
