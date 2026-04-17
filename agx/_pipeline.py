"""
AG-X Community Edition — LocalPipeline

Implements Phase B (cage assertion check) and Phase B2 (vaccine persistence)
as a lightweight local pipeline — no database connections beyond LocalStore.

In cloud mode (AGX_ENDPOINT is set), execute() POSTs to the TraceGuard Ω
REST API instead of running locally. Same interface, zero code change for
the guard decorator.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from agx._config import settings
from agx._models import AgxSpan, CageResult, RunOutcome, VaccineManifest
from agx.cage import DeterministicCage
from agx.store import LocalStore

log = logging.getLogger(__name__)


class PipelineResult:
    """Returned by LocalPipeline.execute()."""

    __slots__ = ("span", "blocked", "output")

    def __init__(self, span: AgxSpan, blocked: bool, output: Any) -> None:
        self.span = span
        self.blocked = blocked
        self.output = output


class LocalPipeline:
    """Runs Phase B + B2 locally for one @agx.protect invocation.

    Phase B  — cage assertion check on the agent's output
    Phase B2 — persist trace to LocalStore; fire vaccines (log + metrics)

    If AGX_ENDPOINT is set, delegates to _cloud_execute() instead.
    """

    def __init__(self, store: LocalStore) -> None:
        self._store = store

    async def execute(
        self,
        *,
        agent_name: str,
        session_id: str,
        input_prompt: Optional[str],
        output: Any,
        vaccines: Optional[VaccineManifest] = None,
        start_time: float,
    ) -> PipelineResult:
        """Run the pipeline. Returns a PipelineResult."""
        if settings.cloud_mode:
            return await self._cloud_execute(
                agent_name=agent_name,
                session_id=session_id,
                input_prompt=input_prompt,
                output=output,
                start_time=start_time,
            )

        return await self._local_execute(
            agent_name=agent_name,
            session_id=session_id,
            input_prompt=input_prompt,
            output=output,
            vaccines=vaccines,
            start_time=start_time,
        )

    async def _local_execute(
        self,
        *,
        agent_name: str,
        session_id: str,
        input_prompt: Optional[str],
        output: Any,
        vaccines: Optional[VaccineManifest],
        start_time: float,
    ) -> PipelineResult:
        # --- Phase B: cage assertions ---
        output_str = _to_str(output)
        cage_result: Optional[CageResult] = None
        vaccines_fired: list[str] = []
        blocked = False

        if vaccines and vaccines.vaccines:
            assertions = []
            for vaccine in vaccines.vaccines:
                # Collect assertions from this vaccine
                assertions.extend(vaccine.executable_assertions)
                # Track which vaccines have assertions (fired if any assertion runs)
                if vaccine.executable_assertions:
                    vaccines_fired.append(vaccine.id)

            if assertions:
                cage = DeterministicCage(assertions=assertions)
                cage_result = cage.run(output_str)
                blocked = cage_result.blocked

                if not cage_result.passed:
                    # Log per-vaccine violations
                    for verdict in cage_result.verdicts:
                        if not verdict.passed:
                            log.warning(
                                "AGX [%s] assertion failed (%s/%s): %s",
                                agent_name,
                                verdict.assertion.engine.value,
                                verdict.assertion.severity.value,
                                verdict.message,
                            )

        # --- Phase B2: determine outcome + persist ---
        total_ms = (time.monotonic() - start_time) * 1000

        if blocked:
            outcome = RunOutcome.BLOCKED
        elif cage_result and not cage_result.passed:
            outcome = RunOutcome.WARNED
        else:
            outcome = RunOutcome.SUCCESS

        span = AgxSpan(
            agent_name=agent_name,
            session_id=session_id,
            outcome=outcome,
            input_prompt=input_prompt,
            output_snapshot=output_str[:4096] if output_str else None,
            cage_result=cage_result,
            vaccines_fired=vaccines_fired,
            total_ms=round(total_ms, 3),
            timestamp=datetime.now(timezone.utc),
        )

        await self._store.save_span(span)

        return PipelineResult(span=span, blocked=blocked, output=output)

    async def _cloud_execute(
        self,
        *,
        agent_name: str,
        session_id: str,
        input_prompt: Optional[str],
        output: Any,
        start_time: float,
    ) -> PipelineResult:
        """Route execution to the AG-X Cloud API (upgrade bridge)."""
        try:
            import httpx
        except ImportError:
            log.warning(
                "httpx not installed — falling back to local execution. "
                "Install httpx to use AGX_ENDPOINT cloud routing."
            )
            return await self._local_execute(
                agent_name=agent_name,
                session_id=session_id,
                input_prompt=input_prompt,
                output=output,
                vaccines=None,
                start_time=start_time,
            )

        payload = {
            "agent_name": agent_name,
            "session_id": session_id,
            "input_prompt": input_prompt,
            "output_snapshot": _to_str(output)[:4096],
        }

        headers = {"Content-Type": "application/json"}
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{settings.endpoint}/v1/pipeline/execute",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                blocked = data.get("blocked", False)
                span_data = data.get("span", {})
                span = AgxSpan(
                    agent_name=agent_name,
                    session_id=session_id,
                    outcome=RunOutcome(span_data.get("outcome", "SUCCESS")),
                    input_prompt=input_prompt,
                    output_snapshot=_to_str(output)[:4096],
                )
                return PipelineResult(span=span, blocked=blocked, output=output)
        except Exception as exc:
            log.warning("AGX cloud pipeline failed (%s); running locally", exc)
            return await self._local_execute(
                agent_name=agent_name,
                session_id=session_id,
                input_prompt=input_prompt,
                output=output,
                vaccines=None,
                start_time=start_time,
            )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _to_str(value: Any) -> str:
    """Convert any Python value to a string representation."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        import json
        try:
            return json.dumps(value)
        except Exception:
            pass
    return str(value)
