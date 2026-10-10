"""Pure cost-ceiling decision function for Sonnet escalation gating (R9.5, R9.6).

This module implements the gate documented in design.md §Cost Ceiling State Machine.
It is intentionally a single pure function with **no I/O, no logging, and no
module-level state** so the orchestrator can call it before every Claude
escalation and so it can be exercised by Hypothesis (see Property 5 — Cost gate
monotonicity).

Stage semantics
---------------

* ``draft``           — always allowed (Haiku draft cost is part of the
  base-rate spend, never blocked).
* ``critic``          — always allowed (Haiku critic cost likewise base-rate).
* ``regen_haiku``     — always allowed (one Haiku regeneration per R8.4 and
  R9.3; cost stays in the Haiku tier).
* ``escalation_sonnet`` — gated. Blocked iff the projected cost would push
  either the batch spend over ``batch_ceiling`` or the daily spend over
  ``daily_ceiling``.

Unknown stage strings fall through to ``"allow"`` defensively: a typo in the
caller must never silently block generation. Misspellings are caught upstream
by tests, not by this gate.

Property 5 — monotonicity
-------------------------

For any stage and any fixed ``projected_cost``, ``batch_ceiling``, and
``daily_ceiling``: if ``check`` blocks at ``spend_a`` it must also block at
every ``spend_b > spend_a``. The implementation satisfies this trivially
because the only blocking branch uses strict ``>`` against monotonically
increasing sums, so adding more spend cannot turn a block back into an allow.
"""

from __future__ import annotations

from typing import Literal

__all__ = ["check"]


# Stages that bypass the gate entirely. Defined as a frozenset so membership
# checks are O(1) and the constant is hashable / immutable for safe reuse.
_ALWAYS_ALLOWED_STAGES: frozenset[str] = frozenset({"draft", "critic", "regen_haiku"})

# The single stage subject to ceiling enforcement.
_GATED_STAGE: str = "escalation_sonnet"


def check(
    stage: str,
    projected_cost: float,
    batch_spend: float,
    daily_spend: float,
    batch_ceiling: float,
    daily_ceiling: float,
) -> Literal["allow", "block"]:
    """Decide whether a Claude call may proceed under the active cost ceilings.

    Args:
        stage: One of ``"draft"``, ``"critic"``, ``"regen_haiku"``, or
            ``"escalation_sonnet"``. Any other value is treated as an unknown
            stage and allowed (defensive: never block on a misspelled stage).
        projected_cost: Estimated USD cost of the call about to be made.
        batch_spend: USD already spent in the current batch (in-memory counter
            on the orchestrator instance).
        daily_spend: USD already spent today (read from ``daily_cost_log``,
            keyed by UTC date).
        batch_ceiling: ``settings.batch_cost_ceiling_usd``.
        daily_ceiling: ``settings.daily_cost_ceiling_usd``.

    Returns:
        ``"allow"`` if the call may proceed, ``"block"`` if the gate must
        prevent it. Only ``escalation_sonnet`` ever returns ``"block"``; every
        other stage (including unknown stages) returns ``"allow"``.

    Notes:
        Pure function — no I/O, no logging, no module-level state mutation.
        Idempotent and safe to call from any thread.
    """

    # Non-escalation stages bypass the gate entirely (R9.5: only Sonnet
    # escalation is subject to the ceiling). Unknown stages also fall here
    # because we never want a typo in the caller to silently block work.
    if stage != _GATED_STAGE:
        return "allow"

    # Strict ``>`` ensures that exactly hitting the ceiling is still permitted
    # — only crossing it triggers a block. This matches the task acceptance
    # examples, where ``0.49 + 0.05 = 0.54 > 0.50`` blocks but ``0.45 + 0.05 =
    # 0.50`` would not. Strict comparison also guarantees Property 5
    # monotonicity: if the inequality holds at ``spend_a``, it holds for any
    # larger ``spend_b`` because both projected sums increase monotonically.
    if batch_spend + projected_cost > batch_ceiling:
        return "block"
    if daily_spend + projected_cost > daily_ceiling:
        return "block"

    return "allow"
