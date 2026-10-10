"""Unit tests for ``answers.critic.evaluate`` and ``evaluate_lite`` (R8.3, R8.7).

Covers the five documented response paths through the critic stage:

1. Full critic returns a verdict whose four sub-scores are all ≥ 70 — the
   parsed ``CriticVerdict`` round-trips with ``verdict="pass"``.
2. Full critic returns a verdict with ``depth_score=65`` (below 70). The
   verdict logic is enforced inside the prompt, so this test asserts that
   what the FAKE returned (``verdict="fail"``) is what the function emits —
   we do not re-judge in Python.
3. VSAQ-lite returns a verdict with ``depth_score`` and
   ``traceability_score`` set to JSON ``null``. After lite normalization the
   parsed :class:`CriticVerdict` carries ``100`` in both fields.
4. Malformed Claude response → :class:`CriticFailure`.
5. Boundary verdict at exactly (70, 69) — round-trips whatever the FAKE
   returned, including the prompt-side verdict decision.

All tests use the ``fake_claude`` fixture so no real Claude calls are made.
"""
from __future__ import annotations

import pytest

from answers.critic import evaluate, evaluate_lite
from answers.draft import DraftContext
from answers.errors import CriticFailure
from answers.schema import (
    AnswerSections,
    Bullet,
    CoverageMode,
    KGSkeleton,
    PathoStep,
)


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def laq_coverage_mode() -> CoverageMode:
    """A LAQ-style coverage mode with two sections gated in."""
    return CoverageMode(
        mode="laq_full",
        sections_to_populate=["definition", "pathophysiology"],
        per_section_caps={"pathophysiology": 6},
        depth_target="laq",
        vsaq_disambiguation_fallback=False,
    )


@pytest.fixture
def vsaq_coverage_mode() -> CoverageMode:
    """A VSAQ-focused coverage mode with a single section gated in."""
    return CoverageMode(
        mode="vsaq_focused",
        sections_to_populate=["definition"],
        per_section_caps={},
        depth_target="vsaq",
        vsaq_disambiguation_fallback=False,
    )


@pytest.fixture
def empty_kg_skeleton() -> KGSkeleton:
    return KGSkeleton(
        concept_ids=[],
        relation_ids=[],
        must_know_concept_ids=[],
        skeleton_block_text="",
        used=False,
    )


@pytest.fixture
def laq_ctx(laq_coverage_mode, empty_kg_skeleton) -> DraftContext:
    return DraftContext(
        question_text="Discuss the pathophysiology of diabetic ketoacidosis.",
        question_type="LAQ",
        intent="mechanism_of",
        focus_topic="diabetic ketoacidosis",
        requested_item_count=None,
        coverage_mode=laq_coverage_mode,
        kg_skeleton=empty_kg_skeleton,
        retrieved_context=None,
        mnemonic_hint=None,
        subject="General Medicine",
    )


@pytest.fixture
def vsaq_ctx(vsaq_coverage_mode, empty_kg_skeleton) -> DraftContext:
    return DraftContext(
        question_text="Define diabetic ketoacidosis.",
        question_type="VSAQ",
        intent="define",
        focus_topic="diabetic ketoacidosis",
        requested_item_count=None,
        coverage_mode=vsaq_coverage_mode,
        kg_skeleton=empty_kg_skeleton,
        retrieved_context=None,
        mnemonic_hint=None,
        subject="General Medicine",
    )


@pytest.fixture
def populated_draft() -> AnswerSections:
    """A minimal-but-non-empty draft so the user message carries real JSON."""
    return AnswerSections(
        definition=(
            "Diabetic ketoacidosis (DKA) is an acute metabolic emergency "
            "characterized by hyperglycemia, ketonemia, and high anion-gap "
            "metabolic acidosis."
        ),
        pathophysiology=[
            PathoStep(
                step_index=1,
                text="Insulin deficiency removes inhibition of lipolysis",
                source_chunk_ids=["chunk_001"],
                kg_concept_ids=["c_insulin_deficiency"],
            ),
            PathoStep(
                step_index=2,
                text="Hepatic ketogenesis produces β-hydroxybutyrate",
                source_chunk_ids=["chunk_002"],
                kg_concept_ids=["c_ketogenesis"],
            ),
        ],
    )


@pytest.fixture
def vsaq_draft() -> AnswerSections:
    """A VSAQ-style single-section draft (definition only)."""
    return AnswerSections(
        definition="DKA is an acute metabolic emergency.",
    )


def _full_pass_payload() -> dict:
    """Verdict shape with all four sub-scores ≥ 70."""
    return {
        "verdict": "pass",
        "score": 88,
        "structure_score": 100,
        "depth_score": 85,
        "traceability_score": 90,
        "mandatory_data_score": 78,
        "too_shallow_bullets": [],
        "too_deep_bullets": [],
        "missing_must_know": [],
        "feedback": [],
    }


# ──────────────────────────────────────────────────────────────────────────
# (1) Full critic — all sub-scores ≥ 70 → verdict="pass"
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_full_pass_returns_pass_verdict(
    fake_claude, laq_ctx, populated_draft
):
    """Full critic with four sub-scores ≥ 70 round-trips ``verdict="pass"`` (R8.3)."""
    fake_claude.register_default(_full_pass_payload())

    verdict = await evaluate(
        ctx=laq_ctx,
        draft=populated_draft,
        claude_client=fake_claude,
        model="claude-haiku-4-5-20251001",
    )

    assert verdict.verdict == "pass"
    assert verdict.structure_score == 100
    assert verdict.depth_score == 85
    assert verdict.traceability_score == 90
    assert verdict.mandatory_data_score == 78
    # Single Claude call on the happy path.
    assert fake_claude.call_count == 1
    call = fake_claude.calls[0]
    # The §A.3 system prompt reaches the transport unchanged.
    from answers.prompts import EXAMINER_CRITIC_FULL_SYSTEM_PROMPT
    assert call["system"] == EXAMINER_CRITIC_FULL_SYSTEM_PROMPT
    # task_type matches the design contract.
    assert call["task_type"] == "answer_critic"
    # User message carries the question + draft JSON + must-know block.
    assert "diabetic ketoacidosis" in call["user_text"]
    assert "AnswerSections_Draft:" in call["user_text"]
    assert "Must_Know_Concepts:" in call["user_text"]
    assert "Coverage_Mode:" in call["user_text"]


# ──────────────────────────────────────────────────────────────────────────
# (2) Full critic — depth_score=65 → verdict="fail" (round-tripped from prompt)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_full_below_threshold_round_trips_fail(
    fake_claude, laq_ctx, populated_draft
):
    """Full critic with depth_score=65 → ``verdict="fail"`` (R8.3 prompt-side rule).

    The verdict string is decided inside the system prompt, not in Python —
    the function trusts and round-trips whatever the FAKE returned.
    """
    fail_payload = {
        "verdict": "fail",
        "score": 65,
        "structure_score": 80,
        "depth_score": 65,            # below 70 → fail per the prompt rubric
        "traceability_score": 75,
        "mandatory_data_score": 90,
        "too_shallow_bullets": [
            "Insulin deficiency leads to DKA",
        ],
        "too_deep_bullets": [],
        "missing_must_know": [],
        "feedback": [
            "Pathophysiology step 1 is too shallow — add mechanism",
        ],
    }
    fake_claude.register_default(fail_payload)

    verdict = await evaluate(
        ctx=laq_ctx,
        draft=populated_draft,
        claude_client=fake_claude,
    )

    # Round-trip: assert the parsed verdict matches what the FAKE returned.
    assert verdict.verdict == "fail"
    assert verdict.depth_score == 65
    assert verdict.structure_score == 80
    assert verdict.traceability_score == 75
    assert verdict.mandatory_data_score == 90
    assert "Insulin deficiency leads to DKA" in verdict.too_shallow_bullets



# ──────────────────────────────────────────────────────────────────────────
# (3) VSAQ-lite — null depth/traceability normalize to 100
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_lite_normalizes_null_depth_and_traceability(
    fake_claude, vsaq_ctx, vsaq_draft
):
    """VSAQ-lite with structure=80, mandatory=75, depth/traceability=null →
    normalized to 100 after parse (R8.7 design AC)."""
    lite_payload = {
        "verdict": "pass",
        "score": 80,
        "structure_score": 80,
        "depth_score": None,            # the §A.4 prompt emits these as null
        "traceability_score": None,
        "mandatory_data_score": 75,
        "too_shallow_bullets": [],
        "too_deep_bullets": [],
        "missing_must_know": [],
        "feedback": [],
    }
    fake_claude.register_default(lite_payload)

    verdict = await evaluate_lite(
        ctx=vsaq_ctx,
        draft=vsaq_draft,
        claude_client=fake_claude,
    )

    # Lite normalization: null sub-scores filled with 100.
    assert verdict.depth_score == 100
    assert verdict.traceability_score == 100
    # The two scored sub-scores survive untouched.
    assert verdict.structure_score == 80
    assert verdict.mandatory_data_score == 75
    assert verdict.verdict == "pass"
    # min(sub_scores) is now driven by the actually-scored fields, so a
    # downstream consumer using ``score = min(sub_scores)`` sees 75.
    sub_scores = [
        verdict.structure_score,
        verdict.depth_score,
        verdict.traceability_score,
        verdict.mandatory_data_score,
    ]
    assert min(sub_scores) == 75

    # The §A.4 system prompt is wired through, not the full one.
    from answers.prompts import EXAMINER_CRITIC_VSAQ_LITE_SYSTEM_PROMPT
    assert fake_claude.calls[0]["system"] == EXAMINER_CRITIC_VSAQ_LITE_SYSTEM_PROMPT


# ──────────────────────────────────────────────────────────────────────────
# (4) Malformed Claude response → CriticFailure
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_raises_critic_failure_on_malformed_json(
    fake_claude, laq_ctx, populated_draft
):
    """Wrapper-shape ``{"text": "<not json>"}`` → :class:`CriticFailure`."""
    fake_claude.register_default({"text": "not json, just prose"})

    with pytest.raises(CriticFailure) as excinfo:
        await evaluate(
            ctx=laq_ctx,
            draft=populated_draft,
            claude_client=fake_claude,
        )

    assert "malformed verdict JSON" in excinfo.value.detail
    assert excinfo.value.mapping_id == ""


@pytest.mark.asyncio
async def test_evaluate_lite_raises_critic_failure_on_malformed_json(
    fake_claude, vsaq_ctx, vsaq_draft
):
    """Lite path also surfaces malformed responses as :class:`CriticFailure`."""
    fake_claude.register_default({"text": "garbage"})

    with pytest.raises(CriticFailure):
        await evaluate_lite(
            ctx=vsaq_ctx,
            draft=vsaq_draft,
            claude_client=fake_claude,
        )



# ──────────────────────────────────────────────────────────────────────────
# (5) Boundary — sub-scores at exactly (70, 69)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_full_round_trips_boundary_70_69(
    fake_claude, laq_ctx, populated_draft
):
    """Boundary scores ``(70, 69)``: the verdict reflects what the FAKE returned.

    We trust the prompt's threshold logic, not Python re-judgment — so a
    payload that says ``verdict="fail"`` because ``traceability_score=69``
    falls below 70 should round-trip as-is, while a sibling payload with
    every sub-score at exactly 70 should round-trip ``verdict="pass"``.
    """
    # ── Sub-case A: 70/70/70/70 → prompt-side "pass" round-trips. ───────
    pass_at_threshold = {
        "verdict": "pass",
        "score": 70,
        "structure_score": 70,
        "depth_score": 70,
        "traceability_score": 70,
        "mandatory_data_score": 70,
        "too_shallow_bullets": [],
        "too_deep_bullets": [],
        "missing_must_know": [],
        "feedback": [],
    }
    fake_claude.register_default(pass_at_threshold)

    verdict_pass = await evaluate(
        ctx=laq_ctx,
        draft=populated_draft,
        claude_client=fake_claude,
    )
    assert verdict_pass.verdict == "pass"
    assert verdict_pass.depth_score == 70
    assert verdict_pass.traceability_score == 70

    # ── Sub-case B: 70/70/69/70 → prompt-side "fail" round-trips. ───────
    fake_claude.reset()
    fail_just_below = {
        "verdict": "fail",
        "score": 69,
        "structure_score": 70,
        "depth_score": 70,
        "traceability_score": 69,
        "mandatory_data_score": 70,
        "too_shallow_bullets": [],
        "too_deep_bullets": [],
        "missing_must_know": [],
        "feedback": ["Add a chunk_id to bullet 2 of pathophysiology"],
    }
    fake_claude.register_default(fail_just_below)

    verdict_fail = await evaluate(
        ctx=laq_ctx,
        draft=populated_draft,
        claude_client=fake_claude,
    )
    assert verdict_fail.verdict == "fail"
    assert verdict_fail.traceability_score == 69
