"""Pytest fixtures and Hypothesis strategies for the answer-pipeline test suite.

Design references
-----------------
- design.md §Prework Analysis → Test seam (FakeClaudeClient)
- design.md §Test Placement (where strategies live)
- design.md §Catalog → P1–P10 (which strategies each property consumes)

Public surface
--------------
- ``fake_claude`` pytest fixture — fresh ``FakeClaudeClient`` per test.
- ``answer_sections_st()`` — Hypothesis strategy producing ``AnswerSections``.
- ``question_intent_st()`` — strategy over the 13 R2.1 intent labels.
- ``question_type_st()`` — strategy over ``{"VSAQ", "SAQ", "LAQ"}``.
- ``focus_topic_st()`` — strategy over short medical-ish phrases + arbitrary text.
- ``raw_intent_output_st()`` — strategy producing dicts that *may* be valid
  ``QuestionIntent`` shapes; used to exercise the coercion helper for P6.

Constants
---------
- ``VSAQ_SINGLE_SECTION_INTENTS`` — used by P8 (VSAQ exclusivity).
- ``CANONICAL_9_SECTIONS`` — used by P2, P9 (coverage totality).
- ``INTENT_LABELS_13`` — the closed label set defined in R2.1.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import strategies as st

# Make ``backend/`` importable so ``answers.*`` resolves the same way the
# production code does (server.py runs from the backend/ working directory).
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from answers.schema import (  # noqa: E402  (import after sys.path tweak)
    AnswerSections,
    Bullet,
    ClassificationEntry,
    ClinicalFeatures,
    Complications,
    InvestigationBullet,
    Investigations,
    Management,
    PathoStep,
    PharmBullet,
)
from tests.answers.fakes import FakeClaudeClient  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────
# Constants (referenced from property tests)
# ──────────────────────────────────────────────────────────────────────────

#: VSAQ + intent combinations that must populate exactly one section (R13).
VSAQ_SINGLE_SECTION_INTENTS: set[str] = {
    "features_of",
    "management_of",
    "investigations_of",
    "complications_of",
    "define",
}

#: The 9-section LAQ_Skeleton order (R1.7).
CANONICAL_9_SECTIONS: list[str] = [
    "definition",
    "classification",
    "etiology",
    "pathophysiology",
    "clinical_features",
    "investigations",
    "management",
    "complications",
    "prognosis",
]

#: Closed set of intent labels per R2.1.
INTENT_LABELS_13: list[str] = [
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
]

#: Sample medical phrase pool used by ``focus_topic_st``.
_MEDICAL_PHRASES: list[str] = [
    "diabetic ketoacidosis",
    "myocardial infarction",
    "tuberculosis",
    "acute pancreatitis",
    "rheumatoid arthritis",
    "chronic kidney disease",
    "asthma",
    "pneumonia",
    "stroke",
    "hypothyroidism",
    "anaemia",
    "heart failure",
    "hypertension",
    "cirrhosis",
    "meningitis",
]

#: Question types per R3.
_QUESTION_TYPES: list[str] = ["VSAQ", "SAQ", "LAQ"]


# ──────────────────────────────────────────────────────────────────────────
# Pytest fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_claude() -> FakeClaudeClient:
    """A fresh ``FakeClaudeClient`` for each test (no shared state)."""
    return FakeClaudeClient()


# ──────────────────────────────────────────────────────────────────────────
# Hypothesis strategies — primitives
# ──────────────────────────────────────────────────────────────────────────

# Constrain text to printable, non-empty, short strings so generated
# AnswerSections are realistic and serialize cleanly through JSON.
_short_text = st.text(
    alphabet=st.characters(
        min_codepoint=32,
        max_codepoint=126,
        blacklist_categories=("Cs",),
    ),
    min_size=1,
    max_size=40,
).map(lambda s: s.strip()).filter(lambda s: len(s) > 0)

_chunk_id = st.text(
    alphabet=st.characters(
        min_codepoint=ord("a"),
        max_codepoint=ord("z"),
        whitelist_characters="0123456789_",
    ),
    min_size=4,
    max_size=12,
)

_chunk_id_list = st.lists(_chunk_id, min_size=0, max_size=3)
_concept_id_list = st.lists(_chunk_id, min_size=0, max_size=3)


# ──────────────────────────────────────────────────────────────────────────
# Hypothesis strategies — bullet sub-models
# ──────────────────────────────────────────────────────────────────────────


def _bullet_st() -> st.SearchStrategy[Bullet]:
    return st.builds(
        Bullet,
        text=_short_text,
        source_chunk_ids=_chunk_id_list,
        kg_concept_ids=_concept_id_list,
    )


def _pharm_bullet_st() -> st.SearchStrategy[PharmBullet]:
    return st.builds(
        PharmBullet,
        text=_short_text,
        source_chunk_ids=_chunk_id_list,
        kg_concept_ids=_concept_id_list,
        drug_class=_short_text,
        drug_name=_short_text,
        dose=st.sampled_from(["10 mg", "0.1 U/kg", "500 mg", "1 g", "5 mL"]),
        frequency=st.sampled_from(["once daily", "BD", "TDS", "QID", "PRN"]),
    )


def _investigation_bullet_st() -> st.SearchStrategy[InvestigationBullet]:
    return st.builds(
        InvestigationBullet,
        text=_short_text,
        source_chunk_ids=_chunk_id_list,
        kg_concept_ids=_concept_id_list,
        test_name=_short_text,
        value_or_range=st.sampled_from(
            ["< 7.30", "≥ 250 mg/dL", "70–110 mg/dL", "positive", "12–18 g/dL"]
        ),
    )


def _classification_entry_st() -> st.SearchStrategy[ClassificationEntry]:
    return st.builds(
        ClassificationEntry,
        text=_short_text,
        source_chunk_ids=_chunk_id_list,
        kg_concept_ids=_concept_id_list,
        system_name=st.sampled_from(["WHO", "NYHA", "GOLD", "TNM", "Wells", "Killip"]),
        year=st.integers(min_value=1950, max_value=2030),
        categories=st.lists(_short_text, min_size=1, max_size=4),
    )


def _patho_step_st() -> st.SearchStrategy[PathoStep]:
    return st.builds(
        PathoStep,
        text=_short_text,
        source_chunk_ids=_chunk_id_list,
        kg_concept_ids=_concept_id_list,
        step_index=st.integers(min_value=1, max_value=10),
    )


# ──────────────────────────────────────────────────────────────────────────
# Hypothesis strategies — composite sections
# ──────────────────────────────────────────────────────────────────────────


def _clinical_features_st() -> st.SearchStrategy[ClinicalFeatures]:
    return st.builds(
        ClinicalFeatures,
        symptoms=st.lists(_bullet_st(), min_size=0, max_size=3),
        signs=st.lists(_bullet_st(), min_size=0, max_size=3),
        hallmarks=st.lists(_bullet_st(), min_size=0, max_size=2),
    )


def _investigations_st() -> st.SearchStrategy[Investigations]:
    return st.builds(
        Investigations,
        bedside=st.lists(_investigation_bullet_st(), min_size=0, max_size=3),
        lab=st.lists(_investigation_bullet_st(), min_size=0, max_size=3),
        imaging=st.lists(_investigation_bullet_st(), min_size=0, max_size=2),
    )


def _management_st() -> st.SearchStrategy[Management]:
    return st.builds(
        Management,
        non_pharm=st.lists(_bullet_st(), min_size=0, max_size=3),
        pharm=st.lists(_pharm_bullet_st(), min_size=0, max_size=3),
        surgical=st.lists(_bullet_st(), min_size=0, max_size=2),
        targets=st.lists(_bullet_st(), min_size=0, max_size=2),
    )


def _complications_st() -> st.SearchStrategy[Complications]:
    return st.builds(
        Complications,
        acute=st.lists(_bullet_st(), min_size=0, max_size=3),
        chronic=st.lists(_bullet_st(), min_size=0, max_size=3),
    )


# ──────────────────────────────────────────────────────────────────────────
# Hypothesis strategies — public, top-level
# ──────────────────────────────────────────────────────────────────────────


def answer_sections_st() -> st.SearchStrategy[AnswerSections]:
    """Generate ``AnswerSections`` instances spanning empty, partial, and full cases.

    The strategy mixes ``None`` (section omitted), empty list (collapses to None
    via ``_collapse_empty``), and populated sections so that property tests
    exercise both the empty-collapse paths (R1.6) and the LAQ_Skeleton ordering
    paths (R1.7).
    """
    return st.builds(
        AnswerSections,
        definition=st.one_of(st.none(), st.just(""), _short_text),
        classification=st.one_of(
            st.none(),
            st.just([]),
            st.lists(_classification_entry_st(), min_size=1, max_size=3),
        ),
        etiology=st.one_of(
            st.none(),
            st.just([]),
            st.lists(_bullet_st(), min_size=1, max_size=4),
        ),
        pathophysiology=st.one_of(
            st.none(),
            st.just([]),
            st.lists(_patho_step_st(), min_size=1, max_size=4),
        ),
        clinical_features=st.one_of(st.none(), _clinical_features_st()),
        investigations=st.one_of(st.none(), _investigations_st()),
        management=st.one_of(st.none(), _management_st()),
        complications=st.one_of(st.none(), _complications_st()),
        prognosis=st.one_of(
            st.none(),
            st.just([]),
            st.lists(_bullet_st(), min_size=1, max_size=3),
        ),
        mnemonics=st.dictionaries(
            keys=st.sampled_from(CANONICAL_9_SECTIONS),
            values=st.sampled_from(["MUDPILES", "AEIOU TIPS", "DKA"]),
            min_size=0,
            max_size=2,
        ),
    )


def question_intent_st() -> st.SearchStrategy[str]:
    """Sample one of the 13 closed intent labels defined in R2.1."""
    return st.sampled_from(INTENT_LABELS_13)


def question_type_st() -> st.SearchStrategy[str]:
    """Sample one of ``{"VSAQ", "SAQ", "LAQ"}``."""
    return st.sampled_from(_QUESTION_TYPES)


def focus_topic_st() -> st.SearchStrategy[str]:
    """Generate a short focus-topic phrase mixing canned medical terms with arbitrary text."""
    canned = st.sampled_from(_MEDICAL_PHRASES)
    free = st.text(
        alphabet=st.characters(
            min_codepoint=32, max_codepoint=126, blacklist_categories=("Cs",)
        ),
        min_size=0,
        max_size=30,
    )
    return st.one_of(canned, free)


def raw_intent_output_st() -> st.SearchStrategy[dict[str, Any]]:
    """Generate raw intent-classifier output dicts spanning valid and malformed shapes.

    Used by Property 6 (intent label closure) to exercise
    ``intent.coerce_intent_label`` over:
    - valid 13-label outputs;
    - unknown/garbage label strings;
    - missing fields;
    - wrong types on numeric fields.
    """
    valid_intent = st.sampled_from(INTENT_LABELS_13)
    bad_intent = st.text(
        alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
        min_size=1,
        max_size=20,
    ).filter(lambda s: s not in INTENT_LABELS_13)
    intent_field = st.one_of(valid_intent, bad_intent, st.none(), st.integers())

    focus_field = st.one_of(focus_topic_st(), st.none())

    count_field = st.one_of(
        st.none(),
        st.integers(min_value=1, max_value=20),
        st.text(min_size=1, max_size=4),  # malformed type
    )

    flag_field = st.one_of(st.booleans(), st.none(), st.integers(0, 1))

    def _build(intent: Any, focus: Any, count: Any, flag: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        # Half the time, deliberately omit a field to test missing-field coercion.
        if intent is not None or len(out) % 2 == 0:
            out["intent"] = intent
        if focus is not None:
            out["focus_topic"] = focus
        if count is not None:
            out["requested_item_count"] = count
        if flag is not None:
            out["intent_confidence_low"] = flag
        return out

    return st.builds(_build, intent_field, focus_field, count_field, flag_field)


# ──────────────────────────────────────────────────────────────────────────
# Public re-exports for star-imports
# ──────────────────────────────────────────────────────────────────────────

__all__ = [
    "VSAQ_SINGLE_SECTION_INTENTS",
    "CANONICAL_9_SECTIONS",
    "INTENT_LABELS_13",
    "fake_claude",
    "answer_sections_st",
    "question_intent_st",
    "question_type_st",
    "focus_topic_st",
    "raw_intent_output_st",
]
