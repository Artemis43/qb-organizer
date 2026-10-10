"""Medical-Grade Answer Pipeline.

This package replaces the legacy ``backend/answers/generator.py`` with a
section-aware, KG-grounded, critic-verified pipeline. The legacy module is
preserved during the migration window and is removed by task 49.

Public API (R10, R12) re-exported from :mod:`answers.pipeline`:

- :func:`generate_answers` — batch driver (task 30).
- :func:`regenerate_answer` — single-mapping wrapper (task 31).
- :func:`purge_answers` — admin purge action (task 32, R10).
"""

from answers.pipeline import (
    generate_answers,
    purge_answers,
    regenerate_answer,
)

__all__ = [
    "generate_answers",
    "regenerate_answer",
    "purge_answers",
]
