# qb-organizer/backend/knowledge/graph_builder.py
"""Knowledge Graph Builder — builds the concept mesh from ingested textbook data.

Pipeline per textbook:
  1. For each chapter, fetch its chunks and Claude-extracted topics/key_terms
  2. Send to Claude batch → extract typed concepts + relationships + definitions
  3. Deduplicate against existing graph (fuzzy canonical name match)
  4. Store concepts, relations, and provenance (concept_sources)

Design decisions:
  - Uses Claude Haiku batch (cheap, ~$0.003/chapter) — no SciSpacy dependency
  - All extraction is LLM-based for highest accuracy on medical domain
  - Merges across textbooks: same canonical_name → existing concept + new source
  - Relation types are medical-education specific (is_subtype_of, causes, etc.)
"""

import json
import logging
import uuid
import re
from datetime import datetime, timezone

from config import settings
from claude.client import get_claude_client
from state import db as database
from state.checkpoint import Checkpoint

logger = logging.getLogger(__name__)

# ── Relation Types ────────────────────────────────────────────────

VALID_RELATION_TYPES = {
    "is_subtype_of",      # Sickle Cell Anemia → Anemia
    "causes",             # H. pylori → Peptic Ulcer
    "presents_with",      # Appendicitis → RIF Pain
    "treated_by",         # Fracture → Open Reduction
    "investigated_by",    # Anemia → CBC, Peripheral Smear
    "complication_of",    # DVT → Fracture
    "associated_with",    # Diabetes → Atherosclerosis
    "part_of",            # Femur → Lower Limb Bones
    "differential_of",   # Appendicitis ↔ Ectopic Pregnancy
    "risk_factor_for",    # Smoking → Lung Cancer
    "synonym_of",         # SCA → Sickle Cell Anemia
    "precedes",           # Dysplasia → Malignancy
    "managed_by",         # Shock → IV Fluids, O2
    "produces",           # Beta cells → Insulin
    "metabolized_by",     # Bilirubin → Liver
    "innervated_by",      # Deltoid → Axillary nerve
    "vascularized_by",    # Heart → Coronary arteries
    "contraindicated_in", # Aspirin → Peptic ulcer
    "indicated_in",       # Antibiotics → Infection
    "side_effect_of",     # Cough → ACE inhibitors
}

VALID_CONCEPT_TYPES = {
    "disease", "anatomy", "procedure", "drug", "symptom",
    "investigation", "pathology", "physiology", "organism",
    "syndrome", "sign", "condition", "concept", "other"
}

# ── Claude Prompts ────────────────────────────────────────────────

CONCEPT_EXTRACTION_SYSTEM = """You are a senior medical professor and ontologist building a structured medical knowledge graph from MBBS textbook content.

Your task: Extract ALL medical concepts from the given chapter text and identify relationships between them.

CONCEPT TYPES (use exactly):
- disease: pathological conditions (e.g., Anemia, Appendicitis, Fracture)
- anatomy: body structures (e.g., Femur, Brachial Plexus, Mitral Valve)
- procedure: surgical/clinical procedures (e.g., Open Reduction, Appendectomy)
- drug: medications and drug classes (e.g., Warfarin, NSAIDs, Antibiotics)
- symptom: patient complaints and symptoms (e.g., Pain, Fever, Dyspnea)
- investigation: diagnostic tests (e.g., CBC, X-ray, CT Scan, Biopsy)
- pathology: pathological processes/mechanisms (e.g., Inflammation, Necrosis, Fibrosis)
- physiology: normal processes (e.g., Haemostasis, Coagulation Cascade)
- organism: pathogens and organisms (e.g., Staphylococcus, H. pylori)
- syndrome: named syndromes (e.g., Crush Syndrome, Compartment Syndrome)
- sign: clinical signs (e.g., Trendelenburg Sign, Murphy's Sign)
- condition: states/conditions (e.g., Shock, Sepsis, Pregnancy)
- concept: abstract medical concepts (e.g., Virulence, Immunity, Homeostasis)

RELATION TYPES (use exactly):
- is_subtype_of: hierarchical (child → parent)
- causes: agent causes disease/condition
- presents_with: disease presents with symptom/sign
- treated_by: disease/condition → treatment/procedure/drug
- investigated_by: condition → diagnostic test
- complication_of: complication → primary disease/procedure
- associated_with: bidirectional association
- part_of: anatomy → larger structure
- differential_of: differential diagnosis relationship
- risk_factor_for: risk factor → disease
- precedes: temporal/causal sequence
- managed_by: general management
- produces: physiological production (e.g., organ/cell produces hormone/substance)
- metabolized_by: metabolic clearance (e.g., substance metabolized by organ/enzyme)
- innervated_by: nervous supply (e.g., muscle/structure innervated by nerve)
- vascularized_by: blood supply (e.g., organ/structure vascularized by artery/vein)
- contraindicated_in: warning/danger (e.g., drug contraindicated in condition/disease)
- indicated_in: clinical indication (e.g., drug/procedure indicated in condition/disease)
- side_effect_of: adverse effect (e.g., symptom/sign side effect of drug)

CRITICAL RULES:
1. Extract ALL concepts mentioned — from "bone" to "Osteosarcoma"
2. Be specific: "Sickle Cell Anemia" not just "Anemia" for the specific concept
3. Both go in: specific concept AND its parent (with is_subtype_of relation)
4. aliases: common abbreviations and synonyms (e.g., ["SCA", "HbSS disease"])
5. importance: must_know (exam critical), standard (important), advanced (extra)
6. definition: 1 sentence from the textbook content, NOT external knowledge

Respond with ONLY valid JSON — no markdown, no explanation:
{
  "concepts": [
    {
      "name": "Sickle Cell Anemia",
      "concept_type": "disease",
      "definition": "A hereditary hemolytic anemia caused by HbS mutation leading to sickling of RBCs.",
      "aliases": ["SCA", "HbSS disease", "Sickle cell disease"],
      "importance": "must_know"
    }
  ],
  "relations": [
    {
      "source": "Sickle Cell Anemia",
      "target": "Anemia",
      "relation_type": "is_subtype_of",
      "confidence": 0.98
    },
    {
      "source": "Sickle Cell Anemia",
      "target": "Hemolysis",
      "relation_type": "causes",
      "confidence": 0.95
    }
  ]
}"""


def _normalize(name: str) -> str:
    """Normalize concept name for deduplication."""
    return re.sub(r'\s+', ' ', name.strip().lower())


async def _find_or_create_concept(
    name: str,
    concept_type: str,
    definition: str,
    aliases: list,
    importance: str,
    subject: str,
) -> tuple[str, bool]:
    """Find existing concept by canonical name or create new one.

    Returns (concept_id, is_new).
    Uses fuzzy matching on canonical_name and aliases for deduplication.
    """
    canonical = _normalize(name)

    # Exact canonical match
    existing = await database.fetch_all(
        "concepts",
        "canonical_name = ? AND subject = ?",
        (canonical, subject),
    )
    if existing:
        # Update frequency and merge aliases
        c = existing[0]
        new_freq = (c.get("frequency") or 1) + 1
        existing_aliases = json.loads(c.get("aliases") or "[]")
        merged_aliases = list(set(existing_aliases + aliases))
        await database.update("concepts", c["id"], {
            "frequency": new_freq,
            "aliases": merged_aliases,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return c["id"], False

    # Check aliases for match
    for alias in aliases:
        alias_canonical = _normalize(alias)
        if alias_canonical == canonical:
            continue
        existing_by_alias = await database.fetch_all(
            "concepts",
            "canonical_name = ? AND subject = ?",
            (alias_canonical, subject),
        )
        if existing_by_alias:
            c = existing_by_alias[0]
            # Add current name as alias to existing concept
            ex_aliases = json.loads(c.get("aliases") or "[]")
            if name not in ex_aliases:
                ex_aliases.append(name)
            await database.update("concepts", c["id"], {
                "frequency": (c.get("frequency") or 1) + 1,
                "aliases": ex_aliases,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            return c["id"], False

    # Create new concept
    concept_id = f"c_{uuid.uuid4().hex[:12]}"
    await database.insert("concepts", {
        "id": concept_id,
        "name": name,
        "canonical_name": canonical,
        "concept_type": concept_type if concept_type in VALID_CONCEPT_TYPES else "other",
        "definition": definition or "",
        "aliases": aliases or [],
        "subject": subject,
        "importance": importance if importance in ("must_know", "standard", "advanced") else "standard",
        "frequency": 1,
    })
    return concept_id, True


async def _upsert_relation(
    source_id: str,
    target_id: str,
    relation_type: str,
    confidence: float,
    extracted_by: str = "claude",
) -> bool:
    """Create a relation if it doesn't already exist."""
    if relation_type not in VALID_RELATION_TYPES:
        return False
    if source_id == target_id:
        return False

    # Check for duplicate
    existing = await database.fetch_all(
        "concept_relations",
        "source_id = ? AND target_id = ? AND relation_type = ?",
        (source_id, target_id, relation_type),
    )
    if existing:
        return False

    relation_id = f"r_{uuid.uuid4().hex[:12]}"
    await database.insert("concept_relations", {
        "id": relation_id,
        "source_id": source_id,
        "target_id": target_id,
        "relation_type": relation_type,
        "confidence": confidence,
        "extracted_by": extracted_by,
    })
    return True


async def _add_source(
    concept_id: str,
    textbook_id: str,
    chapter_id: str,
    chunk_ids: list,
    page_numbers: list,
    extracted_text: str,
) -> None:
    """Add a provenance record linking a concept to its textbook source."""
    # Avoid duplicate sources per chapter
    existing = await database.fetch_all(
        "concept_sources",
        "concept_id = ? AND textbook_id = ? AND chapter_id = ?",
        (concept_id, textbook_id, chapter_id),
    )
    if existing:
        return  # Already have this source

    source_id = f"cs_{uuid.uuid4().hex[:12]}"
    await database.insert("concept_sources", {
        "id": source_id,
        "concept_id": concept_id,
        "textbook_id": textbook_id,
        "chapter_id": chapter_id,
        "chunk_ids": chunk_ids,
        "page_numbers": page_numbers,
        "extracted_text": extracted_text[:500] if extracted_text else "",
        "extraction_method": "claude",
    })


# ── Relations-Only Extraction ─────────────────────────────────────

RELATIONS_EXTRACTION_SYSTEM = """You are a senior medical professor identifying relationships between medical concepts from an MBBS textbook chapter.

You are given a list of medical concepts that were extracted from a chapter. Your task: identify ALL relationships between these concepts based on the chapter content.

RELATION TYPES (use exactly):
- is_subtype_of: hierarchical (child → parent). E.g., "Colles Fracture" → "Fracture"
- causes: agent causes disease/condition. E.g., "Staphylococcus" → "Osteomyelitis"
- presents_with: disease presents with symptom/sign. E.g., "Fracture" → "Pain"
- treated_by: disease/condition → treatment/procedure/drug. E.g., "Fracture" → "Open Reduction"
- investigated_by: condition → diagnostic test. E.g., "Fracture" → "X-ray"
- complication_of: complication → primary disease/procedure. E.g., "Malunion" → "Fracture"
- associated_with: bidirectional association. E.g., "Diabetes" → "Osteoporosis"
- part_of: anatomy → larger structure. E.g., "Femoral Head" → "Hip Joint"
- differential_of: differential diagnosis. E.g., "Tuberculosis" → "Osteomyelitis"
- risk_factor_for: risk factor → disease. E.g., "Osteoporosis" → "Pathological Fracture"
- precedes: temporal/causal sequence. E.g., "Inflammation" → "Fibrosis"
- managed_by: general management. E.g., "Pain" → "NSAIDs"
- produces: physiological production. E.g., "Beta Cells" → "Insulin"
- metabolized_by: metabolic clearance. E.g., "Bilirubin" → "Liver"
- innervated_by: nervous supply. E.g., "Deltoid" → "Axillary Nerve"
- vascularized_by: blood supply. E.g., "Heart" → "Coronary Arteries"
- contraindicated_in: warning/danger. E.g., "Aspirin" → "Peptic Ulcer"
- indicated_in: clinical indication. E.g., "Antibiotics" → "Infection"
- side_effect_of: adverse effect. E.g., "Cough" → "ACE Inhibitors"

RULES:
1. ONLY use concept names from the provided list (exact match)
2. Each relation must reference concepts that appear in the provided list
3. Be thorough — identify ALL valid relationships
4. Assign confidence: 0.95 for clearly stated relations, 0.80 for implied, 0.65 for inferred
5. Each concept should have at least 1 relation if possible

Respond with ONLY valid JSON — no markdown, no explanation:
{
  "relations": [
    {
      "source": "Exact Concept Name",
      "target": "Exact Concept Name",
      "relation_type": "causes",
      "confidence": 0.95
    }
  ]
}"""


async def extract_relations_for_textbook(textbook_id: str) -> dict:
    """Submit a batch to extract relations between already-extracted concepts.

    For each chapter, gathers existing concepts (from concept_sources) and asks
    Claude to identify relationships between them. Much cheaper/faster than full
    KG re-extraction since we only need the relations array.
    """
    textbook = await database.fetch_one("textbooks", textbook_id)
    if not textbook:
        raise ValueError(f"Textbook {textbook_id} not found")

    subject = textbook["subject"]
    textbook_name = textbook["name"]

    chapters = await database.fetch_all(
        "chapters", "textbook_id = ?", (textbook_id,), "chapter_number ASC"
    )
    if not chapters:
        return {"error": "No chapters found"}

    batch_requests = []

    for chapter in chapters:
        chapter_id = chapter["id"]

        # Get concepts for this chapter from concept_sources
        sources = await database.fetch_all(
            "concept_sources", "chapter_id = ?", (chapter_id,)
        )
        if not sources:
            continue

        concept_ids = list({s["concept_id"] for s in sources})
        concepts = []
        for cid in concept_ids:
            c = await database.fetch_one("concepts", cid)
            if c:
                concepts.append(c)

        if len(concepts) < 2:
            continue  # Need at least 2 concepts for relations

        # Build concept list for the prompt
        concept_lines = []
        for c in concepts:
            concept_lines.append(f"- {c['name']} [{c['concept_type']}]")

        concept_list = "\n".join(concept_lines)

        batch_requests.append({
            "custom_id": f"rel_{chapter_id}",
            "max_tokens": 4096,  # Relations-only output is compact
            "system": RELATIONS_EXTRACTION_SYSTEM,
            "messages": [{
                "role": "user",
                "content": (
                    f"Identify ALL medical relationships between these concepts "
                    f"from the chapter.\n\n"
                    f"Textbook: {textbook_name}\n"
                    f"Chapter {chapter.get('chapter_number', '?')}: {chapter['name']}\n"
                    f"Subject: {subject}\n\n"
                    f"CONCEPTS ({len(concepts)}):\n{concept_list}\n\n"
                    f"Find ALL relationships between the above concepts."
                ),
            }],
        })

    if not batch_requests:
        return {"status": "completed", "message": "No chapters with concepts found."}

    # Submit batch
    client = get_claude_client()
    try:
        batch_id = await client.request_batch(
            requests=batch_requests,
            model=settings.haiku_model,
            task_type="kg_relations",
            subject=subject,
        )
    except Exception as e:
        raise RuntimeError(f"Relations batch submission failed: {e}") from e

    # Store batch ID in checkpoint
    checkpoint = Checkpoint("extract_relations", textbook_id)
    checkpoint.set_meta("relations_batch_id", batch_id)
    checkpoint.set_meta("textbook_id", textbook_id)

    await database.update("textbooks", textbook_id, {"kg_status": "relations_pending"})

    logger.info(
        f"Relations batch submitted: {batch_id} ({len(batch_requests)} chapters) "
        f"for textbook {textbook_id}"
    )

    return {
        "status": "batch_pending",
        "batch_id": batch_id,
        "chapters_queued": len(batch_requests),
        "message": f"Relations extraction batch submitted ({len(batch_requests)} chapters).",
    }


async def process_relations_batch_results(textbook_id: str, batch_id: str) -> dict:
    """Process Claude batch results for relations-only extraction."""
    textbook = await database.fetch_one("textbooks", textbook_id)
    if not textbook:
        raise ValueError(f"Textbook {textbook_id} not found")

    subject = textbook["subject"]

    client = get_claude_client()
    batch_result = await client.poll_batch(batch_id)

    if batch_result["status"] != "ended":
        return {
            "status": batch_result["status"],
            "message": f"Batch still processing: {batch_result['status']}",
            "request_counts": batch_result.get("request_counts", {}),
        }

    results = batch_result.get("results", {})
    relations_new = 0
    chapters_ok = 0
    chapters_err = 0

    for custom_id, result in results.items():
        if result["status"] != "success":
            chapters_err += 1
            logger.error(f"Relations extraction failed for {custom_id}: {result.get('error')}")
            continue

        data = result["data"]
        relations_raw = data.get("relations", [])

        if not relations_raw:
            continue

        # Extract chapter_id from custom_id (format: "rel_8b6f6d5e_ch01")
        chapter_id = custom_id.replace("rel_", "", 1)

        # Build name→id map for this chapter's concepts
        sources = await database.fetch_all(
            "concept_sources", "chapter_id = ?", (chapter_id,)
        )
        concept_ids = list({s["concept_id"] for s in sources})
        name_to_id = {}
        for cid in concept_ids:
            c = await database.fetch_one("concepts", cid)
            if c:
                name_to_id[_normalize(c["name"])] = c["id"]
                # Also map aliases
                aliases = c.get("aliases", [])
                if isinstance(aliases, str):
                    try:
                        aliases = json.loads(aliases)
                    except (json.JSONDecodeError, TypeError):
                        aliases = []
                for alias in aliases:
                    name_to_id[_normalize(alias)] = c["id"]

        # Store relations
        for rel_data in relations_raw:
            source_name = _normalize(rel_data.get("source", ""))
            target_name = _normalize(rel_data.get("target", ""))
            rel_type = rel_data.get("relation_type", "")
            confidence = float(rel_data.get("confidence", 0.8))

            source_id = name_to_id.get(source_name)
            target_id = name_to_id.get(target_name)

            if source_id and target_id and source_id != target_id:
                added = await _upsert_relation(source_id, target_id, rel_type, confidence)
                if added:
                    relations_new += 1

        chapters_ok += 1

    # Update status
    status = "completed" if chapters_err == 0 else "completed_with_errors"
    await database.update("textbooks", textbook_id, {"kg_status": status})

    msg = (
        f"Relations extracted: {relations_new} new relations "
        f"from {chapters_ok} chapters ({chapters_err} errors)."
    )
    logger.info(f"Relations batch processed for {textbook_id}: {msg}")

    return {
        "status": status,
        "relations_new": relations_new,
        "chapters_ok": chapters_ok,
        "chapters_err": chapters_err,
        "message": msg,
    }


# ── Batch Request Builder (used by both ingestion pipeline and standalone) ──

async def _build_kg_batch_requests(
    textbook_id: str,
    textbook_name: str,
    subject: str,
    chapter_ids: list[str],
) -> list[dict]:
    """Build Claude batch requests for KG concept extraction.

    Called by:
    - builder.py (integrated pipeline — automatic after KB batch)
    - build_knowledge_graph() (standalone build from KG page)
    """
    batch_requests = []

    for chapter_id in chapter_ids:
        chapter = await database.fetch_one("chapters", chapter_id)
        if not chapter:
            continue

        chunks = await database.fetch_all(
            "chunks", "chapter_id = ?", (chapter_id,), "chunk_index ASC"
        )
        if not chunks:
            continue

        # Build chapter text — combine topics/key_terms + chunk text
        chapter_text_parts = []

        for field in ["summary", "topics", "key_terms"]:
            val = chapter.get(field)
            if val:
                if isinstance(val, str) and val.startswith("["):
                    try:
                        items = json.loads(val)
                        chapter_text_parts.append(f"{field.upper()}: {', '.join(str(i) for i in items)}")
                    except json.JSONDecodeError:
                        chapter_text_parts.append(f"{field.upper()}: {val}")
                elif val:
                    chapter_text_parts.append(f"{field.upper()}: {val}")

        chunk_texts = [c["text"] for c in chunks]
        chapter_content = "\n\n".join(chunk_texts)
        max_chars = 40000
        if len(chapter_content) > max_chars:
            chapter_content = chapter_content[:max_chars] + "\n\n[Text truncated]"
        chapter_text_parts.append(f"\nCHAPTER TEXT:\n{chapter_content}")

        full_text = "\n".join(chapter_text_parts)

        batch_requests.append({
            "custom_id": chapter_id,
            "max_tokens": 8192,
            "system": CONCEPT_EXTRACTION_SYSTEM,
            "messages": [{
                "role": "user",
                "content": (
                    f"Extract ALL medical concepts and their relationships from this chapter.\n"
                    f"Textbook: {textbook_name}\n"
                    f"Chapter {chapter.get('chapter_number', '?')}: {chapter['name']}\n"
                    f"Subject: {subject}\n\n"
                    f"{full_text}"
                ),
            }],
        })

    return batch_requests


# ── Main Build Function ───────────────────────────────────────────

async def build_knowledge_graph(
    textbook_id: str,

    progress_callback=None,
) -> dict:
    """Build the knowledge graph for a textbook.

    Processes each chapter via Claude batch, extracts concepts and relations,
    deduplicates against existing graph, stores with provenance.

    Returns summary dict with counts.
    """
    async def notify(step: str, current: int, total: int, msg: str):
        if progress_callback:
            await progress_callback(step, current, total, msg)
        logger.info(f"[kg:{step}] {msg} ({current}/{total})")

    textbook = await database.fetch_one("textbooks", textbook_id)
    if not textbook:
        raise ValueError(f"Textbook {textbook_id} not found")

    subject = textbook["subject"]
    textbook_name = textbook["name"]
    checkpoint = Checkpoint("build_kg", f"{textbook_id}")

    await database.update("textbooks", textbook_id, {"kg_status": "building"})

    chapters = await database.fetch_all(
        "chapters", "textbook_id = ?", (textbook_id,), "chapter_number ASC"
    )
    if not chapters:
        return {"error": "No chapters found for this textbook"}

    await notify("prepare", 0, len(chapters), f"Preparing KG extraction for {len(chapters)} chapters")

    # ── Build Claude batch requests ──
    batch_requests = []
    chapter_context = {}  # chapter_id → {chapter, chunks, page_numbers}

    for chapter in chapters:
        chapter_id = chapter["id"]

        if checkpoint.is_completed(f"kg_chapter_{chapter_id}"):
            await notify("prepare", chapters.index(chapter) + 1, len(chapters),
                         f"Skipping (cached): {chapter['name']}")
            continue

        chunks = await database.fetch_all(
            "chunks", "chapter_id = ?", (chapter_id,), "chunk_index ASC"
        )
        if not chunks:
            checkpoint.mark_completed(f"kg_chapter_{chapter_id}")
            continue

        # Gather page numbers
        all_pages = []
        for c in chunks:
            pages_raw = c.get("page_numbers", "[]")
            try:
                pages = json.loads(pages_raw) if isinstance(pages_raw, str) else pages_raw
                all_pages.extend([int(p) for p in pages if str(p).isdigit()])
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Build chapter text — combine topics/key_terms + chunk text for richer extraction
        chapter_text_parts = []

        # Include Claude-extracted metadata if available
        for field in ["summary", "topics", "key_terms"]:
            val = chapter.get(field)
            if val:
                if isinstance(val, str) and val.startswith("["):
                    try:
                        items = json.loads(val)
                        chapter_text_parts.append(f"{field.upper()}: {', '.join(str(i) for i in items)}")
                    except json.JSONDecodeError:
                        chapter_text_parts.append(f"{field.upper()}: {val}")
                elif val:
                    chapter_text_parts.append(f"{field.upper()}: {val}")

        # Add chunk text (truncated)
        chunk_texts = [c["text"] for c in chunks]
        chapter_content = "\n\n".join(chunk_texts)
        max_chars = 40000
        if len(chapter_content) > max_chars:
            chapter_content = chapter_content[:max_chars] + "\n\n[Text truncated]"
        chapter_text_parts.append(f"\nCHAPTER TEXT:\n{chapter_content}")

        full_text = "\n".join(chapter_text_parts)

        chapter_context[chapter_id] = {
            "chapter": chapter,
            "chunks": chunks,
            "chunk_ids": [c["id"] for c in chunks],
            "page_numbers": sorted(set(all_pages)),
        }

        batch_requests.append({
            "custom_id": chapter_id,
            "max_tokens": 8192,
            "system": CONCEPT_EXTRACTION_SYSTEM,
            "messages": [{
                "role": "user",
                "content": (
                    f"Extract ALL medical concepts and their relationships from this chapter.\n"
                    f"Textbook: {textbook_name}\n"
                    f"Chapter {chapter.get('chapter_number', '?')}: {chapter['name']}\n"
                    f"Subject: {subject}\n\n"
                    f"{full_text}"
                ),
            }],
        })

    if not batch_requests:
        await database.update("textbooks", textbook_id, {"kg_status": "completed"})
        return {
            "status": "completed",
            "message": "All chapters already processed (cached).",
            "concepts_new": 0,
            "relations_new": 0,
        }

    await notify("extract", 0, len(batch_requests),
                 f"Sending {len(batch_requests)} chapters to Claude for concept extraction...")

    # ── Submit batch ──
    client = get_claude_client()
    try:
        batch_id = await client.request_batch(
            requests=batch_requests,
            model=settings.haiku_model,
            task_type="kg_extraction",
            subject=subject,
        )
    except Exception as e:
        await database.update("textbooks", textbook_id, {"kg_status": "failed"})
        raise RuntimeError(f"Claude batch submission failed: {e}") from e

    checkpoint.set_meta("kg_batch_id", batch_id)
    await database.update("textbooks", textbook_id, {"kg_status": "kg_batch_pending"})

    await notify("extract", len(batch_requests), len(batch_requests),
                 f"Batch submitted: {batch_id}. Use 'Process Batch' when ready.")

    return {
        "status": "batch_pending",
        "batch_id": batch_id,
        "chapters_queued": len(batch_requests),
        "message": (
            f"Knowledge graph extraction batch submitted ({len(batch_requests)} chapters). "
            f"Click 'Process KG Batch' to retrieve results when ready."
        ),
    }


async def process_kg_batch_results(textbook_id: str, batch_id: str) -> dict:
    """Process Claude batch results and store concepts/relations in the graph."""
    textbook = await database.fetch_one("textbooks", textbook_id)
    if not textbook:
        raise ValueError(f"Textbook {textbook_id} not found")

    subject = textbook["subject"]
    checkpoint = Checkpoint("build_kg", f"{textbook_id}")

    client = get_claude_client()
    batch_result = await client.poll_batch(batch_id)

    if batch_result["status"] != "ended":
        return {
            "status": batch_result["status"],
            "message": f"Batch still processing. Status: {batch_result['status']}",
            "request_counts": batch_result.get("request_counts", {}),
        }

    results = batch_result.get("results", {})
    concepts_new = 0
    concepts_merged = 0
    relations_new = 0
    chapters_ok = 0
    chapters_err = 0

    for chapter_id, result in results.items():
        if checkpoint.is_completed(f"kg_chapter_{chapter_id}"):
            continue

        if result["status"] != "success":
            chapters_err += 1
            logger.error(f"KG extraction failed for {chapter_id}: {result.get('error')}")
            continue

        data = result["data"]
        concepts_raw = data.get("concepts", [])
        relations_raw = data.get("relations", [])

        if not concepts_raw:
            checkpoint.mark_completed(f"kg_chapter_{chapter_id}")
            continue

        # Load chapter + chunks for provenance
        chapter = await database.fetch_one("chapters", chapter_id)
        if not chapter:
            continue

        chunks = await database.fetch_all("chunks", "chapter_id = ?", (chapter_id,))
        chunk_ids = [c["id"] for c in chunks]

        all_pages = []
        for c in chunks:
            pages_raw = c.get("page_numbers", "[]")
            try:
                pages = json.loads(pages_raw) if isinstance(pages_raw, str) else pages_raw
                all_pages.extend([int(p) for p in pages if str(p).isdigit()])
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        page_numbers = sorted(set(all_pages))

        # Build name → concept_id map for this chapter (for relation linking)
        name_to_id = {}

        # ── Store concepts ──
        for concept_data in concepts_raw:
            name = concept_data.get("name", "").strip()
            if not name or len(name) < 2:
                continue

            concept_id, is_new = await _find_or_create_concept(
                name=name,
                concept_type=concept_data.get("concept_type", "other"),
                definition=concept_data.get("definition", ""),
                aliases=concept_data.get("aliases", []),
                importance=concept_data.get("importance", "standard"),
                subject=subject,
            )

            name_to_id[_normalize(name)] = concept_id
            for alias in concept_data.get("aliases", []):
                name_to_id[_normalize(alias)] = concept_id

            if is_new:
                concepts_new += 1
            else:
                concepts_merged += 1

            # Add provenance source
            # Use first chunk text as snippet
            snippet = chunks[0]["text"][:300] if chunks else ""
            await _add_source(
                concept_id=concept_id,
                textbook_id=textbook_id,
                chapter_id=chapter_id,
                chunk_ids=chunk_ids,
                page_numbers=page_numbers,
                extracted_text=snippet,
            )

        # ── Store relations ──
        for rel_data in relations_raw:
            source_name = _normalize(rel_data.get("source", ""))
            target_name = _normalize(rel_data.get("target", ""))
            rel_type = rel_data.get("relation_type", "")
            confidence = float(rel_data.get("confidence", 0.8))

            source_id = name_to_id.get(source_name)
            target_id = name_to_id.get(target_name)

            if not source_id or not target_id:
                # Try DB lookup for concepts from previous chapters
                if not source_id:
                    existing = await database.fetch_all(
                        "concepts", "canonical_name = ? AND subject = ?", (source_name, subject)
                    )
                    if existing:
                        source_id = existing[0]["id"]

                if not target_id:
                    existing = await database.fetch_all(
                        "concepts", "canonical_name = ? AND subject = ?", (target_name, subject)
                    )
                    if existing:
                        target_id = existing[0]["id"]

            if source_id and target_id:
                added = await _upsert_relation(source_id, target_id, rel_type, confidence)
                if added:
                    relations_new += 1

        checkpoint.mark_completed(f"kg_chapter_{chapter_id}")
        chapters_ok += 1

    # Update textbook KG status
    status = "completed" if chapters_err == 0 else "completed_with_errors"
    await database.update("textbooks", textbook_id, {
        "kg_status": status,
    })

    return {
        "status": status,
        "concepts_new": concepts_new,
        "concepts_merged": concepts_merged,
        "relations_new": relations_new,
        "chapters_processed": chapters_ok,
        "chapters_failed": chapters_err,
        "message": (
            f"Knowledge graph built: {concepts_new} new concepts, "
            f"{concepts_merged} merged, {relations_new} new relations, "
            f"{chapters_ok} chapters processed."
        ),
    }


# ── Query Helpers ─────────────────────────────────────────────────

async def get_concept_with_relations(concept_id: str) -> dict:
    """Fetch a concept with all its relations (outgoing and incoming)."""
    concept = await database.fetch_one("concepts", concept_id)
    if not concept:
        return {}

    # Outgoing relations
    outgoing_raw = await database.fetch_all(
        "concept_relations", "source_id = ?", (concept_id,)
    )
    # Incoming relations
    incoming_raw = await database.fetch_all(
        "concept_relations", "target_id = ?", (concept_id,)
    )

    async def enrich_relation(rel: dict, direction: str) -> dict:
        other_id = rel["target_id"] if direction == "outgoing" else rel["source_id"]
        other = await database.fetch_one("concepts", other_id)
        return {
            **rel,
            "direction": direction,
            "other_concept": {
                "id": other_id,
                "name": other["name"] if other else "Unknown",
                "concept_type": other.get("concept_type", "other") if other else "other",
            },
        }

    outgoing = [await enrich_relation(r, "outgoing") for r in outgoing_raw]
    incoming = [await enrich_relation(r, "incoming") for r in incoming_raw]

    # Sources
    sources_raw = await database.fetch_all(
        "concept_sources", "concept_id = ?", (concept_id,)
    )
    sources = []
    for s in sources_raw:
        tb = await database.fetch_one("textbooks", s["textbook_id"])
        ch = await database.fetch_one("chapters", s.get("chapter_id", "")) if s.get("chapter_id") else None
        sources.append({
            **s,
            "textbook_name": tb["name"] if tb else "Unknown",
            "chapter_name": ch["name"] if ch else "Unknown",
        })

    aliases = concept.get("aliases", [])
    if isinstance(aliases, str):
        try:
            aliases = json.loads(aliases)
        except json.JSONDecodeError:
            aliases = []

    return {
        **concept,
        "aliases": aliases,
        "outgoing_relations": outgoing,
        "incoming_relations": incoming,
        "sources": sources,
    }


async def get_graph_stats(subject: str = None) -> dict:
    """Get statistics about the knowledge graph."""
    where = "subject = ?" if subject else ""
    params = (subject,) if subject else ()

    total_concepts = await database.count("concepts", where, params)

    type_counts = {}
    for ctype in VALID_CONCEPT_TYPES:
        type_where = f"concept_type = ?{' AND subject = ?' if subject else ''}"
        type_params = (ctype, subject) if subject else (ctype,)
        count = await database.count("concepts", type_where, type_params)
        if count > 0:
            type_counts[ctype] = count

    total_relations = await database.count("concept_relations")
    total_sources = await database.count("concept_sources")

    importance_counts = {}
    for imp in ["must_know", "standard", "advanced"]:
        imp_where = f"importance = ?{' AND subject = ?' if subject else ''}"
        imp_params = (imp, subject) if subject else (imp,)
        count = await database.count("concepts", imp_where, imp_params)
        importance_counts[imp] = count

    return {
        "total_concepts": total_concepts,
        "total_relations": total_relations,
        "total_sources": total_sources,
        "by_type": type_counts,
        "by_importance": importance_counts,
    }


async def search_concepts(
    query: str,
    subject: str = None,
    concept_type: str = None,
    importance: str = None,
    limit: int = 50,
) -> list[dict]:
    """Search concepts by name, with optional filters."""
    canonical_query = _normalize(query)

    conditions = []
    params = []

    if query:
        conditions.append("(canonical_name LIKE ? OR name LIKE ? OR aliases LIKE ?)")
        like = f"%{canonical_query}%"
        params.extend([like, f"%{query}%", f"%{query}%"])

    if subject:
        conditions.append("subject = ?")
        params.append(subject)

    if concept_type:
        conditions.append("concept_type = ?")
        params.append(concept_type)

    if importance:
        conditions.append("importance = ?")
        params.append(importance)

    where = " AND ".join(conditions) if conditions else ""
    results = await database.fetch_all(
        "concepts", where, tuple(params), "frequency DESC, importance DESC"
    )

    # Parse JSON aliases and attach source count
    output = []
    for c in results[:limit]:
        aliases = c.get("aliases", [])
        if isinstance(aliases, str):
            try:
                aliases = json.loads(aliases)
            except json.JSONDecodeError:
                aliases = []

        source_count = await database.count("concept_sources", "concept_id = ?", (c["id"],))
        relation_count = await database.count(
            "concept_relations",
            "source_id = ? OR target_id = ?",
            (c["id"], c["id"]),
        )

        output.append({
            **c,
            "aliases": aliases,
            "source_count": source_count,
            "relation_count": relation_count,
        })

    return output


async def get_graph_for_subject(subject: str = None, concept_type: str = None, limit: int = 200) -> dict:
    """Return graph data (nodes + edges) for visualization."""
    clauses = []
    params = []
    if subject:
        clauses.append("subject = ?")
        params.append(subject)
    if concept_type:
        clauses.append("concept_type = ?")
        params.append(concept_type)

    where = " AND ".join(clauses) if clauses else ""
    params_tuple = tuple(params)

    concepts_all = await database.fetch_all("concepts", where, params_tuple, "frequency DESC")
    total_db_nodes = len(concepts_all)
    concepts = concepts_all[:limit]
    concept_ids = {c["id"] for c in concepts}

    # Get relations between these concepts
    relations = []
    for c in concepts:
        rels = await database.fetch_all(
            "concept_relations",
            "source_id = ?",
            (c["id"],),
        )
        for r in rels:
            if r["target_id"] in concept_ids:
                relations.append(r)

    nodes = []
    for c in concepts:
        aliases = c.get("aliases", [])
        if isinstance(aliases, str):
            try:
                aliases = json.loads(aliases)
            except json.JSONDecodeError:
                aliases = []
        nodes.append({
            "id": c["id"],
            "name": c["name"],
            "concept_type": c["concept_type"],
            "importance": c["importance"],
            "frequency": c["frequency"],
            "aliases": aliases[:3],  # Only first 3 for viz
        })

    edges = [{
        "id": r["id"],
        "source": r["source_id"],
        "target": r["target_id"],
        "relation_type": r["relation_type"],
        "confidence": r["confidence"],
    } for r in relations]

    return {
        "nodes": nodes,
        "edges": edges,
        "subject": subject,
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "total_db_nodes": total_db_nodes,
    }


async def get_concepts_for_question(question_text: str, subject: str, limit: int = 10) -> list[dict]:
    """Find concepts most relevant to a question (for GraphRAG)."""
    # Extract key words from question (filter stop words)
    STOP = {
        "what", "how", "why", "when", "where", "which", "who", "is", "are",
        "was", "were", "the", "a", "an", "and", "or", "but", "in", "on",
        "at", "to", "for", "of", "with", "as", "by", "from", "that", "this",
        "it", "its", "be", "been", "being", "have", "has", "had", "do", "does",
        "did", "will", "would", "could", "should", "may", "might", "shall",
        "can", "not", "no", "nor", "so", "yet", "both", "each", "every",
        "all", "any", "more", "most", "other", "some", "than", "too", "very",
        "just", "about", "up", "out", "briefly", "discuss", "describe", "explain",
        "write", "note", "enumerate", "mention", "classify", "define",
        "enumerate", "clinical", "significance", "importance", "short",
        "answer", "detail", "long",
    }

    words = re.findall(r'[a-zA-Z]{3,}', question_text.lower())
    keywords = [w for w in words if w not in STOP]

    if not keywords:
        return []

    # Search for each keyword
    found_ids = set()
    found_concepts = []

    for keyword in keywords[:8]:  # Limit keyword iterations
        results = await search_concepts(keyword, subject=subject, limit=5)
        for c in results:
            if c["id"] not in found_ids:
                found_ids.add(c["id"])
                found_concepts.append(c)

    # Sort by relevance (frequency × importance weight)
    importance_weight = {"must_know": 3, "standard": 2, "advanced": 1}
    found_concepts.sort(
        key=lambda c: (c.get("frequency", 1) * importance_weight.get(c.get("importance", "standard"), 2)),
        reverse=True,
    )

    return found_concepts[:limit]


async def get_related_chunk_ids(concept_ids: list[str], subject: str) -> list[str]:
    """Get all chunk IDs from concept sources — for GraphRAG retrieval."""
    all_chunk_ids = []
    seen = set()

    for concept_id in concept_ids:
        sources = await database.fetch_all(
            "concept_sources", "concept_id = ?", (concept_id,)
        )
        for s in sources:
            raw = s.get("chunk_ids", "[]")
            try:
                chunks = json.loads(raw) if isinstance(raw, str) else raw
                for cid in chunks:
                    if cid not in seen:
                        seen.add(cid)
                        all_chunk_ids.append(cid)
            except (json.JSONDecodeError, TypeError):
                pass

    return all_chunk_ids


async def get_extended_concepts_for_question(
    question_text: str, subject: str, limit: int = 10
) -> dict:
    """Get concepts + their 1-hop neighbors with relation context for GraphRAG.

    Returns:
        {
            "concepts": [...],           # All matched + neighbor concepts
            "relation_context": [...],   # Human-readable relation chains
            "direct_count": int,
            "neighbor_count": int,
        }
    """
    direct_concepts = await get_concepts_for_question(question_text, subject, limit=8)

    neighbor_concepts = []
    relation_context = []
    seen = {c["id"] for c in direct_concepts}

    for concept in direct_concepts:
        # Outgoing relations
        out_rels = await database.fetch_all(
            "concept_relations", "source_id = ?", (concept["id"],)
        )
        for rel in out_rels[:6]:  # Cap per-concept to avoid explosion
            if rel["target_id"] not in seen:
                target = await database.fetch_one("concepts", rel["target_id"])
                if target and target.get("subject") == subject:
                    neighbor_concepts.append(target)
                    seen.add(rel["target_id"])
                    relation_context.append(
                        f"{concept['name']} --[{rel['relation_type']}]--> {target['name']}"
                    )

        # Incoming relations
        in_rels = await database.fetch_all(
            "concept_relations", "target_id = ?", (concept["id"],)
        )
        for rel in in_rels[:6]:
            if rel["source_id"] not in seen:
                source = await database.fetch_one("concepts", rel["source_id"])
                if source and source.get("subject") == subject:
                    neighbor_concepts.append(source)
                    seen.add(rel["source_id"])
                    relation_context.append(
                        f"{source['name']} --[{rel['relation_type']}]--> {concept['name']}"
                    )

    all_concepts = direct_concepts + neighbor_concepts[:limit]
    all_concept_ids = [c["id"] for c in all_concepts]

    return {
        "concepts": all_concepts,
        "concept_ids": all_concept_ids,
        "concept_names": [c["name"] for c in all_concepts],
        "relation_context": relation_context,
        "direct_count": len(direct_concepts),
        "neighbor_count": min(len(neighbor_concepts), limit),
    }
