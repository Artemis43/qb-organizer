"""Unit tests for ``answers.markdown.serialize_to_markdown`` (R11).

These tests pin the serializer to the executable contract from design
§Markdown Serializer Worked Example (§D.1 input → §D.2 output, byte-for-byte).

The §D.1 JSON and §D.2 markdown strings are hard-coded verbatim from the design
document so the test stands alone without depending on the design file at runtime.
"""
from __future__ import annotations

import json

from answers.markdown import serialize_to_markdown
from answers.schema import AnswerSections


# ──────────────────────────────────────────────────────────────────────────
# §D.1 — Input AnswerSections JSON (verbatim from design.md)
# ──────────────────────────────────────────────────────────────────────────

D1_JSON = r"""
{
  "definition": "Diabetic ketoacidosis (DKA) is an acute metabolic emergency characterized by hyperglycemia (≥250 mg/dL), ketonemia (β-hydroxybutyrate ≥3 mmol/L), and high anion-gap metabolic acidosis (pH <7.30, HCO₃⁻ <18 mEq/L), most commonly precipitated by absolute or relative insulin deficiency in Type 1 DM.",
  "classification": [
    {"text": "ADA 2009 severity classification", "system_name": "ADA", "year": 2009, "categories": ["Mild (pH 7.25–7.30)", "Moderate (pH 7.00–7.24)", "Severe (pH <7.00)"], "source_chunk_ids": ["chunk_a"], "kg_concept_ids": ["c_dka_sev"]}
  ],
  "etiology": [
    {"text": "Missed insulin doses or insulin-pump failure (most common precipitant in known T1DM)", "source_chunk_ids": ["chunk_b"], "kg_concept_ids": ["c_insulin_omission"]},
    {"text": "Infection (UTI, pneumonia, gastroenteritis) raising counter-regulatory hormones", "source_chunk_ids": ["chunk_b"], "kg_concept_ids": ["c_infection_dka"]},
    {"text": "New-onset Type 1 DM presenting with DKA in 25–40% of paediatric cases", "source_chunk_ids": ["chunk_c"], "kg_concept_ids": ["c_t1dm"]}
  ],
  "pathophysiology": [
    {"step_index": 1, "text": "Absolute insulin deficiency removes inhibition of hormone-sensitive lipase", "source_chunk_ids": ["chunk_d"], "kg_concept_ids": ["c_insulin_def"]},
    {"step_index": 2, "text": "Uninhibited lipolysis releases free fatty acids into the hepatic portal circulation", "source_chunk_ids": ["chunk_d"], "kg_concept_ids": ["c_lipolysis"]},
    {"step_index": 3, "text": "Hepatic β-oxidation generates acetyl-CoA, overwhelming TCA cycle capacity", "source_chunk_ids": ["chunk_e"], "kg_concept_ids": ["c_beta_oxidation"]},
    {"step_index": 4, "text": "Surplus acetyl-CoA is diverted to ketogenesis (acetoacetate, β-hydroxybutyrate)", "source_chunk_ids": ["chunk_e"], "kg_concept_ids": ["c_ketogenesis"]},
    {"step_index": 5, "text": "Ketoacids dissociate, producing high anion-gap metabolic acidosis", "source_chunk_ids": ["chunk_f"], "kg_concept_ids": ["c_hagma"]},
    {"step_index": 6, "text": "Compensatory hyperventilation manifests as Kussmaul's breathing", "source_chunk_ids": ["chunk_f"], "kg_concept_ids": ["c_kussmaul"]}
  ],
  "clinical_features": {
    "symptoms": [
      {"text": "Polyuria from osmotic diuresis (glucose >180 mg/dL exceeds renal threshold)", "source_chunk_ids": ["chunk_g"], "kg_concept_ids": ["c_polyuria"]},
      {"text": "Polydipsia from hyperosmolar cellular dehydration", "source_chunk_ids": ["chunk_g"], "kg_concept_ids": ["c_polydipsia"]},
      {"text": "Nausea, vomiting, and abdominal pain (especially in paediatric DKA)", "source_chunk_ids": ["chunk_h"], "kg_concept_ids": ["c_dka_gi"]}
    ],
    "signs": [
      {"text": "Kussmaul's breathing — deep, sighing respirations compensating for HAGMA", "source_chunk_ids": ["chunk_f"], "kg_concept_ids": ["c_kussmaul"]},
      {"text": "Fruity breath odour from acetone exhalation", "source_chunk_ids": ["chunk_i"], "kg_concept_ids": ["c_acetone_breath"]},
      {"text": "Dehydration signs: tachycardia, dry mucosae, prolonged capillary refill", "source_chunk_ids": ["chunk_i"], "kg_concept_ids": ["c_dehydration"]}
    ],
    "hallmarks": [
      {"text": "Triad of hyperglycemia + ketonemia + high anion-gap metabolic acidosis", "source_chunk_ids": ["chunk_j"], "kg_concept_ids": ["c_dka_triad"]}
    ]
  },
  "investigations": {
    "bedside": [
      {"text": "Capillary blood glucose", "test_name": "Capillary glucose", "value_or_range": "≥ 250 mg/dL", "source_chunk_ids": ["chunk_k"], "kg_concept_ids": ["c_cbg"]},
      {"text": "Urine ketones strip", "test_name": "Urine ketones", "value_or_range": "positive (≥ 2+)", "source_chunk_ids": ["chunk_k"], "kg_concept_ids": ["c_urine_ketones"]}
    ],
    "lab": [
      {"text": "Arterial blood gas", "test_name": "Arterial pH", "value_or_range": "< 7.30", "source_chunk_ids": ["chunk_l"], "kg_concept_ids": ["c_abg"]},
      {"text": "Serum bicarbonate", "test_name": "HCO₃⁻", "value_or_range": "< 18 mEq/L", "source_chunk_ids": ["chunk_l"], "kg_concept_ids": ["c_hco3"]},
      {"text": "Serum β-hydroxybutyrate", "test_name": "β-hydroxybutyrate", "value_or_range": "≥ 3 mmol/L", "source_chunk_ids": ["chunk_m"], "kg_concept_ids": ["c_bhb"]}
    ],
    "imaging": [
      {"text": "Chest X-ray to rule out infective precipitant", "test_name": "Chest X-ray", "value_or_range": "no consolidation (negative)", "source_chunk_ids": ["chunk_n"], "kg_concept_ids": ["c_cxr"]}
    ]
  },
  "management": {
    "non_pharm": [
      {"text": "Establish two large-bore IV access lines and monitor hourly vitals", "source_chunk_ids": ["chunk_o"], "kg_concept_ids": ["c_iv_access"]}
    ],
    "pharm": [
      {"text": "Insulin (regular) IV infusion to suppress ketogenesis", "drug_class": "Short-acting insulin", "drug_name": "Insulin regular", "dose": "0.1 U/kg/hr", "frequency": "continuous IV infusion", "source_chunk_ids": ["chunk_p"], "kg_concept_ids": ["c_insulin_regular"]},
      {"text": "Crystalloid resuscitation with isotonic saline", "drug_class": "Isotonic crystalloid", "drug_name": "0.9% NaCl", "dose": "1 L", "frequency": "over first hour", "source_chunk_ids": ["chunk_p"], "kg_concept_ids": ["c_normal_saline"]}
    ],
    "surgical": [],
    "targets": [
      {"text": "Anion gap closure (≤ 12 mEq/L) and resolution of ketonemia within 12–24 hours", "source_chunk_ids": ["chunk_q"], "kg_concept_ids": ["c_dka_target"]}
    ]
  },
  "complications": {
    "acute": [
      {"text": "Cerebral oedema (especially paediatric, mortality 20–40%)", "source_chunk_ids": ["chunk_r"], "kg_concept_ids": ["c_cerebral_oedema"]},
      {"text": "Hypokalemia from insulin-driven intracellular potassium shift", "source_chunk_ids": ["chunk_r"], "kg_concept_ids": ["c_hypokalemia"]}
    ],
    "chronic": []
  },
  "prognosis": [
    {"text": "Mortality 1–5% in adult DKA in Indian tertiary centres; higher in elderly and recurrent DKA", "source_chunk_ids": ["chunk_s"], "kg_concept_ids": ["c_dka_prognosis"]}
  ],
  "mnemonics": {}
}
"""

# ──────────────────────────────────────────────────────────────────────────
# §D.2 — EXACT serializer output (verbatim from design.md)
# ──────────────────────────────────────────────────────────────────────────

D2_MARKDOWN = (
    "### Definition\n"
    "\n"
    "Diabetic ketoacidosis (DKA) is an acute metabolic emergency characterized by hyperglycemia (≥250 mg/dL), ketonemia (β-hydroxybutyrate ≥3 mmol/L), and high anion-gap metabolic acidosis (pH <7.30, HCO₃⁻ <18 mEq/L), most commonly precipitated by absolute or relative insulin deficiency in Type 1 DM.\n"
    "\n"
    "### Classification\n"
    "\n"
    "- **ADA 2009**: Mild (pH 7.25–7.30) / Moderate (pH 7.00–7.24) / Severe (pH <7.00)\n"
    "\n"
    "### Etiology\n"
    "\n"
    "- Missed insulin doses or insulin-pump failure (most common precipitant in known T1DM)\n"
    "- Infection (UTI, pneumonia, gastroenteritis) raising counter-regulatory hormones\n"
    "- New-onset Type 1 DM presenting with DKA in 25–40% of paediatric cases\n"
    "\n"
    "### Pathophysiology\n"
    "\n"
    "1. Absolute insulin deficiency removes inhibition of hormone-sensitive lipase → 2. Uninhibited lipolysis releases free fatty acids into the hepatic portal circulation → 3. Hepatic β-oxidation generates acetyl-CoA, overwhelming TCA cycle capacity → 4. Surplus acetyl-CoA is diverted to ketogenesis (acetoacetate, β-hydroxybutyrate) → 5. Ketoacids dissociate, producing high anion-gap metabolic acidosis → 6. Compensatory hyperventilation manifests as Kussmaul's breathing\n"
    "\n"
    "### Clinical Features\n"
    "\n"
    "#### Symptoms\n"
    "\n"
    "- Polyuria from osmotic diuresis (glucose >180 mg/dL exceeds renal threshold)\n"
    "- Polydipsia from hyperosmolar cellular dehydration\n"
    "- Nausea, vomiting, and abdominal pain (especially in paediatric DKA)\n"
    "\n"
    "#### Signs\n"
    "\n"
    "- Kussmaul's breathing — deep, sighing respirations compensating for HAGMA\n"
    "- Fruity breath odour from acetone exhalation\n"
    "- Dehydration signs: tachycardia, dry mucosae, prolonged capillary refill\n"
    "\n"
    "#### Hallmarks\n"
    "\n"
    "- Triad of hyperglycemia + ketonemia + high anion-gap metabolic acidosis\n"
    "\n"
    "### Investigations\n"
    "\n"
    "#### Bedside\n"
    "\n"
    "- **Capillary glucose** — ≥ 250 mg/dL (diagnostic cutoff)\n"
    "- **Urine ketones** — positive (≥ 2+) (diagnostic cutoff)\n"
    "\n"
    "#### Lab\n"
    "\n"
    "- **Arterial pH** — < 7.30 (diagnostic cutoff)\n"
    "- **HCO₃⁻** — < 18 mEq/L (diagnostic cutoff)\n"
    "- **β-hydroxybutyrate** — ≥ 3 mmol/L (diagnostic cutoff)\n"
    "\n"
    "#### Imaging\n"
    "\n"
    "- **Chest X-ray** — no consolidation (negative) (diagnostic cutoff)\n"
    "\n"
    "### Management\n"
    "\n"
    "#### Non-pharmacological\n"
    "\n"
    "- Establish two large-bore IV access lines and monitor hourly vitals\n"
    "\n"
    "#### Pharmacological\n"
    "\n"
    "- **Insulin regular** (Short-acting insulin) — 0.1 U/kg/hr continuous IV infusion\n"
    "- **0.9% NaCl** (Isotonic crystalloid) — 1 L over first hour\n"
    "\n"
    "#### Targets\n"
    "\n"
    "- Anion gap closure (≤ 12 mEq/L) and resolution of ketonemia within 12–24 hours\n"
    "\n"
    "### Complications\n"
    "\n"
    "#### Acute\n"
    "\n"
    "- Cerebral oedema (especially paediatric, mortality 20–40%)\n"
    "- Hypokalemia from insulin-driven intracellular potassium shift\n"
    "\n"
    "### Prognosis\n"
    "\n"
    "- Mortality 1–5% in adult DKA in Indian tertiary centres; higher in elderly and recurrent DKA"
)


# ──────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────


def _build_d1_sections() -> AnswerSections:
    return AnswerSections.model_validate(json.loads(D1_JSON))


def test_d1_renders_byte_for_byte_to_d2():
    """Design §D.1 input must produce design §D.2 markdown byte-for-byte (R11.1–R11.7)."""
    sections = _build_d1_sections()
    actual = serialize_to_markdown(sections)
    if actual != D2_MARKDOWN:
        # Provide a unified diff to make any future drift easy to debug.
        import difflib
        diff = "\n".join(
            difflib.unified_diff(
                D2_MARKDOWN.splitlines(),
                actual.splitlines(),
                fromfile="design_D2",
                tofile="serializer_output",
                lineterm="",
            )
        )
        raise AssertionError(
            "Serializer output diverged from §D.2.\n"
            f"actual_len={len(actual)} expected_len={len(D2_MARKDOWN)}\n\n{diff}"
        )


def test_empty_subsection_produces_no_heading():
    """An empty ``complications.chronic`` must NOT render a `#### Chronic` heading (R11.7)."""
    sections = _build_d1_sections()
    md = serialize_to_markdown(sections)
    assert "#### Chronic" not in md
    assert "#### Surgical" not in md  # management.surgical is also empty in §D.1


def test_pathophysiology_uses_arrow_joiner():
    """Pathophysiology with N steps renders as a single line with N-1 ``→`` separators (R11.5)."""
    payload = {
        "pathophysiology": [
            {"step_index": 1, "text": "first step"},
            {"step_index": 2, "text": "second step"},
            {"step_index": 3, "text": "third step"},
        ]
    }
    sections = AnswerSections.model_validate(payload)
    md = serialize_to_markdown(sections)
    expected = (
        "### Pathophysiology\n"
        "\n"
        "1. first step → 2. second step → 3. third step"
    )
    assert md == expected
    assert md.count(" → ") == 2


def test_bullet_with_empty_text_is_dropped():
    """A bullet whose ``text`` is an empty string must not appear in the output (R11.7)."""
    payload = {
        "etiology": [
            {"text": "real cause"},
            {"text": "   "},  # whitespace-only — drop
            {"text": ""},     # empty — drop
            {"text": "another real cause"},
        ]
    }
    sections = AnswerSections.model_validate(payload)
    md = serialize_to_markdown(sections)
    assert md == (
        "### Etiology\n"
        "\n"
        "- real cause\n"
        "- another real cause"
    )


def test_module_imports_cleanly():
    """`python -c "from answers.markdown import *"` works and exposes the public API."""
    from answers import markdown as mod

    assert callable(mod.serialize_to_markdown)
    assert "definition" in mod.SECTION_TITLES
    assert mod.SECTION_TITLES["clinical_features"] == "Clinical Features"
    assert mod.SUBSECTION_TITLES["non_pharm"] == "Non-pharmacological"
    assert mod.SUBSECTION_TITLES["pharm"] == "Pharmacological"


if __name__ == "__main__":
    # Allow running the byte-for-byte assertion directly: ``python test_markdown.py``.
    test_d1_renders_byte_for_byte_to_d2()
    test_empty_subsection_produces_no_heading()
    test_pathophysiology_uses_arrow_joiner()
    test_bullet_with_empty_text_is_dropped()
    test_module_imports_cleanly()
    print("OK — all markdown serializer tests passed.")
