"""Typed exception hierarchy for the answer pipeline (R1.9, R5.7, R6.5, R8.5, R9.6).

This module defines the routing layer documented in design.md §Error Handling.
The orchestrator (`pipeline.py`) catches subclasses of :class:`AnswerPipelineError`
at the per-mapping boundary so that a single failure never aborts the whole batch,
and so structured logs always carry the offending ``mapping_id``.

Hierarchy::

    AnswerPipelineError                 (base — never raised directly)
    ├── SchemaViolation                 R1.9   — model returned an unknown section key
    ├── MandatoryDataMissing            R5.7   — validator failed after the allowed re-prompt
    ├── CriticFailure                   R8.5   — critic returned ``fail`` after all retries
    ├── CostBlocked                     R9.6   — cost gate blocked Sonnet escalation
    └── KGUnavailable                   R6.5   — graph_builder lookup failed; vector-only fallback

Every concrete subclass takes the same two constructor arguments — ``mapping_id``
and ``detail`` — and stores them as attributes so the logging layer can emit them
through ``logger.warning(..., extra={"mapping_id": exc.mapping_id})`` without
having to re-parse the formatted message.
"""

from __future__ import annotations

__all__ = [
    "AnswerPipelineError",
    "SchemaViolation",
    "MandatoryDataMissing",
    "CriticFailure",
    "CostBlocked",
    "KGUnavailable",
]


class AnswerPipelineError(Exception):
    """Base class for every error raised inside ``backend/answers/``.

    Concrete subclasses are caught at the ``generate_answers`` boundary and
    recorded in ``GenerateBatchResult.errors`` (see design.md §Error Handling).
    Both ``mapping_id`` and ``detail`` are stored as attributes so structured
    log records can carry them in their ``extra`` dict.
    """

    def __init__(self, mapping_id: str, detail: str) -> None:
        self.mapping_id = mapping_id
        self.detail = detail
        # Compose the message in the format `[mapping_id] detail` so default
        # ``str(exc)`` output is already log-friendly without losing structure.
        super().__init__(f"[{mapping_id}] {detail}")


class SchemaViolation(AnswerPipelineError):
    """The draft model emitted a key outside the R1 ``AnswerSections`` schema.

    Per R1.9 the unknown key is dropped and a warning is logged. The pipeline
    raises this exception only when the violation makes the draft unusable
    (e.g. ``pydantic.ValidationError`` after the JSON-repair retry).
    """


class MandatoryDataMissing(AnswerPipelineError):
    """A mandatory-data validator failed even after the allowed re-prompt.

    Per R5.7 the pipeline still persists the draft with
    ``mandatory_data_complete=False``; this exception is raised by the
    validator layer so the orchestrator can attach the violation list to
    ``Provenance.mandatory_data_violations`` before persisting.
    """


class CriticFailure(AnswerPipelineError):
    """Examiner critic returned ``verdict="fail"`` after every allowed retry.

    Per R8.5 the pipeline caps regeneration at one Haiku retry plus one
    optional Sonnet escalation. When all retries are exhausted the orchestrator
    raises this so the failure surfaces in ``GenerateBatchResult.errors``
    while the latest draft is still persisted with a red ``critic_verdict``.
    """


class CostBlocked(AnswerPipelineError):
    """Cost gate blocked a Sonnet escalation (R9.6 — graceful fallback).

    This is non-fatal: the orchestrator catches it, persists the latest Haiku
    draft, sets ``provenance.cost_ceiling_blocked=True``, and emits a
    ``cost_blocked`` SSE event. It exists as an exception (rather than a
    return value) so the cost-gate boundary can short-circuit deeper Claude
    calls without threading a sentinel through every helper.
    """


class KGUnavailable(AnswerPipelineError):
    """``graph_builder`` lookup failed or returned zero usable concepts.

    Per R6.5 the pipeline falls back to vector-only retrieval, sets
    ``kg_skeleton_used=False`` on the answer record, and continues.
    """
