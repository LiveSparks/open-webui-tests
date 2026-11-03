"""The media-route blocker for the browser suites (the operator's speed program).

Route-blocks image, font and media resources so a UI journey's wall clock is
dominated by what assertions actually read (text and events) instead of asset
payloads. Opt-in through OWUI_BLOCK_MEDIA=1: unset keeps the context verbatim
(no blocked route), because nothing in the mapped chat surface asserts bytes
(three distinct cases do: the knowledge upload tests, file roundtrips and one
skills surface), and pytest-playwright itself only reproduces the full
resource pipeline when the test asks for it.
"""

from __future__ import annotations

import os


BLOCKED_TYPES = ("image", "font", "media")


def enabled(env: str | None = None) -> bool:
    return (env if env is not None else os.environ.get("OWUI_BLOCK_MEDIA", "")).strip().lower() in ("1", "true")


def attach(context) -> None:
    """Route-block the context's requests. Idempotent per context: the route
    handler caps the types it cares about and passes the rest through."""

    context.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in BLOCKED_TYPES
        else route.continue_(),
    )
