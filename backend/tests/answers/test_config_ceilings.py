"""Tests for the cost-ceiling fields on ``Settings``.

Design references
-----------------
- design.md §Cost Ceiling State Machine (R9)
- design.md §Database schema delta (closing paragraph on the two new
  settings fields)
- tasks.md → Task 35 (Add ``batch_cost_ceiling_usd`` and
  ``daily_cost_ceiling_usd`` to ``Settings`` and the env round-trip)

These tests verify that the two new ``Settings`` fields exist with the
defaults documented in the design (R9.5):

- ``batch_cost_ceiling_usd`` defaults to ``0.50``
- ``daily_cost_ceiling_usd`` defaults to ``5.00``

A fresh ``Settings()`` instance is constructed with ``_env_file=None`` so
that any developer's local ``backend/.env`` overrides do not influence the
default-value assertions. The live ``settings`` singleton is also asserted
to expose both attributes so Task 33's defensive
``getattr(settings, "batch_cost_ceiling_usd", 0.50)`` resolves to the
real attribute.

The ``PUT /api/settings`` round-trip is intentionally out of scope here
(it requires a FastAPI ``TestClient`` and a writable ``.env`` fixture).
"""
from __future__ import annotations

from config import Settings, settings


def test_batch_cost_ceiling_default_is_fifty_cents():
    """A fresh ``Settings`` defaults ``batch_cost_ceiling_usd`` to 0.50.

    R9.5 requires the per-batch ceiling to be a configurable float
    defaulting to 0.50 USD.
    """
    fresh = Settings(_env_file=None)
    assert fresh.batch_cost_ceiling_usd == 0.50
    assert isinstance(fresh.batch_cost_ceiling_usd, float)


def test_daily_cost_ceiling_default_is_five_dollars():
    """A fresh ``Settings`` defaults ``daily_cost_ceiling_usd`` to 5.00.

    R9.5 requires the per-day ceiling to be a configurable float
    defaulting to 5.00 USD.
    """
    fresh = Settings(_env_file=None)
    assert fresh.daily_cost_ceiling_usd == 5.00
    assert isinstance(fresh.daily_cost_ceiling_usd, float)


def test_live_settings_singleton_exposes_both_ceilings():
    """The live ``settings`` singleton must expose both attributes.

    Task 33's cost-gate plumbing uses
    ``getattr(settings, "batch_cost_ceiling_usd", 0.50)`` defensively;
    after Task 35 the real attribute should resolve so the ``getattr``
    fallback is never reached.
    """
    assert hasattr(settings, "batch_cost_ceiling_usd")
    assert hasattr(settings, "daily_cost_ceiling_usd")
    assert isinstance(settings.batch_cost_ceiling_usd, float)
    assert isinstance(settings.daily_cost_ceiling_usd, float)
