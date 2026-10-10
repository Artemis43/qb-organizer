"""Property-based tests for the medical-grade answer pipeline.

Each property maps to a numbered entry in design.md §Catalog (P1–P10) and is
tagged with a header comment plus the ``@pytest.mark.property`` marker so the
suite can be filtered with ``pytest -m property``.
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings

from answers.schema import AnswerSections
from tests.answers.conftest import answer_sections_st


# Feature: medical-grade-answer-pipeline, Property 1: Schema empty-collapse idempotence
# Validates: Requirements 1.6
#
# For any AnswerSections instance ``s``, applying the empty-section collapse
# twice yields the same result as applying it once. The validator runs on every
# ``model_validate`` and on every JSON round-trip; non-idempotent collapse would
# mean repeated I/O could change the persisted shape.
@pytest.mark.property
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(answer_sections_st())
def test_p1_schema_empty_collapse_idempotence(s: AnswerSections) -> None:
    once = AnswerSections.model_validate(s.model_dump())
    twice = AnswerSections.model_validate(once.model_dump())
    assert once == twice


# Feature: medical-grade-answer-pipeline, Property 2: Coverage resolver totality
# Validates: Requirements 3.1, 3.3, 3.4, 3.5, 13.1, 13.2, 13.3, 13.4, 13.5
#
# For any ``(question_type, intent, focus_topic, requested_item_count)`` quadruple
# drawn from the supported domain, ``resolve_coverage_mode`` returns a
# ``CoverageMode`` whose ``sections_to_populate`` is a non-empty subset of the
# canonical 9 sections. ``question_type_st`` returns ``"VSAQ" | "SAQ" | "LAQ"``
# and ``question_intent_st`` returns one of the 13 valid intent labels, so the
# resolver must remain total across every supported (qt, intent) row plus
# arbitrary ``focus_topic`` and item-count inputs.
from hypothesis import strategies as st

from answers.coverage import resolve_coverage_mode
from tests.answers.conftest import (
    CANONICAL_9_SECTIONS,
    focus_topic_st,
    question_intent_st,
    question_type_st,
)


@pytest.mark.property
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(
    question_type_st(),
    question_intent_st(),
    focus_topic_st(),
    st.one_of(st.none(), st.integers(1, 20)),
)
def test_p2_coverage_resolver_totality(
    question_type: str,
    intent: str,
    focus_topic: str,
    requested_item_count: int | None,
) -> None:
    cm = resolve_coverage_mode(
        question_type, intent, focus_topic, requested_item_count
    )
    assert set(cm.sections_to_populate) - set(CANONICAL_9_SECTIONS) == set()
    assert len(cm.sections_to_populate) >= 1


# Feature: medical-grade-answer-pipeline, Property 3: Markdown round-trip stability
# Validates: Requirements 11.2, 11.3, 11.4, 11.5, 11.7
#
# For any AnswerSections instance ``s``, ``serialize_to_markdown(s)`` contains a
# level-3 heading ``### <Title>`` for every populated top-level section (the
# field is not None after ``_collapse_empty``), no such heading for any section
# whose field is None, and pathophysiology with N >= 2 steps emits exactly
# N - 1 ` → ` separators in the rendered patho line.
from answers.markdown import SECTION_TITLES, serialize_to_markdown


@pytest.mark.property
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(answer_sections_st())
def test_p3_markdown_round_trip_stability(s: AnswerSections) -> None:
    md = serialize_to_markdown(s)
    for section_key, title in SECTION_TITLES.items():
        heading = f"### {title}"
        value = getattr(s, section_key)
        if value is None:
            assert heading not in md, (
                f"section '{section_key}' is None but stray heading "
                f"'{heading}' appeared in markdown output"
            )
        else:
            assert heading in md, (
                f"section '{section_key}' is populated but heading "
                f"'{heading}' is missing from markdown output"
            )
    # Pathophysiology arrow-joining (R11.5): of the steps with non-empty text
    # (the ones markdown.py keeps after dropping empty-text bullets per R11.7),
    # N surviving steps produce exactly N - 1 ` → ` separators.
    if s.pathophysiology is not None:
        surviving = [
            step
            for step in s.pathophysiology
            if step.text and step.text.strip()
        ]
        if len(surviving) >= 2:
            expected = len(surviving) - 1
            actual = md.count(" → ")
            assert actual == expected, (
                f"pathophysiology has {len(surviving)} surviving steps; "
                f"expected {expected} ' → ' separators, got {actual}"
            )


# Feature: medical-grade-answer-pipeline, Property 4: Validator violation determinism
# Validates: Requirements 5.1, 5.2, 5.3
#
# For any AnswerSections instance ``s``, the structural validators are pure and
# deterministic: repeated calls with the same input return equal
# ``MandatoryDataReport`` instances. No hidden state, no time-of-day dependence,
# no randomness. The pipeline builds re-prompt payloads from the violation list
# (R5.5); a non-deterministic validator would feed the model contradicting
# feedback across attempts.
from answers.validators import (
    check_bullet_text,
    check_classification,
    check_investigations,
    check_pharm,
)


@pytest.mark.property
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(answer_sections_st())
def test_p4_validator_violation_determinism(s: AnswerSections) -> None:
    assert check_pharm(s) == check_pharm(s)
    assert check_investigations(s) == check_investigations(s)
    assert check_classification(s) == check_classification(s)
    assert check_bullet_text(s) == check_bullet_text(s)


# Feature: medical-grade-answer-pipeline, Property 5: Cost gate monotonicity
# Validates: Requirements 9.6
#
# For any fixed ``projected``, batch ``ceiling`` and daily ceiling, if
# ``cost_gate.check`` blocks the ``escalation_sonnet`` stage at ``spend_a`` it
# must also block at every ``spend_b > spend_a``. Adding more accumulated spend
# can never turn a block back into an allow — otherwise a single batch could
# oscillate across the ceiling and let an escalation through after the budget
# is already exhausted. We also assert the always-block-above-ceiling
# sub-property: any ``spend`` already above the ceiling produces ``"block"``
# regardless of the projected cost.
from hypothesis import assume

from answers.cost_gate import check


@pytest.mark.property
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.floats(min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    st.floats(min_value=0.5, max_value=5.0, allow_nan=False, allow_infinity=False),
)
def test_p5_cost_gate_monotonicity(
    spend_a: float,
    spend_b: float,
    projected: float,
    ceiling: float,
) -> None:
    assume(spend_a < spend_b)

    # (1) Monotonicity for the batch ceiling: if the gate blocks at the
    # smaller spend, it must also block at the larger spend.
    decision_a = check("escalation_sonnet", projected, spend_a, 0.0, ceiling, 999.0)
    decision_b = check("escalation_sonnet", projected, spend_b, 0.0, ceiling, 999.0)
    if decision_a == "block":
        assert decision_b == "block"

    # (2) Always-block-above-ceiling: any spend already above the batch ceiling
    # must produce ``"block"`` regardless of the projected cost. We exercise
    # this in the same function whenever the generated ``spend_a`` happens to
    # exceed ``ceiling``; otherwise we synthesise a deterministic above-ceiling
    # spend so the sub-property is checked on every example.
    if spend_a > ceiling:
        assert (
            check("escalation_sonnet", projected, spend_a, 0.0, ceiling, 999.0)
            == "block"
        )
    above_ceiling_spend = ceiling + 0.1
    assert (
        check("escalation_sonnet", projected, above_ceiling_spend, 0.0, ceiling, 999.0)
        == "block"
    )


# Feature: medical-grade-answer-pipeline, Property 6: Intent label closure
# Validates: Requirements 2.1, 2.4
#
# For any raw classifier output (valid label, unknown label, missing fields,
# wrong-type fields, malformed JSON shape), the pure ``coerce_intent_label``
# helper returns a ``QuestionIntent`` whose ``intent`` is a member of the
# closed 13-label set ``INTENT_LABELS_13`` defined in R2.1. Without this
# closure, the coverage resolver — which indexes into a label-keyed table —
# would crash whenever Claude hallucinates a new label.
from answers.intent import INTENT_LABELS_13, coerce_intent_label
from tests.answers.conftest import raw_intent_output_st


@pytest.mark.property
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(raw_intent_output_st())
def test_p6_intent_label_closure(raw: dict) -> None:
    result = coerce_intent_label(raw)
    assert result.intent in INTENT_LABELS_13


# Feature: medical-grade-answer-pipeline, Property 7: AnswerSections JSON round-trip
# Validates: Requirements 1.1, 1.7, 1.8, 1.9
#
# For any ``AnswerSections`` instance ``s`` (after empty-collapse), JSON
# serialization via ``model_dump`` followed by ``model_validate`` reproduces
# the original instance, and ``model_dump`` preserves the LAQ_Skeleton field
# order across the round-trip. SQLite stores ``sections`` as JSON text, so
# this property is what guarantees that persisting and re-loading an answer
# keeps both the bullet shape (R1.8) and the canonical key order (R1.7) the
# downstream markdown serializer relies on.
#
# Note: ``_collapse_empty`` may rewrite some fields from ``[]`` to ``None``,
# so we first normalize ``s`` by running ``model_validate`` once to obtain
# the canonical post-collapse instance, then assert idempotence on the
# round-trip from that canonical form.
@pytest.mark.property
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(answer_sections_st())
def test_p7_answer_sections_json_round_trip(s: AnswerSections) -> None:
    collapsed = AnswerSections.model_validate(s.model_dump())
    round_tripped = AnswerSections.model_validate(collapsed.model_dump())
    assert collapsed == round_tripped
    assert list(collapsed.model_dump().keys()) == list(round_tripped.model_dump().keys())


# Feature: medical-grade-answer-pipeline, Property 8: VSAQ exclusivity
# Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5
#
# For any VSAQ paired with a single-section intent (``features_of``,
# ``management_of``, ``investigations_of``, ``complications_of``, ``define``),
# the resolver must populate exactly one section. VSAQs target a single
# concept, so emitting more than one section would over-answer the prompt and
# violate R13. ``st.sampled_from`` requires a sequence, so we sort the set
# (its iteration order is otherwise unspecified) before sampling.
from tests.answers.conftest import VSAQ_SINGLE_SECTION_INTENTS


@pytest.mark.property
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.sampled_from(sorted(VSAQ_SINGLE_SECTION_INTENTS)),
    focus_topic_st(),
    st.one_of(st.none(), st.integers(1, 20)),
)
def test_p8_vsaq_exclusivity(
    intent: str,
    focus_topic: str,
    requested_item_count: int | None,
) -> None:
    cm = resolve_coverage_mode("VSAQ", intent, focus_topic, requested_item_count)
    assert len(cm.sections_to_populate) == 1


# Feature: medical-grade-answer-pipeline, Property 9: SAQ broad totality
# Validates: Requirements 3.3
#
# For any SAQ paired with the broad ``discuss`` intent (or the empty-string
# fallback that R3.3 also routes through the broad table), ``resolve_coverage_mode``
# must populate the full canonical 9-section template. SAQs under a discuss
# intent are the "everything we know about X" case, so the resolver cannot
# silently drop sections regardless of ``focus_topic`` or
# ``requested_item_count``.
@pytest.mark.property
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.just("SAQ"),
    st.sampled_from(["discuss", ""]),
    focus_topic_st(),
    st.one_of(st.none(), st.integers(1, 20)),
)
def test_p9_saq_broad_totality(
    question_type: str,
    intent: str,
    focus_topic: str,
    requested_item_count: int | None,
) -> None:
    cm = resolve_coverage_mode(
        question_type, intent, focus_topic, requested_item_count
    )
    assert set(cm.sections_to_populate) == set(CANONICAL_9_SECTIONS)


# Feature: medical-grade-answer-pipeline, Property 10: LAQ definition mandate
# Validates: Requirements 3.6
#
# For any LAQ regardless of intent (``discuss`` or the empty-string fallback),
# ``resolve_coverage_mode`` must include the ``definition`` section in the
# resolved coverage. R3.6 mandates that long-answer questions always lead with
# a definition; dropping it would produce a header-less answer body.
@pytest.mark.property
@settings(max_examples=100)
@given(
    st.just("LAQ"),
    st.sampled_from(["discuss", ""]),
    focus_topic_st(),
    st.one_of(st.none(), st.integers(1, 20)),
)
def test_p10_laq_definition_mandate(
    question_type: str,
    intent: str,
    focus_topic: str,
    requested_item_count: int | None,
) -> None:
    cm = resolve_coverage_mode(
        question_type, intent, focus_topic, requested_item_count
    )
    assert "definition" in cm.sections_to_populate
