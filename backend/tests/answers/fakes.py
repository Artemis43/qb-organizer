"""Test seam for the medical-grade answer pipeline.

Implements ``FakeClaudeClient``: an in-memory stand-in for
``claude.client.ClaudeClient`` whose ``request(...)`` method returns canned
responses keyed by a prompt fingerprint, so unit and integration tests can
exercise the pipeline without making real API calls.

Design references:
- design.md §Prework Analysis → Test seam
- design.md §Test Placement
- claude/client.py::ClaudeClient.request (signature mirrored below)

Fingerprint scheme
------------------
A request fingerprint is the SHA-1 hex digest of
``(system + user_text[:512]).encode()``, where ``user_text`` is the content of
the first user message in ``messages``. This intentionally ignores ``model``,
``max_tokens``, ``task_type``, ``subject``, and ``request_id`` — those vary
across stages but should not change which canned response a test wants
returned.

Usage
-----
    fake = FakeClaudeClient()
    fp = FakeClaudeClient.fingerprint(system="...", user_text="...")
    fake.register(fp, {"intent": "discuss", "focus_topic": "DKA"})
    # OR a global default for unknown fingerprints:
    fake.register_default({"intent": "discuss", "focus_topic": ""})

    response = await fake.request(
        messages=[{"role": "user", "content": "..."}],
        system="...",
        model="haiku",
    )

    assert fake.call_count == 1
    assert fake.calls[0]["system"] == "..."
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional


class FakeClaudeClient:
    """In-memory fake of ``claude.client.ClaudeClient``.

    Mirrors the public ``async def request(...)`` signature so it can be
    swapped in via dependency injection at the ``pipeline.py`` boundary
    (``claude_client_factory`` parameter, defaulting to ``get_claude_client``).
    """

    def __init__(self) -> None:
        # fingerprint → response dict (or raw text payload {"text": "..."})
        self._responses: dict[str, Any] = {}
        # Optional fallback when no fingerprint is registered.
        self._default: Optional[Any] = None
        # Call counter and recorded call log for assertions.
        self.call_count: int = 0
        self.calls: list[dict] = []

    # ── fingerprint helpers ────────────────────────────────────────────

    @staticmethod
    def fingerprint(system: str, user_text: str) -> str:
        """Compute the SHA-1 fingerprint used to key canned responses.

        ``user_text`` is truncated to 512 characters to keep fingerprints
        stable when the tail of the prompt is large/varying retrieval text.
        """
        payload = (system or "") + (user_text or "")[:512]
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _extract_user_text(messages: list[dict]) -> str:
        """Return the content of the first user message, or '' if none."""
        for m in messages or []:
            if m.get("role") == "user":
                content = m.get("content", "")
                # Anthropic SDK supports both str and list-of-block content.
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts: list[str] = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(str(block.get("text", "")))
                        elif isinstance(block, str):
                            parts.append(block)
                    return "".join(parts)
                return str(content)
        return ""

    # ── registration API ──────────────────────────────────────────────

    def register(self, fingerprint: str, response: Any) -> None:
        """Register a canned response for an exact prompt fingerprint."""
        self._responses[fingerprint] = response

    def register_default(self, response: Any) -> None:
        """Register a fallback returned for any unknown fingerprint."""
        self._default = response

    def reset(self) -> None:
        """Clear all registrations and call history."""
        self._responses.clear()
        self._default = None
        self.call_count = 0
        self.calls.clear()

    # ── public API mirroring ClaudeClient ──────────────────────────────

    async def request(
        self,
        messages: list[dict],
        system: str = "",
        model: str = "",
        max_tokens: int = 4096,
        task_type: str = "general",
        subject: str = "",
        response_schema: Any = None,
        request_id: str = "",
    ) -> Any:
        """Return the registered response for the request fingerprint.

        Raises ``KeyError`` if neither an exact match nor a default response
        has been registered — tests must explicitly opt in to behaviour.
        """
        user_text = self._extract_user_text(messages)
        fp = self.fingerprint(system=system, user_text=user_text)

        self.call_count += 1
        self.calls.append({
            "system": system,
            "user_text": user_text,
            "model": model,
            "task_type": task_type,
            "subject": subject,
            "request_id": request_id,
            "fingerprint": fp,
        })

        if fp in self._responses:
            return self._clone(self._responses[fp])
        if self._default is not None:
            return self._clone(self._default)

        raise KeyError(
            f"FakeClaudeClient: no response registered for fingerprint {fp[:12]}… "
            f"(system_len={len(system)}, user_len={len(user_text)}). "
            "Call register(fp, response) or register_default(response) before requesting."
        )

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _clone(response: Any) -> Any:
        """Return an isolated copy of the response so tests can mutate freely."""
        if isinstance(response, (dict, list)):
            # Round-trip via JSON for a deep, stdlib-only clone of plain data.
            try:
                return json.loads(json.dumps(response))
            except (TypeError, ValueError):
                return response
        return response
