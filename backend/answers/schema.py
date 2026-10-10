"""Pydantic v2 AnswerSections schema, leaf bullet models, and auxiliary pipeline types (R1).

Implements design §Data Models → Section_Schema verbatim, plus the auxiliary types
documented in §Data Models → Auxiliary types. All section fields are optional and
empty containers collapse to None via the `_collapse_empty` model validator so the
serialized JSON contains only sections the active Coverage_Mode gated in (R1.6).

Field order on `AnswerSections` follows the LAQ_Skeleton order so JSON key order is
preserved across round-trip (R1.7).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ──────────────────────────────────────────────────────────────────────────
# Section_Schema — leaf bullet models
# ──────────────────────────────────────────────────────────────────────────


class Bullet(BaseModel):
    text: str
    source_chunk_ids: list[str] = Field(default_factory=list)  # R8 traceability, R15
    kg_concept_ids: list[str] = Field(default_factory=list)


class PharmBullet(Bullet):
    drug_class: str
    drug_name: str
    dose: str
    frequency: str


class InvestigationBullet(Bullet):
    test_name: str
    value_or_range: str  # normal range OR diagnostic cutoff (R5.2)


class ClassificationEntry(Bullet):
    system_name: str       # "WHO", "NYHA", "GOLD", "TNM", "Wells", "Killip", ...
    year: int              # R5.3 — must be present
    categories: list[str]


class PathoStep(Bullet):
    step_index: int        # sequential ordering verified by parent (R4.3)


# ──────────────────────────────────────────────────────────────────────────
# Section_Schema — composite section models
# ──────────────────────────────────────────────────────────────────────────


class ClinicalFeatures(BaseModel):
    symptoms: list[Bullet] = Field(default_factory=list)
    signs: list[Bullet] = Field(default_factory=list)
    hallmarks: list[Bullet] = Field(default_factory=list)


class Investigations(BaseModel):
    bedside: list[InvestigationBullet] = Field(default_factory=list)
    lab: list[InvestigationBullet] = Field(default_factory=list)
    imaging: list[InvestigationBullet] = Field(default_factory=list)


class Management(BaseModel):
    non_pharm: list[Bullet] = Field(default_factory=list)
    pharm: list[PharmBullet] = Field(default_factory=list)
    surgical: list[Bullet] = Field(default_factory=list)
    targets: list[Bullet] = Field(default_factory=list)


class Complications(BaseModel):
    acute: list[Bullet] = Field(default_factory=list)
    chronic: list[Bullet] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────
# Section_Schema — top-level AnswerSections
# ──────────────────────────────────────────────────────────────────────────


class AnswerSections(BaseModel):
    """Top-level structured answer. Every section is optional (R1.6). Field order is
    preserved on serialization so the LAQ_Skeleton ordering survives JSON round-trip (R1.7)."""

    definition: Optional[str] = None
    classification: Optional[list[ClassificationEntry]] = None
    etiology: Optional[list[Bullet]] = None
    pathophysiology: Optional[list[PathoStep]] = None
    clinical_features: Optional[ClinicalFeatures] = None
    investigations: Optional[Investigations] = None
    management: Optional[Management] = None
    complications: Optional[Complications] = None
    prognosis: Optional[list[Bullet]] = None

    # Optional per-section mnemonic anchor (R14)
    mnemonics: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _collapse_empty(self) -> "AnswerSections":
        """Per R1.6: any structurally-empty container collapses to None so the
        serialized JSON contains only sections the active Coverage_Mode gated in."""

        def _empty_seq(v) -> bool:
            return v is None or (isinstance(v, list) and len(v) == 0)

        # Composite sections: empty iff every leaf list is empty.
        if self.clinical_features is not None:
            cf = self.clinical_features
            if _empty_seq(cf.symptoms) and _empty_seq(cf.signs) and _empty_seq(cf.hallmarks):
                self.clinical_features = None

        if self.investigations is not None:
            inv = self.investigations
            if _empty_seq(inv.bedside) and _empty_seq(inv.lab) and _empty_seq(inv.imaging):
                self.investigations = None

        if self.management is not None:
            mg = self.management
            if (_empty_seq(mg.non_pharm) and _empty_seq(mg.pharm)
                    and _empty_seq(mg.surgical) and _empty_seq(mg.targets)):
                self.management = None

        if self.complications is not None:
            cp = self.complications
            if _empty_seq(cp.acute) and _empty_seq(cp.chronic):
                self.complications = None

        # Leaf-list sections: empty list collapses to None.
        for fld in ("classification", "etiology", "pathophysiology", "prognosis"):
            v = getattr(self, fld)
            if isinstance(v, list) and len(v) == 0:
                setattr(self, fld, None)

        # `definition` is a plain string — empty string collapses to None.
        if isinstance(self.definition, str) and not self.definition.strip():
            self.definition = None

        return self


# ──────────────────────────────────────────────────────────────────────────
# Auxiliary types (design §Data Models → Auxiliary types)
# ──────────────────────────────────────────────────────────────────────────


class QuestionIntent(BaseModel):
    intent: str                         # one of the R2.1 label set
    focus_topic: str
    requested_item_count: Optional[int] = None
    intent_confidence_low: bool = False


class CoverageMode(BaseModel):
    mode: str                           # vsaq_focused | saq_compressed | saq_narrow_deep | laq_full
    sections_to_populate: list[str]
    per_section_caps: dict[str, int]
    depth_target: str                   # "vsaq" | "saq" | "laq"
    vsaq_disambiguation_fallback: bool = False


class KGSkeleton(BaseModel):
    concept_ids: list[str]
    relation_ids: list[str]
    must_know_concept_ids: list[str]
    skeleton_block_text: str            # the formatted "Concepts: ... Relations: ..." prompt segment
    used: bool                          # false when no concepts matched (R6.5)


class RetrievedContext(BaseModel):
    chunk_ids: list[str]
    chunk_texts: list[str]
    cited_textbook_pages: list[int]
    retrieval_mode: str                 # "vector_only" | "graph_only" | "hybrid"


class Violation(BaseModel):
    section_path: str                   # e.g. "management.pharm[2]"
    code: str                           # MISSING_FREQUENCY | MISSING_CUTOFF | MISSING_YEAR | ...
    detail: str


class MandatoryDataReport(BaseModel):
    ok: bool
    violations: list[Violation] = Field(default_factory=list)


class CriticVerdict(BaseModel):
    verdict: str                        # "pass" | "fail"
    score: int                          # 0–100
    structure_score: int
    depth_score: int
    traceability_score: int
    mandatory_data_score: int
    too_shallow_bullets: list[str] = Field(default_factory=list)
    too_deep_bullets: list[str] = Field(default_factory=list)
    missing_must_know: list[str] = Field(default_factory=list)
    feedback: list[str] = Field(default_factory=list)


class Provenance(BaseModel):
    draft_model: str
    critic_model: str
    regen_model: Optional[str] = None
    escalation_model: Optional[str] = None
    critic_verdict: str
    critic_score: int
    must_know_presented: list[str]
    must_know_covered: list[str]
    sections_populated: list[str]
    retrieval_mode: str
    kg_concept_ids_used: list[str]
    kg_relation_ids_used: list[str]
    cited_textbook_pages: list[int]
    cost_ceiling_blocked: bool = False


class AnswerRecord(BaseModel):
    id: str
    mapping_id: str
    sections: AnswerSections
    markdown: str
    coverage_mode: str
    question_intent: str
    focus_topic: str
    requested_item_count: Optional[int] = None
    intent_confidence_low: bool = False
    mandatory_data_complete: bool = True
    vsaq_disambiguation_fallback: bool = False
    kg_skeleton_used: bool
    provenance: Provenance


class GenerateBatchResult(BaseModel):
    generated: int
    failed: int
    cost_blocked: int
    batch_spend_usd: float
    daily_spend_usd: float
    answer_ids: list[str]
    errors: list[str] = Field(default_factory=list)


class PurgeReport(BaseModel):
    answers_deleted: int
    mappings_reset: int
    firestore_documents_deleted: int
