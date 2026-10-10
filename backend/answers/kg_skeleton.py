"""Knowledge-graph skeleton block builder using ``graph_builder.py`` (R6, R7).

Builds the ``KG_Skeleton_Block`` prompt segment described in design.md
§A.2 from the matched concepts and their 1-hop neighborhood:

    Concepts:
      - <name> (<concept_type>) [<importance>]
      ...

    Relations:
      - <source_name> --[<relation_type>]--> <target_name>
      ...

    MUST APPEAR IN ANSWER IF RELEVANT:
      - <must_know_name>
      ...

When ``get_extended_concepts_for_question`` returns zero concepts the
returned :class:`KGSkeleton` carries ``used=False`` and empty fields per
R6.5; when the underlying graph_builder / DB call raises, this module
re-raises :class:`KGUnavailable` so the orchestrator can fall back to
vector-only retrieval.
"""
from __future__ import annotations

import logging

from answers.errors import KGUnavailable
from answers.schema import KGSkeleton
from knowledge.graph_builder import (
    get_extended_concepts_for_question,
    get_related_chunk_ids,  # noqa: F401  (re-exported for retrieval.py task 25)
)
from state import db as database

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Relation priority — higher-priority relation types surface first in the
# skeleton block so the draft model sees the medically most useful edges
# even after the per-question 30-relation cap is applied.
# ──────────────────────────────────────────────────────────────────────────

#: Smaller value = higher priority. Order documented in design.md
#: §Components and Interfaces (R6).
_RELATION_PRIORITY: dict[str, int] = {
    "causes": 0,
    "presents_with": 1,
    "treated_by": 2,
    "investigated_by": 3,
    "complication_of": 4,
    "associated_with": 5,
    "part_of": 6,
    "differential_of": 7,
    "risk_factor_for": 8,
}
_DEFAULT_PRIORITY = 99
_MAX_RELATIONS = 30


def _relation_sort_key(rel: dict) -> tuple[int, float]:
    """``(priority_rank, -confidence)`` so higher priority + higher confidence sort first."""
    rtype = rel.get("relation_type", "") or ""
    priority = _RELATION_PRIORITY.get(rtype, _DEFAULT_PRIORITY)
    try:
        conf = float(rel.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return (priority, -conf)


async def build_kg_skeleton(question_text: str, subject: str) -> KGSkeleton:
    """Build the ``KG_Skeleton_Block`` prompt segment for a question (R6, R7).

    Calls :func:`knowledge.graph_builder.get_extended_concepts_for_question`
    for concept + 1-hop neighbor expansion, then queries
    ``concept_relations`` directly for the actual relation rows so we can
    sort by ``(priority, confidence)`` and capture relation IDs for
    provenance.

    Args:
        question_text: The raw question stem.
        subject: Subject filter (e.g. ``"general_surgery"``).

    Returns:
        A :class:`KGSkeleton`. ``used`` is ``False`` with all-empty fields
        when no concepts match (R6.5); otherwise populated with the
        formatted skeleton block.

    Raises:
        KGUnavailable: When the graph_builder lookup or the
            ``concept_relations`` fetch raises. The ``mapping_id`` is unknown
            at this layer; pass empty string and the caller (orchestrator)
            will recover.
    """
    # 1. Extended concept expansion (graph_builder).
    try:
        ext = await get_extended_concepts_for_question(
            question_text, subject, limit=10
        )
    except Exception as exc:  # pragma: no cover — exercised by test 4
        logger.warning(
            "kg_skeleton: get_extended_concepts_for_question failed: %s", exc
        )
        raise KGUnavailable(
            mapping_id="", detail=f"get_extended_concepts_for_question: {exc}"
        ) from exc

    concepts: list[dict] = ext.get("concepts", []) or []
    concept_ids: list[str] = ext.get("concept_ids", []) or []

    # 2. Empty-match fast path (R6.5).
    if not concept_ids:
        return KGSkeleton(
            concept_ids=[],
            relation_ids=[],
            must_know_concept_ids=[],
            skeleton_block_text="",
            used=False,
        )

    concept_by_id: dict[str, dict] = {c["id"]: c for c in concepts if c.get("id")}

    # 3. Must-know subset (R7.1).
    must_know_ids: list[str] = [
        c["id"] for c in concepts if c.get("importance") == "must_know" and c.get("id")
    ]

    # 4. Fetch relation rows for the matched concept set so we can sort by
    #    (priority, confidence) and capture relation IDs. Build the IN-clause
    #    placeholders dynamically; aiosqlite needs one ``?`` per parameter.
    placeholders = ",".join("?" for _ in concept_ids)
    where = (
        f"source_id IN ({placeholders}) OR target_id IN ({placeholders})"
    )
    params = tuple(concept_ids) + tuple(concept_ids)

    try:
        relations: list[dict] = await database.fetch_all(
            "concept_relations", where, params
        )
    except Exception as exc:
        logger.warning("kg_skeleton: concept_relations fetch failed: %s", exc)
        raise KGUnavailable(
            mapping_id="", detail=f"concept_relations fetch: {exc}"
        ) from exc

    # 5. Keep only relations whose BOTH endpoints we have names for, so the
    #    formatted block is fully readable. Out-of-set neighbors would print
    #    as bare IDs and add no value to the prompt.
    id_set = set(concept_ids)
    in_set_relations = [
        r
        for r in relations
        if r.get("source_id") in id_set and r.get("target_id") in id_set
    ]

    # 6. Sort by priority then confidence; cap at 30.
    in_set_relations.sort(key=_relation_sort_key)
    in_set_relations = in_set_relations[:_MAX_RELATIONS]

    relation_ids = [r["id"] for r in in_set_relations if r.get("id")]

    # 7. Build the skeleton block text.
    lines: list[str] = ["Concepts:"]
    for c in concepts:
        name = c.get("name") or c.get("id") or "<unknown>"
        ctype = c.get("concept_type") or "concept"
        importance = c.get("importance") or "standard"
        lines.append(f"  - {name} ({ctype}) [{importance}]")

    lines.append("")
    lines.append("Relations:")
    if in_set_relations:
        for r in in_set_relations:
            src = concept_by_id.get(r["source_id"])
            tgt = concept_by_id.get(r["target_id"])
            if not src or not tgt:
                continue
            lines.append(
                f"  - {src.get('name', src['id'])} "
                f"--[{r.get('relation_type', '')}]--> "
                f"{tgt.get('name', tgt['id'])}"
            )
    else:
        lines.append("  (no relations recorded)")

    if must_know_ids:
        lines.append("")
        lines.append("MUST APPEAR IN ANSWER IF RELEVANT:")
        for cid in must_know_ids:
            c = concept_by_id.get(cid)
            if c is not None:
                lines.append(f"  - {c.get('name', cid)}")

    skeleton_text = "\n".join(lines)

    return KGSkeleton(
        concept_ids=concept_ids,
        relation_ids=relation_ids,
        must_know_concept_ids=must_know_ids,
        skeleton_block_text=skeleton_text,
        used=True,
    )
