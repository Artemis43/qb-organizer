"""Pure coverage-mode resolver mapping (question_type, intent) → populated sections (R3, R13).

Implements design §Coverage mode decision table verbatim. The single public entry
point is :func:`resolve_coverage_mode`, a pure function (no I/O, no logging, no
module-level mutation) that returns a :class:`CoverageMode`.

Decision table (rows are exhaustive — falling off the table raises ``ValueError``)::

    VSAQ × {define, features_of, management_of, investigations_of, complications_of,
            etiology_of, enumerate, classify, unresolvable}
    SAQ  × {discuss, mechanism_of, management_of, features_of, investigations_of,
            complications_of}
    LAQ  × {discuss, mechanism_of}

Disambiguation helper (R13.6, R13.7) — :func:`_infer_section_from_focus_topic` —
maps focus phrases ("causes of", "types of", "symptoms of", …) to canonical
section keys for the VSAQ-enumerate and VSAQ-unresolvable branches.

Robustness rules:
    * Unknown ``question_type`` → ``ValueError`` (fail off-table, R3 totality).
    * Unknown ``intent`` for SAQ / LAQ → silently fall back to the ``discuss``
      branch (the intent classifier is responsible for low-confidence flagging
      via :class:`QuestionIntent.intent_confidence_low`; coverage stays pure).
    * Unknown ``intent`` for VSAQ → routed through the ``unresolvable`` branch
      so ``vsaq_disambiguation_fallback`` surfaces in the answer record (R13.7).
"""
from __future__ import annotations

from typing import Optional

from .schema import CoverageMode


# ──────────────────────────────────────────────────────────────────────────
# Canonical section keys (LAQ_Skeleton order, R1.7)
# ──────────────────────────────────────────────────────────────────────────

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


# Intents that target a single VSAQ section (R13.1–R13.5).
_VSAQ_KNOWN_INTENTS: frozenset[str] = frozenset(
    {
        "define",
        "features_of",
        "management_of",
        "investigations_of",
        "complications_of",
        "etiology_of",
        "enumerate",
        "classify",
    }
)

# Intents that narrow an SAQ to a single deep section (R3.4).
_SAQ_NARROW_INTENTS: frozenset[str] = frozenset(
    {
        "mechanism_of",
        "management_of",
        "features_of",
        "investigations_of",
        "complications_of",
    }
)

# Intents that map to a deep but partial LAQ skeleton (design §Coverage table).
_LAQ_NARROW_INTENTS: frozenset[str] = frozenset({"mechanism_of"})


# Maps the SAQ-narrow intent to the section it targets and that section's cap.
_SAQ_NARROW_SECTION: dict[str, str] = {
    "mechanism_of": "pathophysiology",
    "management_of": "management",
    "features_of": "clinical_features",
    "investigations_of": "investigations",
    "complications_of": "complications",
}

_SAQ_NARROW_CAP: dict[str, int] = {
    "mechanism_of": 6,
    "management_of": 6,
    "features_of": 6,
    "investigations_of": 4,
    "complications_of": 4,
}


# Focus-topic → section inference (R13.6, R13.7).  Keys are checked as
# substrings of the lower-cased ``focus_topic`` so phrasing variation
# ("causes of", "list the causes of …") all collapse to the same section.
_FOCUS_TOPIC_HINTS: tuple[tuple[str, str], ...] = (
    ("causes of", "etiology"),
    ("etiology of", "etiology"),
    ("aetiology of", "etiology"),
    ("types of", "classification"),
    ("classification of", "classification"),
    ("symptoms of", "clinical_features"),
    ("features of", "clinical_features"),
    ("signs of", "clinical_features"),
    ("treatment of", "management"),
    ("management of", "management"),
    ("investigations for", "investigations"),
    ("investigations of", "investigations"),
    ("workup of", "investigations"),
    ("complications of", "complications"),
    ("pathophysiology of", "pathophysiology"),
    ("mechanism of", "pathophysiology"),
)


def _infer_section_from_focus_topic(focus_topic: str) -> str:
    """Map a free-text ``focus_topic`` to a canonical section key.

    Per R13.6 / R13.7:
        - "Enumerate causes of X"        → ``etiology``
        - "Enumerate types of X"         → ``classification``
        - "Enumerate symptoms of X"      → ``clinical_features``
        - "Treatment of X"               → ``management``
        - "Investigations for X"         → ``investigations``
        - "Complications of X"           → ``complications``
        - "Pathophysiology of X"         → ``pathophysiology``
        - default (unrecognised)         → ``definition``

    The lookup is case-insensitive substring matching.  Any string that does
    not match any hint defaults to ``definition`` so the caller still gets a
    well-formed coverage mode.
    """
    if not isinstance(focus_topic, str):
        return "definition"
    needle = focus_topic.lower()
    for phrase, section in _FOCUS_TOPIC_HINTS:
        if phrase in needle:
            return section
    return "definition"


# ──────────────────────────────────────────────────────────────────────────
# Per-branch builders
# ──────────────────────────────────────────────────────────────────────────


def _vsaq_cap(requested_item_count: Optional[int], default: int) -> int:
    """Return ``requested_item_count`` if provided and positive, else ``default``."""
    if isinstance(requested_item_count, int) and requested_item_count > 0:
        return requested_item_count
    return default


def _build_vsaq(
    intent: str,
    focus_topic: str,
    requested_item_count: Optional[int],
) -> CoverageMode:
    """Resolve every VSAQ row of the decision table.

    Unknown ``intent`` strings (those outside :data:`_VSAQ_KNOWN_INTENTS`) are
    routed through the ``unresolvable`` branch so the ``vsaq_disambiguation_fallback``
    flag still surfaces (R13.7).
    """
    # ── define ───────────────────────────────────────────────────────────
    if intent == "define":
        return CoverageMode(
            mode="vsaq_focused",
            sections_to_populate=["definition"],
            per_section_caps={},
            depth_target="vsaq",
            vsaq_disambiguation_fallback=False,
        )

    # ── single-section, capped intents (R13.1–R13.4) ─────────────────────
    single_section_map = {
        "features_of": "clinical_features",
        "management_of": "management",
        "investigations_of": "investigations",
        "complications_of": "complications",
        "etiology_of": "etiology",
    }
    if intent in single_section_map:
        section = single_section_map[intent]
        return CoverageMode(
            mode="vsaq_focused",
            sections_to_populate=[section],
            per_section_caps={section: _vsaq_cap(requested_item_count, 5)},
            depth_target="vsaq",
            vsaq_disambiguation_fallback=False,
        )

    # ── enumerate (R13.6) — section inferred from focus_topic ────────────
    if intent == "enumerate":
        section = _infer_section_from_focus_topic(focus_topic)
        return CoverageMode(
            mode="vsaq_focused",
            sections_to_populate=[section],
            per_section_caps={section: _vsaq_cap(requested_item_count, 5)},
            depth_target="vsaq",
            vsaq_disambiguation_fallback=False,
        )

    # ── classify ─────────────────────────────────────────────────────────
    if intent == "classify":
        return CoverageMode(
            mode="vsaq_focused",
            sections_to_populate=["classification"],
            per_section_caps={"classification": _vsaq_cap(requested_item_count, 4)},
            depth_target="vsaq",
            vsaq_disambiguation_fallback=False,
        )

    # ── unresolvable (R13.7) — also catches every unknown VSAQ intent ───
    inferred = _infer_section_from_focus_topic(focus_topic)
    sections = (
        ["definition"] if inferred == "definition" else ["definition", inferred]
    )
    cap = _vsaq_cap(requested_item_count, 4)
    caps: dict[str, int] = {}
    if inferred != "definition":
        caps[inferred] = cap
    return CoverageMode(
        mode="vsaq_focused",
        sections_to_populate=sections,
        per_section_caps=caps,
        depth_target="vsaq",
        vsaq_disambiguation_fallback=True,
    )


def _build_saq(intent: str) -> CoverageMode:
    """Resolve every SAQ row of the decision table.

    Per R3.3 a ``discuss`` (or empty/unknown) intent populates the full canonical
    9-section skeleton with at most 2 bullets per leaf list.  Per R3.4 a
    narrowing intent populates only its targeted section at LAQ depth.
    """
    # ── narrow intents (R3.4) ────────────────────────────────────────────
    if intent in _SAQ_NARROW_INTENTS:
        section = _SAQ_NARROW_SECTION[intent]
        cap = _SAQ_NARROW_CAP[intent]
        return CoverageMode(
            mode="saq_narrow_deep",
            sections_to_populate=[section],
            per_section_caps={section: cap},
            depth_target="laq",
            vsaq_disambiguation_fallback=False,
        )

    # ── discuss / unspecified / unknown (R3.3) ───────────────────────────
    return CoverageMode(
        mode="saq_compressed",
        sections_to_populate=list(CANONICAL_9_SECTIONS),
        per_section_caps={s: 2 for s in CANONICAL_9_SECTIONS},
        depth_target="saq",
        vsaq_disambiguation_fallback=False,
    )


def _build_laq(intent: str) -> CoverageMode:
    """Resolve every LAQ row of the decision table.

    Per R3.5 / R3.6 a ``discuss`` (or empty/unknown) intent populates every
    section of the LAQ_Skeleton.  ``mechanism_of`` collapses the surface
    sections but keeps a deep pathophysiology block.
    """
    # ── mechanism_of: deep mechanism with surrounding context ────────────
    if intent == "mechanism_of":
        sections = ["definition", "etiology", "pathophysiology", "clinical_features"]
        return CoverageMode(
            mode="laq_full",
            sections_to_populate=sections,
            per_section_caps={
                "etiology": 6,
                "pathophysiology": 8,        # deep mechanism (design table)
                "clinical_features": 4,      # compressed surface block
            },
            depth_target="laq",
            vsaq_disambiguation_fallback=False,
        )

    # ── discuss / unspecified / unknown (R3.5, R3.6) ─────────────────────
    return CoverageMode(
        mode="laq_full",
        sections_to_populate=list(CANONICAL_9_SECTIONS),
        per_section_caps={s: 6 for s in CANONICAL_9_SECTIONS},
        depth_target="laq",
        vsaq_disambiguation_fallback=False,
    )


# ──────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────


def resolve_coverage_mode(
    question_type: str,
    intent: str,
    focus_topic: str,
    requested_item_count: Optional[int],
) -> CoverageMode:
    """Resolve the (question_type, intent) → CoverageMode decision table.

    Args:
        question_type: One of ``"VSAQ"``, ``"SAQ"``, ``"LAQ"`` (case-insensitive
            for forgiveness, but the canonical upper-case form is preferred).
        intent: One of the R2.1 intent labels.  Empty string and unrecognised
            labels fall back to the ``discuss`` row for SAQ/LAQ and to the
            ``unresolvable`` row for VSAQ.
        focus_topic: Free-text focus extracted by the intent classifier; used
            by the VSAQ ``enumerate`` and VSAQ ``unresolvable`` rows to infer
            which single section to populate (R13.6, R13.7).
        requested_item_count: Explicit count parsed from the stem
            (e.g. "Enumerate 4 causes …").  Used to override the default cap
            on the VSAQ branches (R3.2).

    Returns:
        A :class:`CoverageMode` whose ``sections_to_populate`` is non-empty and
        whose every entry is drawn from :data:`CANONICAL_9_SECTIONS`.

    Raises:
        ValueError: If ``question_type`` is not one of ``VSAQ`` / ``SAQ`` /
            ``LAQ`` (failing off the decision table per R3 totality).
    """
    qt = (question_type or "").strip().upper()
    intent_norm = (intent or "").strip()

    if qt == "VSAQ":
        return _build_vsaq(intent_norm, focus_topic or "", requested_item_count)
    if qt == "SAQ":
        return _build_saq(intent_norm)
    if qt == "LAQ":
        return _build_laq(intent_norm)

    raise ValueError(
        f"resolve_coverage_mode: unknown question_type {question_type!r} "
        f"(expected one of 'VSAQ', 'SAQ', 'LAQ')"
    )


__all__ = [
    "CANONICAL_9_SECTIONS",
    "resolve_coverage_mode",
    "_infer_section_from_focus_topic",
]
