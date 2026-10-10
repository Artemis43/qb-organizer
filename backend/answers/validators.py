"""Pure-Python mandatory-data structural validators with regex specifications (R5).

Implements design §Mandatory-Data Validator Regex Specifications verbatim:

    B.1  PHARM_DOSE                     -> PHARM_DOSE_RE, PHARM_DOSE_RANGE_RE
    B.2  PHARM_FREQUENCY                -> PHARM_FREQUENCY_RE
    B.3  INVESTIGATION_VALUE_OR_RANGE   -> INV_OP_NUMERIC_RE, INV_RANGE_RE, INV_QUAL_RE
    B.4  CLASSIFICATION_SYSTEM_AND_YEAR -> YEAR_TRAILING_RE, _parse_classification_year
    B.5  BULLET_TEXT_NON_EMPTY          -> BULLET_TEXT_RE

Public surface:

    check_pharm(s)            -> MandatoryDataReport
    check_investigations(s)   -> MandatoryDataReport
    check_classification(s)   -> MandatoryDataReport
    check_bullet_text(s)      -> MandatoryDataReport
    validate_mandatory_data(s) -> MandatoryDataReport     # aggregates all four

All functions are pure: no I/O, no logging side-effects, no module-level state
mutated at runtime. Regex constants compile once at import time.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional, Union

from .schema import (
    AnswerSections,
    Bullet,
    ClassificationEntry,
    InvestigationBullet,
    MandatoryDataReport,
    PharmBullet,
    Violation,
)


# ──────────────────────────────────────────────────────────────────────────
# B.1 — Pharm dose
# ──────────────────────────────────────────────────────────────────────────

PHARM_DOSE_RE = re.compile(
    r"^\s*\d+(\.\d+)?\s*(mg|mcg|µg|g|U|IU|mL|mEq|drops?)\s*(/\s*(kg|day|dose|m²))?\s*$",
    re.IGNORECASE,
)

PHARM_DOSE_RANGE_RE = re.compile(
    r"^\s*\d+(\.\d+)?\s*[–-]\s*\d+(\.\d+)?\s*(mg|mcg|µg|g|U|IU|mL|mEq|drops?)"
    r"\s*(/\s*(kg|day|dose|m²))?\s*$",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────────────
# B.2 — Pharm frequency
# ──────────────────────────────────────────────────────────────────────────

PHARM_FREQUENCY_RE = re.compile(
    r"^(OD|BD|TDS|QID|q\d+h|stat|prn|HS|once daily|twice daily|three times daily|"
    r"four times daily|every \d+\s*(hours?|hrs?|h)|continuous IV infusion)"
    r"(\s*\([^)]+\))?$",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────────────
# B.3 — Investigation cutoff / range / qualitative
# ──────────────────────────────────────────────────────────────────────────

INV_OP_NUMERIC_RE = re.compile(
    r"[<>≥≤=]\s*\d+(\.\d+)?(\s*[a-zA-Zµ%/]+)?"
)

INV_RANGE_RE = re.compile(
    r"\d+(\.\d+)?\s*[–-]\s*\d+(\.\d+)?(\s*[a-zA-Zµ%/]+)?"
)

INV_QUAL_RE = re.compile(
    r"(positive|negative|present|absent|reactive|non[- ]?reactive)",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────────────
# B.4 — Classification system + year
# ──────────────────────────────────────────────────────────────────────────

YEAR_TRAILING_RE = re.compile(r"(\d{4})\s*$")


def _parse_classification_year(raw: Union[str, int, None]) -> Optional[int]:
    """Parse a trailing 4-digit year from a free-text classification value.

    Per design §B.4:
        - If `raw` is already an int, validate it falls in `[1900, current_year]`.
        - If `raw` is a string (possibly with surrounding text like
          "8th edition 2017"), extract the trailing 4-digit token and validate
          its range.
        - Returns None when no valid year is recoverable.
    """
    if isinstance(raw, int):
        return raw if 1900 <= raw <= datetime.now(timezone.utc).year else None
    m = YEAR_TRAILING_RE.search(str(raw or ""))
    if not m:
        return None
    year = int(m.group(1))
    return year if 1900 <= year <= datetime.now(timezone.utc).year else None


# ──────────────────────────────────────────────────────────────────────────
# B.5 — Bullet text non-empty
# ──────────────────────────────────────────────────────────────────────────

BULLET_TEXT_RE = re.compile(r"\S+")


# ──────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────


def _dose_ok(value: str) -> bool:
    return bool(PHARM_DOSE_RE.match(value or "")) or bool(
        PHARM_DOSE_RANGE_RE.match(value or "")
    )


def _frequency_ok(value: str) -> bool:
    return bool(PHARM_FREQUENCY_RE.match(value or ""))


def _investigation_value_ok(value: str) -> bool:
    if not value:
        return False
    return (
        bool(INV_OP_NUMERIC_RE.search(value))
        or bool(INV_RANGE_RE.search(value))
        or bool(INV_QUAL_RE.search(value))
    )


def _bullet_text_ok(value: str) -> bool:
    return bool(BULLET_TEXT_RE.search(value or ""))


# ──────────────────────────────────────────────────────────────────────────
# B.1 + B.2 — check_pharm
# ──────────────────────────────────────────────────────────────────────────


def check_pharm(s: AnswerSections) -> MandatoryDataReport:
    """Verify every `management.pharm` bullet has a drug class, drug name, a
    numeric dose with unit (B.1), and a recognized frequency abbreviation (B.2).

    R5.1: every pharm bullet must contain drug class + drug name + dose + frequency.
    """
    violations: list[Violation] = []

    if s.management is None or not s.management.pharm:
        return MandatoryDataReport(ok=True, violations=[])

    for i, bullet in enumerate(s.management.pharm):
        path = f"management.pharm[{i}]"

        if not (bullet.drug_class or "").strip():
            violations.append(
                Violation(
                    section_path=f"{path}.drug_class",
                    code="MISSING_DRUG_CLASS",
                    detail=f"{path}.drug_class is empty",
                )
            )

        if not (bullet.drug_name or "").strip():
            violations.append(
                Violation(
                    section_path=f"{path}.drug_name",
                    code="MISSING_DRUG_NAME",
                    detail=f"{path}.drug_name is empty",
                )
            )

        if not _dose_ok(bullet.dose):
            violations.append(
                Violation(
                    section_path=f"{path}.dose",
                    code="MISSING_DOSE",
                    detail=(
                        f"{path}.dose '{bullet.dose}' is not a recognized "
                        f"numeric dose with unit"
                    ),
                )
            )

        if not _frequency_ok(bullet.frequency):
            violations.append(
                Violation(
                    section_path=f"{path}.frequency",
                    code="MISSING_FREQUENCY",
                    detail=(
                        f"{path}.frequency '{bullet.frequency}' is not a "
                        f"recognized frequency abbreviation"
                    ),
                )
            )

    return MandatoryDataReport(ok=not violations, violations=violations)


# ──────────────────────────────────────────────────────────────────────────
# B.3 — check_investigations
# ──────────────────────────────────────────────────────────────────────────


def check_investigations(s: AnswerSections) -> MandatoryDataReport:
    """Verify every `investigations.{bedside,lab,imaging}` bullet's
    `value_or_range` carries a recognized cutoff, range, or qualitative result.

    R5.2: every investigation bullet must contain either a normal range or a
    diagnostic cutoff value.
    """
    violations: list[Violation] = []

    if s.investigations is None:
        return MandatoryDataReport(ok=True, violations=[])

    for sub in ("bedside", "lab", "imaging"):
        bullets: list[InvestigationBullet] = getattr(s.investigations, sub) or []
        for i, bullet in enumerate(bullets):
            path = f"investigations.{sub}[{i}]"
            if not _investigation_value_ok(bullet.value_or_range):
                violations.append(
                    Violation(
                        section_path=f"{path}.value_or_range",
                        code="MISSING_CUTOFF",
                        detail=(
                            f"{path}.value_or_range '{bullet.value_or_range}' "
                            f"has no recognized cutoff, range, or qualitative result"
                        ),
                    )
                )

    return MandatoryDataReport(ok=not violations, violations=violations)


# ──────────────────────────────────────────────────────────────────────────
# B.4 — check_classification
# ──────────────────────────────────────────────────────────────────────────


def check_classification(s: AnswerSections) -> MandatoryDataReport:
    """Verify every classification entry has a non-empty `system_name` AND a
    4-digit `year` in `[1900, current_year]`.

    R5.3: each classification bullet must cite the named system and the year.
    """
    violations: list[Violation] = []

    if not s.classification:
        return MandatoryDataReport(ok=True, violations=[])

    current_year = datetime.now(timezone.utc).year

    for i, entry in enumerate(s.classification):
        path = f"classification[{i}]"

        if not (entry.system_name or "").strip():
            violations.append(
                Violation(
                    section_path=f"{path}.system_name",
                    code="MISSING_SYSTEM",
                    detail=f"{path} is missing system_name or has an invalid year",
                )
            )

        year_ok = (
            isinstance(entry.year, int)
            and 1900 <= entry.year <= current_year
        )
        if not year_ok:
            violations.append(
                Violation(
                    section_path=f"{path}.year",
                    code="MISSING_YEAR",
                    detail=f"{path} is missing system_name or has an invalid year",
                )
            )

    return MandatoryDataReport(ok=not violations, violations=violations)


# ──────────────────────────────────────────────────────────────────────────
# B.5 — check_bullet_text
# ──────────────────────────────────────────────────────────────────────────


def _walk_bullets(s: AnswerSections):
    """Yield (section_path, bullet) tuples for every leaf bullet in `s`.

    Pure generator — no side effects.
    """
    if s.classification:
        for i, b in enumerate(s.classification):
            yield f"classification[{i}]", b

    if s.etiology:
        for i, b in enumerate(s.etiology):
            yield f"etiology[{i}]", b

    if s.pathophysiology:
        for i, b in enumerate(s.pathophysiology):
            yield f"pathophysiology[{i}]", b

    if s.clinical_features is not None:
        for sub in ("symptoms", "signs", "hallmarks"):
            bullets = getattr(s.clinical_features, sub) or []
            for i, b in enumerate(bullets):
                yield f"clinical_features.{sub}[{i}]", b

    if s.investigations is not None:
        for sub in ("bedside", "lab", "imaging"):
            bullets = getattr(s.investigations, sub) or []
            for i, b in enumerate(bullets):
                yield f"investigations.{sub}[{i}]", b

    if s.management is not None:
        for sub in ("non_pharm", "pharm", "surgical", "targets"):
            bullets = getattr(s.management, sub) or []
            for i, b in enumerate(bullets):
                yield f"management.{sub}[{i}]", b

    if s.complications is not None:
        for sub in ("acute", "chronic"):
            bullets = getattr(s.complications, sub) or []
            for i, b in enumerate(bullets):
                yield f"complications.{sub}[{i}]", b

    if s.prognosis:
        for i, b in enumerate(s.prognosis):
            yield f"prognosis[{i}]", b


def check_bullet_text(s: AnswerSections) -> MandatoryDataReport:
    """Verify no leaf bullet has whitespace-only `text`.

    Per design §B.5, empty bullets are normally dropped before validation; this
    check only fires when an empty/whitespace bullet slipped through.
    """
    violations: list[Violation] = []

    for path, bullet in _walk_bullets(s):
        if not isinstance(bullet, Bullet):
            continue
        if not _bullet_text_ok(bullet.text):
            violations.append(
                Violation(
                    section_path=path,
                    code="EMPTY_TEXT",
                    detail=f"bullet at {path} has empty or whitespace-only text",
                )
            )

    return MandatoryDataReport(ok=not violations, violations=violations)


# ──────────────────────────────────────────────────────────────────────────
# Aggregator
# ──────────────────────────────────────────────────────────────────────────


def validate_mandatory_data(s: AnswerSections) -> MandatoryDataReport:
    """Run all four mandatory-data checks and aggregate the results.

    Returns a single `MandatoryDataReport` with `ok=True` iff every individual
    check passed and `violations` containing the concatenated violation list in
    `(pharm, investigations, classification, bullet_text)` order.
    """
    aggregated: list[Violation] = []
    for report in (
        check_pharm(s),
        check_investigations(s),
        check_classification(s),
        check_bullet_text(s),
    ):
        aggregated.extend(report.violations)
    return MandatoryDataReport(ok=not aggregated, violations=aggregated)


__all__ = [
    "PHARM_DOSE_RE",
    "PHARM_DOSE_RANGE_RE",
    "PHARM_FREQUENCY_RE",
    "INV_OP_NUMERIC_RE",
    "INV_RANGE_RE",
    "INV_QUAL_RE",
    "YEAR_TRAILING_RE",
    "BULLET_TEXT_RE",
    "_parse_classification_year",
    "check_pharm",
    "check_investigations",
    "check_classification",
    "check_bullet_text",
    "validate_mandatory_data",
]
