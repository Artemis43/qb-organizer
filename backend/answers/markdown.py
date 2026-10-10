"""Markdown serializer rendering AnswerSections for the existing Firestore push contract (R11).

Implements ``serialize_to_markdown(sections: AnswerSections) -> str`` so that on the
input fixture from design §D.1 it produces output byte-for-byte identical to design §D.2.

Rendering rules (see design §Markdown Serializer Worked Example):
  * Top-level sections render as ``### <Title Case>`` (level-3) using the title-case
    mapping in ``SECTION_TITLES``.
  * Sub-section headings render as ``#### <Title Case>`` (level-4) using the mapping in
    ``SUBSECTION_TITLES`` (`non_pharm` → "Non-pharmacological", `pharm` → "Pharmacological").
  * ``pathophysiology`` renders as a SINGLE LINE:
    ``1. step → 2. step → 3. step → ...`` — each step prefixed by its 1-indexed
    ``step_index`` and joined by `` → ``.
  * Plain ``Bullet`` rows render as ``- {text}``.
  * ``PharmBullet`` rows render as ``- **{drug_name}** ({drug_class}) — {dose} {frequency}``.
  * ``InvestigationBullet`` rows render as
    ``- **{test_name}** — {value_or_range} (diagnostic cutoff)``.
  * ``ClassificationEntry`` rows render as
    ``- **{system_name} {year}**: {category1} / {category2} / ...``.
  * Empty sections, empty sub-sections, and bullets with empty ``text`` are dropped (R11.7).
  * Sections are separated by a blank line. Within a section the level-3 heading is
    followed by a blank line, then the content. Level-4 sub-headings are followed by a
    blank line, then the bullets. Sub-sections within a composite are separated by a
    blank line.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from .schema import (
    AnswerSections,
    Bullet,
    ClassificationEntry,
    Complications,
    InvestigationBullet,
    Investigations,
    Management,
    PathoStep,
    PharmBullet,
)

# ──────────────────────────────────────────────────────────────────────────
# Title-case maps (R11.2, R11.4)
# ──────────────────────────────────────────────────────────────────────────

SECTION_TITLES: dict[str, str] = {
    "definition": "Definition",
    "classification": "Classification",
    "etiology": "Etiology",
    "pathophysiology": "Pathophysiology",
    "clinical_features": "Clinical Features",
    "investigations": "Investigations",
    "management": "Management",
    "complications": "Complications",
    "prognosis": "Prognosis",
}

SUBSECTION_TITLES: dict[str, str] = {
    "symptoms": "Symptoms",
    "signs": "Signs",
    "hallmarks": "Hallmarks",
    "bedside": "Bedside",
    "lab": "Lab",
    "imaging": "Imaging",
    "non_pharm": "Non-pharmacological",
    "pharm": "Pharmacological",
    "surgical": "Surgical",
    "targets": "Targets",
    "acute": "Acute",
    "chronic": "Chronic",
}

# Composite section → ordered tuple of sub-section keys (matches Section_Schema order).
_CLINICAL_FEATURES_SUBS: tuple[str, ...] = ("symptoms", "signs", "hallmarks")
_INVESTIGATIONS_SUBS: tuple[str, ...] = ("bedside", "lab", "imaging")
_MANAGEMENT_SUBS: tuple[str, ...] = ("non_pharm", "pharm", "surgical", "targets")
_COMPLICATIONS_SUBS: tuple[str, ...] = ("acute", "chronic")


# ──────────────────────────────────────────────────────────────────────────
# Bullet renderers
# ──────────────────────────────────────────────────────────────────────────


def _has_text(bullet) -> bool:
    """Return True iff the bullet's ``text`` field is a non-empty/non-whitespace string."""
    text = getattr(bullet, "text", "")
    return bool(text and text.strip())


def _render_plain_bullet(b: Bullet) -> str:
    return f"- {b.text}"


def _render_pharm_bullet(b: PharmBullet) -> str:
    # `- **{drug_name}** ({drug_class}) — {dose} {frequency}`
    return f"- **{b.drug_name}** ({b.drug_class}) — {b.dose} {b.frequency}"


def _render_investigation_bullet(b: InvestigationBullet) -> str:
    # `- **{test_name}** — {value_or_range} (diagnostic cutoff)`
    return f"- **{b.test_name}** — {b.value_or_range} (diagnostic cutoff)"


def _render_classification_entry(c: ClassificationEntry) -> str:
    # `- **{system_name} {year}**: {category1} / {category2} / ...`
    categories = " / ".join(c.categories)
    return f"- **{c.system_name} {c.year}**: {categories}"


def _render_patho_line(steps: Iterable[PathoStep]) -> str:
    """Pathophysiology renders as a single line with ``→`` joiners (R11.5).

    Each retained step is emitted as ``{step_index}. {text}`` and the parts are
    joined by `` → ``.
    """
    parts = [f"{s.step_index}. {s.text}" for s in steps if _has_text(s)]
    return " → ".join(parts)


# ──────────────────────────────────────────────────────────────────────────
# Section assemblers
# ──────────────────────────────────────────────────────────────────────────


def _render_subsection(
    key: str, bullets: Optional[list], renderer: Callable[[object], str]
) -> Optional[str]:
    """Render a level-4 sub-section; return None if it would be empty (R11.7)."""
    if not bullets:
        return None
    rendered = [renderer(b) for b in bullets if _has_text(b)]
    if not rendered:
        return None
    title = SUBSECTION_TITLES[key]
    return f"#### {title}\n\n" + "\n".join(rendered)


def _render_composite(
    section_key: str,
    sub_keys: tuple[str, ...],
    container,
    renderer_for: Callable[[str], Callable[[object], str]],
) -> Optional[str]:
    """Render a composite top-level section (clinical_features, investigations, ...).

    Returns ``None`` when every sub-section is empty so the level-3 heading is
    suppressed (R11.7).
    """
    if container is None:
        return None
    sub_blocks: list[str] = []
    for key in sub_keys:
        bullets = getattr(container, key, None)
        block = _render_subsection(key, bullets, renderer_for(key))
        if block is not None:
            sub_blocks.append(block)
    if not sub_blocks:
        return None
    title = SECTION_TITLES[section_key]
    return f"### {title}\n\n" + "\n\n".join(sub_blocks)


def _management_renderer(key: str) -> Callable[[object], str]:
    return _render_pharm_bullet if key == "pharm" else _render_plain_bullet


def _investigations_renderer(_key: str) -> Callable[[object], str]:
    return _render_investigation_bullet


def _plain_renderer(_key: str) -> Callable[[object], str]:
    return _render_plain_bullet


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────


def serialize_to_markdown(sections: AnswerSections) -> str:
    """Render an ``AnswerSections`` instance as markdown matching design §D.2.

    Empty sections, empty sub-sections, and bullets with empty ``text`` are dropped
    (R11.7). Sections are separated by a blank line; the level-3 heading is followed
    by a blank line, then the content.
    """
    blocks: list[str] = []

    # 1. Definition — plain string
    if sections.definition and sections.definition.strip():
        blocks.append(f"### Definition\n\n{sections.definition.strip()}")

    # 2. Classification — list[ClassificationEntry]
    if sections.classification:
        rows = [
            _render_classification_entry(c)
            for c in sections.classification
            if _has_text(c)
        ]
        if rows:
            blocks.append("### Classification\n\n" + "\n".join(rows))

    # 3. Etiology — list[Bullet]
    if sections.etiology:
        rows = [_render_plain_bullet(b) for b in sections.etiology if _has_text(b)]
        if rows:
            blocks.append("### Etiology\n\n" + "\n".join(rows))

    # 4. Pathophysiology — single arrow-joined line
    if sections.pathophysiology:
        line = _render_patho_line(sections.pathophysiology)
        if line:
            blocks.append("### Pathophysiology\n\n" + line)

    # 5. Clinical Features — composite
    cf_block = _render_composite(
        "clinical_features",
        _CLINICAL_FEATURES_SUBS,
        sections.clinical_features,
        _plain_renderer,
    )
    if cf_block is not None:
        blocks.append(cf_block)

    # 6. Investigations — composite (all sub-sections use InvestigationBullet renderer)
    inv_block = _render_composite(
        "investigations",
        _INVESTIGATIONS_SUBS,
        sections.investigations,
        _investigations_renderer,
    )
    if inv_block is not None:
        blocks.append(inv_block)

    # 7. Management — composite (pharm uses PharmBullet renderer; rest plain)
    mg_block = _render_composite(
        "management",
        _MANAGEMENT_SUBS,
        sections.management,
        _management_renderer,
    )
    if mg_block is not None:
        blocks.append(mg_block)

    # 8. Complications — composite
    cp_block = _render_composite(
        "complications",
        _COMPLICATIONS_SUBS,
        sections.complications,
        _plain_renderer,
    )
    if cp_block is not None:
        blocks.append(cp_block)

    # 9. Prognosis — list[Bullet]
    if sections.prognosis:
        rows = [_render_plain_bullet(b) for b in sections.prognosis if _has_text(b)]
        if rows:
            blocks.append("### Prognosis\n\n" + "\n".join(rows))

    return "\n\n".join(blocks)


__all__ = [
    "SECTION_TITLES",
    "SUBSECTION_TITLES",
    "serialize_to_markdown",
]
