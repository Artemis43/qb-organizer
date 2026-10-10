"""Mnemonic dictionary for the medical-grade answer pipeline (R14).

Each entry is keyed by a `topic_key` (snake_case) and contains:
  - acronym: the mnemonic letters (no spaces)
  - expansion: list of phrases, one per letter / component
  - applies_to_section: the AnswerSections key this mnemonic anchors

Looked up by `prompts.build_draft_user_prompt(...)` and the markdown serializer
when (focus_topic, section) matches an entry.
"""
from __future__ import annotations

import re
from typing import Optional

MNEMONICS: dict[str, dict] = {
    "high_anion_gap_acidosis_mudpiles": {
        "acronym": "MUDPILES",
        "expansion": [
            "M — Methanol toxicity",
            "U — Uremia",
            "D — Diabetic ketoacidosis (and other ketoacidoses)",
            "P — Paraldehyde / Propylene glycol",
            "I — Isoniazid / Iron",
            "L — Lactic acidosis",
            "E — Ethylene glycol",
            "S — Salicylates",
        ],
        "applies_to_section": "etiology",
    },
    "high_anion_gap_acidosis_kussmaul": {
        "acronym": "KUSSMAUL",
        "expansion": [
            "K — Ketoacidosis",
            "U — Uremia",
            "S — Salicylates",
            "S — Solvents (methanol, ethylene glycol)",
            "M — Metformin / Methanol",
            "A — Aldehydes (paraldehyde)",
            "U — Unmeasured anions",
            "L — Lactic acidosis",
        ],
        "applies_to_section": "etiology",
    },
    "coma_aeiou_tips": {
        "acronym": "AEIOU TIPS",
        "expansion": [
            "A — Alcohol",
            "E — Epilepsy / Encephalopathy",
            "I — Insulin (hypo/hyperglycemia)",
            "O — Opioids / Overdose",
            "U — Uremia",
            "T — Trauma / Temperature",
            "I — Infection (meningitis, encephalitis, sepsis)",
            "P — Psychiatric / Poisoning",
            "S — Stroke / Shock",
        ],
        "applies_to_section": "etiology",
    },
    "acs_initial_management_mona_b": {
        "acronym": "MONA-B",
        "expansion": [
            "M — Morphine 2–4 mg IV q5–15 min for pain",
            "O — Oxygen if SpO₂ < 90%",
            "N — Nitrates (sublingual GTN 0.4 mg q5 min × 3)",
            "A — Aspirin 162–325 mg chewed",
            "B — Beta-blocker (metoprolol 25 mg PO if no contraindication)",
        ],
        "applies_to_section": "management",
    },
    "pain_history_opqrst": {
        "acronym": "OPQRST",
        "expansion": [
            "O — Onset (sudden vs gradual, activity at onset)",
            "P — Provocation / Palliation",
            "Q — Quality (sharp, dull, crushing, burning)",
            "R — Region / Radiation",
            "S — Severity (1–10 scale)",
            "T — Timing (duration, frequency, pattern)",
        ],
        "applies_to_section": "clinical_features",
    },
    "symptom_analysis_socrates": {
        "acronym": "SOCRATES",
        "expansion": [
            "S — Site",
            "O — Onset",
            "C — Character",
            "R — Radiation",
            "A — Associated symptoms",
            "T — Timing",
            "E — Exacerbating / Relieving factors",
            "S — Severity",
        ],
        "applies_to_section": "clinical_features",
    },
    "stroke_recognition_fast": {
        "acronym": "FAST",
        "expansion": [
            "F — Face drooping",
            "A — Arm weakness",
            "S — Speech difficulty",
            "T — Time to call emergency services",
        ],
        "applies_to_section": "clinical_features",
    },
    "stroke_recognition_befast": {
        "acronym": "BE-FAST",
        "expansion": [
            "B — Balance loss",
            "E — Eyes (sudden vision change)",
            "F — Face drooping",
            "A — Arm weakness",
            "S — Speech difficulty",
            "T — Time to call emergency services",
        ],
        "applies_to_section": "clinical_features",
    },
    "trauma_primary_survey_abcde": {
        "acronym": "ABCDE",
        "expansion": [
            "A — Airway with cervical spine protection",
            "B — Breathing and ventilation",
            "C — Circulation with hemorrhage control",
            "D — Disability (neurological status, GCS)",
            "E — Exposure / Environment control",
        ],
        "applies_to_section": "management",
    },
    "cholinergic_toxidrome_dumbbels": {
        "acronym": "DUMBBELS",
        "expansion": [
            "D — Diarrhea / Diaphoresis",
            "U — Urination",
            "M — Miosis",
            "B — Bronchorrhea / Bronchospasm",
            "B — Bradycardia",
            "E — Emesis",
            "L — Lacrimation",
            "S — Salivation",
        ],
        "applies_to_section": "clinical_features",
    },
    "hyperammonemia_drugs_champ": {
        "acronym": "CHAMP",
        "expansion": [
            "C — Carbamazepine",
            "H — Haloperidol",
            "A — Asparaginase",
            "M — Methotrexate / Metabolic disorders",
            "P — Phenytoin / Pyrimethamine",
        ],
        "applies_to_section": "etiology",
    },
    "compartment_syndrome_5ps": {
        "acronym": "5Ps",
        "expansion": [
            "Pain (out of proportion, on passive stretch)",
            "Pallor",
            "Paresthesia",
            "Paralysis",
            "Pulselessness (late sign)",
        ],
        "applies_to_section": "clinical_features",
    },
    "gallstones_5fs": {
        "acronym": "5Fs",
        "expansion": [
            "Female",
            "Forty (age >40)",
            "Fat (BMI >30)",
            "Fertile (multiparous)",
            "Fair (Caucasian)",
        ],
        "applies_to_section": "etiology",
    },
    "cardiac_arrest_5hs5ts": {
        "acronym": "5Hs and 5Ts",
        "expansion": [
            "H — Hypovolemia",
            "H — Hypoxia",
            "H — Hydrogen ion (acidosis)",
            "H — Hypo/Hyperkalemia",
            "H — Hypothermia",
            "T — Tension pneumothorax",
            "T — Tamponade (cardiac)",
            "T — Toxins",
            "T — Thrombosis (pulmonary embolism)",
            "T — Thrombosis (myocardial infarction)",
        ],
        "applies_to_section": "etiology",
    },
    "acs_risk_score_timi_grace": {
        "acronym": "TIMI / GRACE",
        "expansion": [
            "TIMI — Thrombolysis In Myocardial Infarction risk score (2000) — 7 variables",
            "GRACE — Global Registry of Acute Coronary Events score (2003) — 8 variables",
        ],
        "applies_to_section": "investigations",
    },
    "pneumonia_severity_curb65": {
        "acronym": "CURB-65",
        "expansion": [
            "C — Confusion (AMTS ≤ 8)",
            "U — Urea > 7 mmol/L",
            "R — Respiratory rate ≥ 30/min",
            "B — Blood pressure (SBP < 90 or DBP ≤ 60 mmHg)",
            "65 — Age ≥ 65 years",
        ],
        "applies_to_section": "investigations",
    },
    "dvt_pe_wells_score": {
        "acronym": "Wells",
        "expansion": [
            "Wells score for DVT (1997) — 9 clinical variables, score ≥ 2 = high probability",
            "Wells score for PE (2001) — 7 clinical variables, score > 6 = high probability",
        ],
        "applies_to_section": "investigations",
    },
    "acute_mi_killip_class": {
        "acronym": "Killip",
        "expansion": [
            "I — No clinical signs of heart failure",
            "II — Rales / S3 gallop / elevated JVP",
            "III — Pulmonary edema",
            "IV — Cardiogenic shock",
        ],
        "applies_to_section": "classification",
    },
    "cirrhosis_child_pugh": {
        "acronym": "Child-Pugh",
        "expansion": [
            "Bilirubin (mg/dL)",
            "Albumin (g/dL)",
            "INR",
            "Ascites (none / mild / moderate-severe)",
            "Encephalopathy (none / grade I-II / grade III-IV)",
        ],
        "applies_to_section": "classification",
    },
    "chest_pain_heart_score": {
        "acronym": "HEART",
        "expansion": [
            "H — History",
            "E — ECG",
            "A — Age",
            "R — Risk factors",
            "T — Troponin",
        ],
        "applies_to_section": "investigations",
    },
}


def _normalize(text: str) -> str:
    """Lowercase and collapse runs of non-alphanumeric characters into single
    underscores so callers can pass human-readable focus topics like
    ``"high anion-gap acidosis"`` and have them match the snake_case
    ``MNEMONICS`` keys.
    """
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def find_mnemonic_for(topic_key: str, section: str) -> Optional[dict]:
    """Look up a mnemonic dictionary entry by ``topic_key`` and ``section``.

    The match is permissive on ``topic_key``: the input is normalized to
    snake_case, then an entry matches if its ``MNEMONICS`` key starts with the
    normalized input, contains the normalized input, or is itself contained in
    the normalized input. Entries are filtered to those whose
    ``applies_to_section`` equals ``section``. The first matching entry by
    insertion order is returned, so when several mnemonics anchor the same
    topic + section the dictionary's declaration order picks the canonical one
    (e.g. ``MUDPILES`` for high anion-gap acidosis etiology).

    Args:
        topic_key: A canonical concept name or focus topic, in any case
            (e.g. ``"high anion-gap acidosis"`` or ``"DKA"``).
        section: The :class:`AnswerSections` key being anchored
            (e.g. ``"etiology"``, ``"management"``, ``"investigations"``).

    Returns:
        The matching mnemonic dict, or ``None`` when no entry matches.
    """
    if not topic_key or not section:
        return None
    needle = _normalize(topic_key)
    if not needle:
        return None
    for key, entry in MNEMONICS.items():
        if entry.get("applies_to_section") != section:
            continue
        if key.startswith(needle) or needle in key or key in needle:
            return entry
    return None
