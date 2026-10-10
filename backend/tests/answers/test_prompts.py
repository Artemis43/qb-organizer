"""Unit tests for ``answers.prompts`` (R4.1, R4.2, R4.3, R4.4, R4.5, R6.3, R7.1, R14.1).

Covers the five verbatim system-prompt constants, the depth-calibration cues in
``DRAFT_GENERATOR_SYSTEM_PROMPT``, and the conditional inclusion / exclusion of
the ``KG_Skeleton_Block`` and ``MUST APPEAR IF RELEVANT`` blocks in
``build_draft_user_prompt``. Also exercises the ``FakeClaudeClient`` test seam to
confirm the assembled user prompt and the system prompt are passed through to
the Claude transport unchanged.
"""
from __future__ import annotations

import pytest

from answers import prompts
from answers.prompts import (
    DRAFT_GENERATOR_SYSTEM_PROMPT,
    EXAMINER_CRITIC_FULL_SYSTEM_PROMPT,
    EXAMINER_CRITIC_VSAQ_LITE_SYSTEM_PROMPT,
    INTENT_CLASSIFIER_PROMPT,
    REGEN_RETRY_USER_PROMPT_TEMPLATE,
    build_draft_user_prompt,
    find_mnemonic,
)
from answers.schema import CoverageMode, KGSkeleton, RetrievedContext


# ──────────────────────────────────────────────────────────────────────────
# Prompt constants
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "constant",
    [
        INTENT_CLASSIFIER_PROMPT,
        DRAFT_GENERATOR_SYSTEM_PROMPT,
        EXAMINER_CRITIC_FULL_SYSTEM_PROMPT,
        EXAMINER_CRITIC_VSAQ_LITE_SYSTEM_PROMPT,
        REGEN_RETRY_USER_PROMPT_TEMPLATE,
    ],
)
def test_prompt_constants_are_non_empty_strings(constant):
    assert isinstance(constant, str)
    assert constant.strip(), "prompt constant must not be blank"


def test_intent_prompt_lists_all_13_labels():
    # R2.1 closure — the 13 labels must appear in the classifier prompt.
    for label in [
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
    ]:
        assert label in INTENT_CLASSIFIER_PROMPT


def test_draft_prompt_contains_three_dka_depth_examples():
    # R4 depth calibration — TOO SHALLOW / CORRECT DEPTH / TOO DEEP labels.
    assert "TOO SHALLOW" in DRAFT_GENERATOR_SYSTEM_PROMPT
    assert "CORRECT DEPTH" in DRAFT_GENERATOR_SYSTEM_PROMPT
    assert "TOO DEEP" in DRAFT_GENERATOR_SYSTEM_PROMPT
    # The DKA example anchor must be present so the depth ceiling is demonstrated.
    assert "DKA" in DRAFT_GENERATOR_SYSTEM_PROMPT or "ketoacidosis" in DRAFT_GENERATOR_SYSTEM_PROMPT
    # JSON schema preview must include at least one section with the LAQ ordering.
    assert "pathophysiology" in DRAFT_GENERATOR_SYSTEM_PROMPT
    assert "management" in DRAFT_GENERATOR_SYSTEM_PROMPT


def test_critic_prompts_define_the_subscores():
    for sub in (
        "structure_score",
        "depth_score",
        "traceability_score",
        "mandatory_data_score",
    ):
        assert sub in EXAMINER_CRITIC_FULL_SYSTEM_PROMPT
    # VSAQ-lite explicitly nullifies depth and traceability per R8.7.
    assert "depth_score" in EXAMINER_CRITIC_VSAQ_LITE_SYSTEM_PROMPT
    assert "null" in EXAMINER_CRITIC_VSAQ_LITE_SYSTEM_PROMPT


def test_regen_template_has_all_format_fields():
    # All format fields must be present so callers cannot trip over a KeyError.
    for field in (
        "{verdict}",
        "{score}",
        "{structure_score}",
        "{depth_score}",
        "{traceability_score}",
        "{mandatory_data_score}",
        "{too_shallow_bullets_bulleted}",
        "{too_deep_bullets_bulleted}",
        "{missing_must_know_bulleted}",
        "{feedback_bulleted}",
    ):
        assert field in REGEN_RETRY_USER_PROMPT_TEMPLATE


def test_regen_template_formats_cleanly():
    rendered = REGEN_RETRY_USER_PROMPT_TEMPLATE.format(
        verdict="fail",
        score=42,
        structure_score=80,
        depth_score=50,
        traceability_score=60,
        mandatory_data_score=40,
        too_shallow_bullets_bulleted="  - bullet x",
        too_deep_bullets_bulleted="  - (none)",
        missing_must_know_bulleted="  - Kussmaul's breathing",
        feedback_bulleted="  - Add cutoff to bullet 3",
    )
    assert "VERDICT: fail" in rendered
    assert "SCORE: 42/100" in rendered
    assert "Kussmaul" in rendered


# ──────────────────────────────────────────────────────────────────────────
# find_mnemonic re-export
# ──────────────────────────────────────────────────────────────────────────


def test_find_mnemonic_returns_known_entry():
    entry = find_mnemonic("high anion-gap acidosis", "etiology")
    assert entry is not None
    assert entry["acronym"] == "MUDPILES"


def test_find_mnemonic_returns_none_on_miss():
    assert find_mnemonic("totally unrelated topic xyz", "etiology") is None


# ──────────────────────────────────────────────────────────────────────────
# build_draft_user_prompt
# ──────────────────────────────────────────────────────────────────────────


def _coverage_mode(sections: list[str]) -> CoverageMode:
    return CoverageMode(
        mode="laq_full",
        sections_to_populate=sections,
        per_section_caps={s: 6 for s in sections},
        depth_target="laq",
        vsaq_disambiguation_fallback=False,
    )


def _populated_kg(must_know: list[str] | None = None) -> KGSkeleton:
    return KGSkeleton(
        concept_ids=["c_dka", "c_insulin_deficiency"],
        relation_ids=["r_dka_causes_acidosis"],
        must_know_concept_ids=list(must_know) if must_know is not None else [],
        skeleton_block_text=(
            "Concepts:\n"
            "  - diabetic ketoacidosis (disease)\n"
            "  - insulin deficiency (mechanism)\n"
            "Relations:\n"
            "  - insulin deficiency --[causes]--> ketogenesis"
        ),
        used=True,
    )


def _empty_kg() -> KGSkeleton:
    return KGSkeleton(
        concept_ids=[],
        relation_ids=[],
        must_know_concept_ids=[],
        skeleton_block_text="",
        used=False,
    )


def _retrieved(chunk_ids: list[str], texts: list[str]) -> RetrievedContext:
    return RetrievedContext(
        chunk_ids=chunk_ids,
        chunk_texts=texts,
        cited_textbook_pages=[101, 102],
        retrieval_mode="hybrid",
    )


def test_build_draft_user_prompt_includes_kg_skeleton_block_when_used():
    coverage = _coverage_mode(["definition", "pathophysiology"])
    kg = _populated_kg(must_know=["c_dka", "c_insulin_deficiency"])
    retrieved = _retrieved(
        chunk_ids=["chunk_aaa111", "chunk_bbb222"],
        texts=["Insulin deficiency drives lipolysis…", "Ketogenesis produces β-OH-butyrate…"],
    )

    prompt = build_draft_user_prompt(
        question_text="Discuss the pathophysiology of DKA.",
        question_type="LAQ",
        intent="mechanism_of",
        focus_topic="diabetic ketoacidosis",
        requested_item_count=None,
        coverage_mode=coverage,
        kg_skeleton=kg,
        retrieved_context=retrieved,
        mnemonic_hint=None,
    )

    # Required content blocks
    assert "KG_Skeleton_Block:" in prompt
    assert "MUST APPEAR IF RELEVANT" in prompt
    assert "c_dka" in prompt
    assert "Retrieved_Chunks" in prompt
    assert "chunk_aaa111" in prompt
    assert "Populate ONLY these sections:" in prompt
    assert "definition" in prompt and "pathophysiology" in prompt
    # Question + intent
    assert "Discuss the pathophysiology of DKA." in prompt
    assert "Question_Type: LAQ" in prompt
    assert "Intent: mechanism_of" in prompt


def test_build_draft_user_prompt_omits_must_appear_when_must_know_empty():
    coverage = _coverage_mode(["definition"])
    # used=True but must_know_concept_ids empty.
    kg = _populated_kg(must_know=[])

    prompt = build_draft_user_prompt(
        question_text="Define DKA.",
        question_type="VSAQ",
        intent="define",
        focus_topic="diabetic ketoacidosis",
        requested_item_count=None,
        coverage_mode=coverage,
        kg_skeleton=kg,
        retrieved_context=None,
        mnemonic_hint=None,
    )

    assert "KG_Skeleton_Block:" in prompt  # used=True still emits the block
    assert "MUST APPEAR IF RELEVANT" not in prompt


def test_build_draft_user_prompt_omits_kg_block_when_skeleton_unused():
    coverage = _coverage_mode(["definition"])
    kg = _empty_kg()  # used=False per R6.5

    prompt = build_draft_user_prompt(
        question_text="Define DKA.",
        question_type="VSAQ",
        intent="define",
        focus_topic="diabetic ketoacidosis",
        requested_item_count=None,
        coverage_mode=coverage,
        kg_skeleton=kg,
        retrieved_context=None,
        mnemonic_hint=None,
    )

    assert "KG_Skeleton_Block:" not in prompt
    assert "MUST APPEAR IF RELEVANT" not in prompt
    # Populate-only instruction is still present so coverage gating is enforced.
    assert "Populate ONLY these sections: definition" in prompt


def test_build_draft_user_prompt_includes_mnemonic_hint_when_provided():
    coverage = _coverage_mode(["etiology"])
    kg = _populated_kg(must_know=[])
    mnemonic = find_mnemonic("high anion-gap acidosis", "etiology")
    assert mnemonic is not None  # sanity: the dict provides this entry

    prompt = build_draft_user_prompt(
        question_text="Enumerate causes of high anion-gap metabolic acidosis.",
        question_type="SAQ",
        intent="enumerate",
        focus_topic="high anion-gap metabolic acidosis",
        requested_item_count=4,
        coverage_mode=coverage,
        kg_skeleton=kg,
        retrieved_context=None,
        mnemonic_hint=mnemonic,
    )

    assert "Mnemonic_Hint:" in prompt
    assert "MUDPILES" in prompt
    assert "Requested_Item_Count: 4" in prompt


# ──────────────────────────────────────────────────────────────────────────
# FakeClaudeClient passthrough — confirms the assembled prompt + system are
# delivered to the Claude transport unchanged (the integration seam contract).
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fake_claude_client_receives_prompts_unchanged(fake_claude):
    coverage = _coverage_mode(["definition"])
    kg = _empty_kg()
    user_prompt = build_draft_user_prompt(
        question_text="Define DKA.",
        question_type="VSAQ",
        intent="define",
        focus_topic="diabetic ketoacidosis",
        requested_item_count=None,
        coverage_mode=coverage,
        kg_skeleton=kg,
        retrieved_context=None,
        mnemonic_hint=None,
    )

    fake_claude.register_default({"definition": "stub"})

    response = await fake_claude.request(
        messages=[{"role": "user", "content": user_prompt}],
        system=prompts.DRAFT_GENERATOR_SYSTEM_PROMPT,
        model="sonnet",
    )

    assert response == {"definition": "stub"}
    assert fake_claude.call_count == 1
    call = fake_claude.calls[0]
    # Both system + user payloads round-trip without mutation.
    assert call["system"] == prompts.DRAFT_GENERATOR_SYSTEM_PROMPT
    assert call["user_text"] == user_prompt
