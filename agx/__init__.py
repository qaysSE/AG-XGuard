"""
AG-X Community Edition — Public API

Typical usage:

    import agx

    @agx.protect(agent_name="my_agent")
    async def my_agent(prompt: str) -> str:
        return await call_llm(prompt)

    # Manual cage usage:
    cage = agx.Cage(assertions=[
        agx.Assertion(engine="json_schema",
                      pattern={"type": "object"},
                      severity="BLOCK"),
    ])
    result = cage.run('{"ok": true}')

    # OTel setup (optional):
    agx.setup_otel()
"""

from __future__ import annotations

from agx._models import (
    AgxSpan,
    AssertionEngine,
    AssertionSeverity,
    AssertionTarget,
    CognitivePatch,
    ExecutableAssertion,
    FailureCategory,
    RunOutcome,
    StructuralPatch,
    Vaccine,
    VaccineManifest,
)
from agx.cage import DeterministicCage
from agx.guard import BlockedByGuardError, protect
from agx.store import LocalStore, get_store

# Convenient aliases
Cage = DeterministicCage
Assertion = ExecutableAssertion


def setup_otel(endpoint: str | None = None) -> bool:
    """Configure OpenTelemetry span export. Returns True on success.

    Requires: pip install agx-community[otel]

    Args:
        endpoint: OTLP gRPC endpoint. Defaults to AGX_OTEL_ENDPOINT env var
                  (http://localhost:4317 if unset).
    """
    from agx.otel import setup_otel as _setup
    return _setup(endpoint)


__all__ = [
    # Core decorators / classes
    "protect",
    "Cage",
    "Assertion",
    "DeterministicCage",
    "ExecutableAssertion",
    # Models
    "AgxSpan",
    "AssertionEngine",
    "AssertionSeverity",
    "AssertionTarget",
    "CognitivePatch",
    "StructuralPatch",
    "FailureCategory",
    "RunOutcome",
    "Vaccine",
    "VaccineManifest",
    # Storage
    "LocalStore",
    "get_store",
    # OTel
    "setup_otel",
    # Exceptions
    "BlockedByGuardError",
]

__version__ = "0.1.0"
