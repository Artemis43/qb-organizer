# Requirements Document

## Introduction

This feature replaces the current essay-style answer generation pipeline (`backend/answers/generator.py`) with a medical-grade pipeline that produces exam answers calibrated to Indian MBBS theory, NEET-PG, and viva-voce standards. The new pipeline emits a structured, section-aware JSON answer (definition → classification → etiology → pathophysiology → clinical features → investigations → management → complications → prognosis) governed by question intent, depth-calibrated to the "fact + 1 linking detail" rule, grounded in the existing knowledge graph (1600+ concepts, 500+ relations), and verified by an examiner-style critic loop.

The authoritative rubric for what counts as "medical-grade" is the LAQ writing guide at `mbbs_laq_answer_writing_guide.html`. Every structural rule, depth example, and mandatory data requirement in this document is derived from that guide. Any conflict between this requirements document and the LAQ guide MUST be resolved in favour of the LAQ guide.

The single most important failure mode this feature exists to fix is **inconsistent depth**: the current pipeline produces bullets that are sometimes too shallow (e.g., "Insulin deficiency → hyperglycemia → DKA") and sometimes too deep (molecular detail beyond standard UG textbooks). Depth calibration (Requirement 4) is therefore the highest-priority requirement.

The pipeline is backend-only in scope. Generated answers are stored in the local SQLite DB as structured JSON plus a serialized markdown rendering, and the existing Firestore push contract (markdown text + image URLs) is preserved so the Android app and admin dashboard require no immediate changes.

## Glossary

- **Answer_Pipeline**: The end-to-end backend system that takes a mapped question and produces a stored medical-grade answer. Replaces the current `generator.py` flow.
- **Question_Intent_Classifier**: The component that classifies a question stem into an intent label (e.g., `define`, `enumerate`, `classify`, `discuss`, `mechanism_of`, `management_of`, `complications_of`, `features_of`, `investigations_of`) and extracts a `focus_topic` string.
- **Section_Schema**: The structured JSON schema for answers, with optional sections: `definition`, `classification`, `etiology`, `pathophysiology`, `clinical_features` (with sub-keys `symptoms`, `signs`, `hallmarks`), `investigations` (with sub-keys `bedside`, `lab`, `imaging`), `management` (with sub-keys `non_pharm`, `pharm`, `surgical`, `targets`), `complications` (with sub-keys `acute`, `chronic`), `prognosis`.
- **Coverage_Mode**: The rule that determines which sections of the Section_Schema are populated, derived from question type (VSAQ/SAQ/LAQ) and question intent.
- **LAQ_Skeleton**: The 9-section structured-essay skeleton from the LAQ guide, in order: Definition → Classification → Etiology → Pathophysiology → Clinical features → Investigations → Management → Complications → Prognosis/Prevention.
- **Depth_Rule**: The "fact + 1 linking detail" calibration rule from the LAQ guide. A bullet must answer "what + how/why" in one compressed sentence and remain at the depth found in standard UG textbooks (e.g., Harrison's, Davidson's, Robbins). Molecular detail beyond UG scope is "too deep"; bare claims with no mechanism are "too shallow".
- **Mandatory_Data**: Section-specific data that the LAQ guide marks as mark-earning and therefore required in the corresponding section: drug class + drug name + dose + frequency in `management.pharm`; normal ranges and diagnostic cutoffs in `investigations`; named classification + year (e.g., WHO 2019, NYHA, GOLD, TNM, Wells, Killip, Child-Pugh, GCS) in `classification`.
- **KG_Skeleton_Block**: A structured prompt segment listing relevant concepts and relations from the knowledge graph in the form `Concepts: ... Relations: A --[relation_type]--> B` that the model is instructed to respect.
- **Must_Know_Concept**: A concept stored in the `concepts` table with `importance = "must_know"`.
- **Examiner_Critic**: A second Claude pass that scores a draft on structure, depth, traceability, and mandatory data, and returns a verdict and feedback.
- **Critic_Verdict**: The `pass | fail` result from the Examiner_Critic, accompanied by a 0–100 score and per-criterion findings.
- **Tiered_Model_Strategy**: The auto-escalation policy that selects Haiku for drafts and critics by default and escalates to Sonnet only when a Haiku critic fails after one retry on SAQ/LAQ.
- **Cost_Ceiling**: A user-configurable per-batch and per-day USD cap that the Answer_Pipeline must enforce before initiating Sonnet escalation or new batches.
- **Provenance_Record**: The metadata block attached to each generated answer, containing model used per stage, critic verdict and score, must-know coverage, sections populated, retrieval mode, KG concepts used, and cited textbook pages.
- **Markdown_Serializer**: The component that converts a Section_Schema instance into the markdown string consumed by the existing Firestore push contract in `qb/firestore_pusher.py`.
- **Purge_Old_Answers_Action**: The one-shot admin action that deletes all rows from the `answers` table and clears any corresponding answer documents from Firestore for the user-selected scope.

## Requirements

### Requirement 1: Section-Aware Answer Schema

**User Story:** As a medical student, I want each generated answer to follow a fixed section-aware schema (definition, classification, etiology, pathophysiology, clinical features, investigations, management, complications, prognosis), so that the structure matches the LAQ skeleton my examiners score against.

#### Acceptance Criteria

1. WHEN the Answer_Pipeline persists an answer, THE Answer_Pipeline SHALL store a `sections` JSON object whose keys are drawn exclusively from the set `{definition, classification, etiology, pathophysiology, clinical_features, investigations, management, complications, prognosis}`.
2. WHERE the `clinical_features` section is populated, THE Answer_Pipeline SHALL structure it with the sub-keys `symptoms`, `signs`, `hallmarks`, each holding an ordered list of bullet objects, and SHALL initialize any sub-key the model omits to an empty array.
3. WHERE the `investigations` section is populated, THE Answer_Pipeline SHALL structure it with the sub-keys `bedside`, `lab`, `imaging`, each holding an ordered list of bullet objects.
4. WHERE the `management` section is populated, THE Answer_Pipeline SHALL structure it with the sub-keys `non_pharm`, `pharm`, `surgical`, `targets`, each holding an ordered list of bullet objects.
5. WHERE the `complications` section is populated, THE Answer_Pipeline SHALL structure it with the sub-keys `acute`, `chronic`, each holding an ordered list of bullet objects.
6. THE Answer_Pipeline SHALL treat every section as optional, omitting any section that is not gated in by the active Coverage_Mode rather than emitting an empty placeholder.
7. WHEN a section is populated, THE Answer_Pipeline SHALL preserve the LAQ_Skeleton ordering (definition → classification → etiology → pathophysiology → clinical_features → investigations → management → complications → prognosis) in the serialized JSON key order.
8. THE Answer_Pipeline SHALL store each bullet as an object with the keys `text` (string) and `source_chunk_ids` (array of chunk IDs).
9. IF the model returns a section key outside the schema in Acceptance Criterion 1, THEN THE Answer_Pipeline SHALL drop the unknown key and log a schema-violation warning.

### Requirement 2: Question Intent Classification

**User Story:** As a medical student, I want the system to detect what the question actually asks for (define, enumerate, discuss the mechanism of, list complications of, etc.), so that the answer focuses on the requested aspect instead of dumping a generic essay.

#### Acceptance Criteria

1. WHEN a mapping enters the Answer_Pipeline, THE Question_Intent_Classifier SHALL produce a `question_intent` value drawn from the set `{define, enumerate, classify, discuss, mechanism_of, management_of, complications_of, features_of, investigations_of, etiology_of, prognosis_of, differentiate, compare}`.
2. WHEN a mapping enters the Answer_Pipeline, THE Question_Intent_Classifier SHALL produce a `focus_topic` string extracted from the question stem.
3. THE Question_Intent_Classifier SHALL persist `question_intent` and `focus_topic` on the answer record.
4. IF the Question_Intent_Classifier cannot confidently assign one of the labels in Acceptance Criterion 1, THEN THE Question_Intent_Classifier SHALL assign the label `discuss` regardless of any best-guess label and SHALL persist a `intent_confidence_low = true` flag on the answer record.
5. WHEN the question stem contains an explicit count (e.g., "Enumerate 4 causes of...", "List 5 investigations for..."), THE Question_Intent_Classifier SHALL extract that count into a `requested_item_count` integer field independently of the value of `intent_confidence_low`.

### Requirement 3: Coverage Mode Selection

**User Story:** As a medical student, I want VSAQs to give me only the section I asked for, SAQs to give a compressed full skeleton, and LAQs to give the full deep skeleton, so that answer length matches the marks the question carries.

#### Acceptance Criteria

1. WHEN the question type is `VSAQ`, THE Answer_Pipeline SHALL populate only the section(s) implied by the `question_intent` (e.g., `features_of` → only `clinical_features`; `management_of` → only `management`; `investigations_of` → only `investigations`; `complications_of` → only `complications`; `define` → only `definition`).
2. WHEN the question type is `VSAQ` AND `requested_item_count` is set, THE Answer_Pipeline SHALL emit exactly `requested_item_count` bullets in the populated section.
3. WHEN the question type is `SAQ` AND the `question_intent` is `discuss` or unspecified, THE Answer_Pipeline SHALL populate every section in the LAQ_Skeleton with at most 2 bullets per leaf list.
4. WHEN the question type is `SAQ` AND the `question_intent` targets a single section (e.g., `mechanism_of`, `management_of`, `features_of`, `investigations_of`, `complications_of`), THE Answer_Pipeline SHALL populate only the targeted section at LAQ depth and SHALL omit every other section.
5. WHEN the question type is `LAQ`, THE Answer_Pipeline SHALL populate every section of the LAQ_Skeleton that is relevant to the `focus_topic`.
6. WHEN the question type is `LAQ` AND the `question_intent` is `discuss` or unspecified, THE Answer_Pipeline SHALL populate the `definition`, `classification`, `etiology`, `pathophysiology`, `clinical_features`, `investigations`, `management`, `complications`, and `prognosis` sections, and the `definition` section SHALL always be populated for any LAQ with `discuss` or unspecified intent.
7. THE Answer_Pipeline SHALL store the resolved Coverage_Mode (`vsaq_focused`, `saq_compressed`, `saq_narrow_deep`, `laq_full`) on the answer record.

### Requirement 4: Depth Calibration via Prompt Rules

**User Story:** As a medical student, I want every bullet to be "fact + 1 linking detail" — no shallow claims, no molecular over-detail — so that each bullet earns the half-mark the examiner is looking for.

#### Acceptance Criteria

1. THE Answer_Pipeline SHALL include the Depth_Rule in the system prompt for every draft generation call.
2. THE Answer_Pipeline SHALL include in the system prompt the three worked DKA pathophysiology examples from the LAQ guide labelled `too_shallow`, `correct`, and `too_deep`, as few-shot calibration examples.
3. THE Answer_Pipeline SHALL include in the system prompt the LAQ guide rules that bullets in `clinical_features` MUST be self-contained (each independently scoreable) and bullets in `pathophysiology` MUST be sequential and linked by arrows showing cause → effect.
4. THE Answer_Pipeline SHALL instruct the model in the system prompt that depth ceiling is "mechanism as found in Harrison's or Davidson's standard UG textbooks" and that detail beyond standard UG textbooks is out of scope.
5. THE Answer_Pipeline SHALL instruct the model in the system prompt to never produce a bullet that asserts a fact without a mechanism, cause, value, or qualifier (i.e., no shallow bullets such as "Insulin deficiency leads to DKA").
6. THE Examiner_Critic SHALL score a `depth_score` from 0 to 100 for each draft, derived from the proportion of bullets that satisfy the Depth_Rule.
7. IF the `depth_score` is below 70, THEN THE Examiner_Critic SHALL emit a `fail` Critic_Verdict with the bullets that violated the Depth_Rule listed under `too_shallow_bullets` or `too_deep_bullets`, classifying any bullet whose violation type is undetermined (including the boundary case of an overall zero score) as `too_shallow_bullets`.
8. WHEN the `depth_score` is exactly 70, THE Examiner_Critic SHALL treat the depth criterion as passing.

### Requirement 5: Mandatory Data Enforcement

**User Story:** As a medical student, I want the answer to always include drug doses with frequencies in management, normal ranges and cutoffs in investigations, and named classifications with year in the classification section, so that I score the marks the LAQ guide says are awarded for these specific facts.

#### Acceptance Criteria

1. WHERE the `management.pharm` section is populated, THE Answer_Pipeline SHALL ensure every bullet contains a drug class, a specific drug name, a numeric dose, and a frequency.
2. WHERE the `investigations` section is populated, THE Answer_Pipeline SHALL ensure every bullet contains either a normal range or a diagnostic cutoff value.
3. WHERE the `classification` section is populated, THE Answer_Pipeline SHALL ensure each classification bullet cites the named system and the year (e.g., "WHO 2019", "NYHA 1994", "TNM 8th edition 2017").
4. WHEN the draft passes the Mandatory_Data structural validator, THE Answer_Pipeline SHALL persist the answer.
5. IF a draft fails the Mandatory_Data structural validator, THEN THE Answer_Pipeline SHALL re-prompt the model with the specific missing items listed (e.g., "investigations bullet 3 is missing a cutoff", "management.pharm bullet 1 is missing a frequency") and request a corrected version of only the failing section.
6. THE Answer_Pipeline SHALL limit re-prompts triggered by Acceptance Criterion 5 to one attempt per failing section per draft.
7. IF the Mandatory_Data structural validator still fails after at least one re-prompt attempt under Acceptance Criterion 5, THEN THE Answer_Pipeline SHALL persist the draft, set `mandatory_data_complete = false` on the answer record, and list the missing items in the Provenance_Record.
8. THE Answer_Pipeline SHALL NOT persist a draft that has failed the Mandatory_Data structural validator without first attempting at least one re-prompt under Acceptance Criterion 5.

### Requirement 6: Restructured Knowledge Graph Context

**User Story:** As a medical student, I want the answer to follow the relationships my knowledge graph already encodes (causes, presents_with, treated_by, investigated_by, complication_of), so that the answer reflects the same structure as my study notes instead of dumping unrelated chunks.

#### Acceptance Criteria

1. WHEN building a generation prompt, THE Answer_Pipeline SHALL include a KG_Skeleton_Block that lists the matched concepts followed by their 1-hop relations, formatted as `Concepts: name (concept_type), ...` and `Relations: source --[relation_type]--> target` lines.
2. THE Answer_Pipeline SHALL retrieve KG_Skeleton_Block content using the existing `get_extended_concepts_for_question` and `get_related_chunk_ids` functions in `backend/knowledge/graph_builder.py`.
3. THE Answer_Pipeline SHALL instruct the model in the system prompt that the KG_Skeleton_Block represents the structural backbone of the topic and that the answer must respect the relationships shown (e.g., causes listed in the skeleton must appear in `etiology`; `presents_with` relations must appear in `clinical_features`; `treated_by` relations must appear in `management`).
4. THE Answer_Pipeline SHALL persist the list of KG concept IDs and relation IDs that contributed to the prompt on the answer record.
5. IF no concepts are matched for a question, THEN THE Answer_Pipeline SHALL fall back to vector-only retrieval and SHALL set `kg_skeleton_used = false` on the answer record.
6. WHEN at least one concept is matched, THE Answer_Pipeline SHALL include the KG_Skeleton_Block in the prompt regardless of other system states.

### Requirement 7: Must-Know Concept Gating

**User Story:** As a medical student, I want concepts tagged `must_know` in the knowledge graph to always appear in the answer when relevant, so that the high-yield exam-critical content never gets dropped.

#### Acceptance Criteria

1. WHEN building a generation prompt, THE Answer_Pipeline SHALL identify the subset of matched concepts whose `importance` field equals `must_know` and SHALL list them in the prompt under a heading "MUST APPEAR IN ANSWER IF RELEVANT".
2. THE Answer_Pipeline SHALL boost the retrieval ranking of chunks linked to Must_Know_Concepts so that they are not dropped by the chunk count cap.
3. THE Examiner_Critic SHALL verify that every concept listed in the "MUST APPEAR IN ANSWER IF RELEVANT" block is referenced (by name or by canonical alias) in at least one bullet of the draft.
4. IF a Must_Know_Concept that was listed in the prompt is absent from the draft, THEN THE Examiner_Critic SHALL include the missing concept name in the `missing_must_know` field of the Critic_Verdict.
5. THE Answer_Pipeline SHALL persist the list of Must_Know_Concepts presented in the prompt and the list of Must_Know_Concepts actually covered in the final answer on the Provenance_Record.

### Requirement 8: Examiner Critic Loop

**User Story:** As a medical student, I want a second pass that scores the draft like an examiner and triggers a regenerate if the draft is weak, so that I receive answers that have been peer-reviewed before reaching me.

#### Acceptance Criteria

1. WHEN a draft is produced for an SAQ or LAQ question, THE Examiner_Critic SHALL evaluate the draft on four criteria: structure (sections present per active Coverage_Mode), depth (Depth_Rule compliance per Requirement 4), traceability (every claim linked to at least one retrieved chunk ID or KG concept ID), and mandatory data (per Requirement 5).
2. THE Examiner_Critic SHALL emit a Critic_Verdict containing a `verdict` field (`pass` or `fail`), a `score` integer from 0 to 100, per-criterion sub-scores `structure_score`, `depth_score`, `traceability_score`, `mandatory_data_score`, and a `feedback` array of free-text findings.
3. THE Examiner_Critic SHALL emit `verdict = pass` only when all four sub-scores are ≥ 70.
4. WHEN the Critic_Verdict is `fail`, THE Answer_Pipeline SHALL regenerate the draft once with the Critic_Verdict feedback included in the prompt as targeted instructions.
5. THE Answer_Pipeline SHALL limit critic-driven regeneration to a maximum of one retry per question.
6. THE Answer_Pipeline SHALL persist the final Critic_Verdict (after the retry, if any) on the Provenance_Record.
7. WHERE the question type is `VSAQ`, THE Answer_Pipeline SHALL run a lightweight critic pass that evaluates only structure and mandatory data, SHALL skip depth and traceability scoring, and SHALL emit `verdict = pass` only when both `structure_score` and `mandatory_data_score` are ≥ 70.

### Requirement 9: Tiered Model Strategy with Cost Ceiling

**User Story:** As a medical student paying for my own API usage, I want the cheap Haiku model to handle most generations and Sonnet to be used only when Haiku fails, with a daily and per-batch USD cap I can configure, so that I get high quality without losing control of cost.

#### Acceptance Criteria

1. WHEN the question type is `VSAQ`, THE Answer_Pipeline SHALL produce the draft with the Haiku model and SHALL NOT escalate to the Sonnet model.
2. WHEN the question type is `SAQ` or `LAQ`, THE Answer_Pipeline SHALL produce the initial draft with the Haiku model and SHALL run the Examiner_Critic with the Haiku model.
3. WHEN the Critic_Verdict for an SAQ or LAQ draft is `fail`, THE Answer_Pipeline SHALL regenerate once with the Haiku model.
4. IF the Critic_Verdict on the Haiku regeneration is still `fail`, THEN THE Answer_Pipeline SHALL escalate by regenerating once with the Sonnet model.
5. THE Answer_Pipeline SHALL read the per-batch and per-day Cost_Ceiling values from the existing `settings` configuration in `backend/config.py`.
6. IF a planned Sonnet escalation would push the per-batch spend or the per-day spend above the configured Cost_Ceiling, THEN THE Answer_Pipeline SHALL skip the Sonnet escalation, persist the latest Haiku draft, and set `cost_ceiling_blocked = true` on the Provenance_Record.
7. THE Answer_Pipeline SHALL persist, for each answer, the model used at each stage (`draft_model`, `critic_model`, `regen_model`, `escalation_model`).
8. THE Answer_Pipeline SHALL expose the running per-batch spend and per-day spend through the existing answer-generation progress channel so that the UI in `frontend/app/answers/page.js` can display current spend.

### Requirement 10: Drop-and-Regenerate Migration

**User Story:** As a medical student who already knows the old answers are unusable, I want a one-shot admin action that purges every existing answer so that the new pipeline regenerates from scratch on the new schema without legacy data lingering.

#### Acceptance Criteria

1. THE Answer_Pipeline SHALL expose a `Purge_Old_Answers_Action` admin endpoint that accepts an optional subject filter.
2. WHEN the Purge_Old_Answers_Action is invoked without a subject filter, THE Answer_Pipeline SHALL delete every row in the `answers` table.
3. WHEN the Purge_Old_Answers_Action is invoked with a subject filter, THE Answer_Pipeline SHALL delete only the rows in the `answers` table whose mapping belongs to the specified subject.
4. WHEN the Purge_Old_Answers_Action completes, THE Answer_Pipeline SHALL set `is_answered = false` on every row in the `mappings` table that was within the action's scope, regardless of whether any answer rows existed for those mappings.
5. WHEN the Purge_Old_Answers_Action completes AND a Firestore service account is configured, THE Answer_Pipeline SHALL delete the corresponding documents in the Firestore `answers` collection that match the `mappingId` of every purged row.
6. THE Answer_Pipeline SHALL return a summary report containing `answers_deleted`, `mappings_reset`, and `firestore_documents_deleted`.
7. IF the user invokes the Purge_Old_Answers_Action, THEN THE Answer_Pipeline SHALL require an explicit confirmation flag (`confirm = true`) in the request payload before performing any deletion.

### Requirement 11: Markdown Serialization for Existing Consumers

**User Story:** As a developer of the Android app and admin dashboard, I want the new pipeline to keep producing the markdown text the existing Firestore push contract sends, so that the consumer apps continue working unchanged while the structured JSON lives only in the backend.

#### Acceptance Criteria

1. WHEN the Answer_Pipeline persists an answer, THE Markdown_Serializer SHALL also produce a markdown rendering of the Section_Schema and SHALL persist it in a `markdown` column alongside the structured `sections` JSON.
2. THE Markdown_Serializer SHALL render each populated top-level section with a level-3 heading whose text is the human-readable section name (e.g., "Definition", "Classification", "Pathophysiology", "Clinical Features", "Investigations", "Management", "Complications", "Prognosis").
3. THE Markdown_Serializer SHALL render leaf bullet lists as markdown unordered lists using `- ` prefixes.
4. THE Markdown_Serializer SHALL render sub-section headings (`symptoms`, `signs`, `hallmarks`, `bedside`, `lab`, `imaging`, `non_pharm`, `pharm`, `surgical`, `targets`, `acute`, `chronic`) as level-4 headings whose text is the human-readable sub-section name.
5. THE Markdown_Serializer SHALL emit pathophysiology bullets joined by ` → ` arrow separators when the source section uses sequential bullet style.
6. WHEN the Firestore pusher reads an answer, THE Firestore pusher SHALL push the `markdown` column value as the `text` field, preserving the existing Firestore document shape consumed by the Android app.
7. THE Markdown_Serializer SHALL exclude empty sections, empty sub-sections, and bullets whose `text` is empty.

### Requirement 12: Confidence and Provenance Metadata

**User Story:** As a medical student reviewing generated answers in the admin dashboard, I want to see for each answer which model produced it, the critic score, which must-know concepts were covered, which sections were populated, and which textbook pages were cited, so that I can trust or override the answer.

#### Acceptance Criteria

1. WHEN the Answer_Pipeline persists an answer, THE Answer_Pipeline SHALL store a `provenance` JSON object containing the keys `draft_model`, `critic_model`, `regen_model`, `escalation_model`, `critic_verdict`, `critic_score`, `must_know_presented`, `must_know_covered`, `sections_populated`, `retrieval_mode`, `kg_concept_ids_used`, `kg_relation_ids_used`, `cited_textbook_pages`, and `cost_ceiling_blocked`.
2. THE Answer_Pipeline SHALL set `sections_populated` to the ordered list of section keys that have at least one non-empty leaf bullet.
3. THE Answer_Pipeline SHALL set `cited_textbook_pages` to the deduplicated, sorted list of page numbers referenced by chunks whose IDs appear in any bullet's `source_chunk_ids`.
4. THE Answer_Pipeline SHALL set `must_know_covered` to the subset of `must_know_presented` whose canonical name or any alias appears in at least one bullet of the final answer.
5. THE Answer_Pipeline SHALL expose the `provenance` object through the existing answer-fetch API endpoint that `frontend/app/answers/page.js` consumes.
6. WHEN the UI in `frontend/app/answers/page.js` renders an answer card, THE UI SHALL display both a status pill derived from `critic_verdict` AND a numeric badge derived from `critic_score`, never one without the other.
7. WHEN the UI in `frontend/app/answers/page.js` renders an answer card, THE UI SHALL display the `must_know_covered` count over the `must_know_presented` count (e.g., "5 / 6 must-know covered").

### Requirement 13: VSAQ Section-Specific Output

**User Story:** As a medical student answering VSAQs that ask only about treatment or only about symptoms, I want the answer to contain only that section, so that my notes match the question scope.

#### Acceptance Criteria

1. WHEN the question type is `VSAQ` AND `question_intent` is `features_of`, THE Answer_Pipeline SHALL populate only the `clinical_features` section.
2. WHEN the question type is `VSAQ` AND `question_intent` is `management_of`, THE Answer_Pipeline SHALL populate only the `management` section.
3. WHEN the question type is `VSAQ` AND `question_intent` is `investigations_of`, THE Answer_Pipeline SHALL populate only the `investigations` section.
4. WHEN the question type is `VSAQ` AND `question_intent` is `complications_of`, THE Answer_Pipeline SHALL populate only the `complications` section.
5. WHEN the question type is `VSAQ` AND `question_intent` is `define`, THE Answer_Pipeline SHALL populate only the `definition` section.
6. WHEN the question type is `VSAQ` AND `question_intent` is `enumerate`, THE Answer_Pipeline SHALL populate exactly one section, namely the single section whose name most closely matches the `focus_topic` (e.g., "Enumerate causes of X" → `etiology`; "Enumerate types of X" → `classification`; "Enumerate symptoms of X" → `clinical_features`).
7. IF the question type is `VSAQ` AND no single section can be inferred from `question_intent` and `focus_topic`, THEN THE Answer_Pipeline SHALL populate the `definition` section and the section whose name most closely matches the focus, and SHALL set `vsaq_disambiguation_fallback = true` on the answer record.

### Requirement 14: Mnemonics as Organizational Anchors

**User Story:** As a medical student, I want recognised mnemonics (MUDPILES, AEIOU TIPS, etc.) to appear as the organizational anchor of their section with each letter expanded inline, so that I get the LAQ guide's mark bonus for using mnemonics correctly.

#### Acceptance Criteria

1. WHERE a section maps to a recognised medical mnemonic (e.g., MUDPILES for high anion-gap acidosis causes, AEIOU TIPS for coma causes), THE Answer_Pipeline SHALL instruct the model in the system prompt to write the mnemonic acronym first and then list bullets in mnemonic-letter order with each letter expanded inline.
2. THE Answer_Pipeline SHALL store the mnemonic acronym used (if any) in a `mnemonic` field on the section record.
3. IF no recognised mnemonic applies to a section, THEN THE Answer_Pipeline SHALL omit the `mnemonic` field for that section.

### Requirement 15: Traceability of Claims to Sources

**User Story:** As a medical student, I want every bullet to be linked to the textbook chunks or KG concepts it came from, so that I can verify the claim and the critic can detect ungrounded statements.

#### Acceptance Criteria

1. THE Answer_Pipeline SHALL ensure every bullet's `source_chunk_ids` contains at least one chunk ID from the retrieved context OR at least one concept ID from the KG_Skeleton_Block.
2. THE Examiner_Critic SHALL flag a bullet as ungrounded if its `source_chunk_ids` is empty.
3. THE Examiner_Critic SHALL compute `traceability_score` as the percentage of bullets that satisfy Acceptance Criterion 1.
4. WHEN `traceability_score` is below 70, THE Examiner_Critic SHALL emit `verdict = fail` regardless of other sub-scores.
5. THE Answer_Pipeline SHALL persist the count of ungrounded bullets on the Provenance_Record under the field `ungrounded_bullet_count`.
