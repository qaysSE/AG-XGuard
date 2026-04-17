"""
AG-X Community Edition — @agx.protect guard decorator

Wraps any sync or async agent function with:
  1. Prompt extraction from args/kwargs
  2. LocalPipeline execution (cage check + trace storage)
  3. OTel span emission (if configured)
  4. Cloud routing (if AGX_ENDPOINT is set)

Adapted from TraceGuard Ω traceguard/core/guard.py.
Changes from original:
  - pipeline.execute() → LocalPipeline.execute()
  - Removed DB-dependent get_active_structural_patches
  - Auto-generates session_id from uuid4() if not provided
  - Sync wrapping uses asyncio.run() or existing event loop
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from typing import Any, Callable, Optional, TypeVar, cast
from uuid import uuid4

from agx._config import settings
from agx._models import RunOutcome, AgxSpan
from agx._pipeline import LocalPipeline
from agx.store import get_store

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# ---------------------------------------------------------------------------
# Prompt extraction heuristics
# ---------------------------------------------------------------------------

# Names commonly used for the prompt/input argument
_PROMPT_ARG_NAMES = frozenset(
    {
        "prompt",
        "message",
        "messages",
        "text",
        "input",
        "query",
        "question",
        "request",
        "user_message",
        "user_input",
        "content",
    }
)


def _extract_prompt(fn: Callable, args: tuple, kwargs: dict) -> Optional[str]:
    """Best-effort extraction of the primary prompt string from call arguments.

    Strategy:
    1. Look for a kwarg whose name is in _PROMPT_ARG_NAMES
    2. Fall back to the first positional argument that is a string
    3. Return None if nothing suitable found
    """
    # 1. kwargs by name
    for name in _PROMPT_ARG_NAMES:
        val = kwargs.get(name)
        if val is not None:
            if isinstance(val, str):
                return val
            if isinstance(val, list):
                # OpenAI-style messages list: grab last user content
                for msg in reversed(val):
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        return str(msg.get("content", ""))
            return str(val)

    # 2. positional args by parameter name
    try:
        sig = inspect.signature(fn)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        for param_name, value in bound.arguments.items():
            if param_name == "self" or param_name == "cls":
                continue
            if param_name in _PROMPT_ARG_NAMES and isinstance(value, str):
                return value
    except (TypeError, ValueError):
        pass

    # 3. first string positional arg
    for arg in args:
        if isinstance(arg, str):
            return arg

    return None


# ---------------------------------------------------------------------------
# Cognitive patch injection
# ---------------------------------------------------------------------------


def _apply_cognitive_patches(prompt: Optional[str], agent_name: str) -> Optional[str]:
    """Inject vaccine cognitive patches into the prompt string before the LLM call."""
    if prompt is None:
        return None

    store = get_store()
    manifest = store.load_vaccines(agent_name)
    if not manifest.vaccines:
        return prompt

    # Sort patches by priority descending (highest priority applied first)
    patches = []
    for vaccine in manifest.vaccines:
        if vaccine.cognitive_patch:
            patches.append(vaccine.cognitive_patch)
    patches.sort(key=lambda p: p.priority, reverse=True)

    modified = prompt
    for patch in patches:
        ptype = patch.type.value
        instruction = patch.instruction.strip()
        if ptype == "PREPEND":
            modified = f"{instruction}\n\n{modified}"
        elif ptype == "APPEND":
            modified = f"{modified}\n\n{instruction}"
        elif ptype == "REPLACE":
            modified = instruction
        elif ptype == "INJECT_RULE":
            modified = f"{modified}\n\nRule: {instruction}"

    return modified


# ---------------------------------------------------------------------------
# Core guard logic (shared between sync and async wrappers)
# ---------------------------------------------------------------------------


async def _guarded_call(
    fn: Callable,
    agent_name: str,
    session_id: str,
    args: tuple,
    kwargs: dict,
) -> Any:
    """Execute *fn* with guard pipeline. Always returns the agent's output."""
    start = time.monotonic()
    store = get_store()
    pipeline = LocalPipeline(store)

    # Extract prompt before any patching
    raw_prompt = _extract_prompt(fn, args, kwargs)

    # Apply cognitive patches (modify prompt arg in kwargs if possible)
    patched_prompt = _apply_cognitive_patches(raw_prompt, agent_name)
    if patched_prompt is not None and patched_prompt != raw_prompt:
        kwargs = _inject_prompt(fn, args, kwargs, patched_prompt)

    # Load vaccines for cage assertions
    vaccines = store.load_vaccines(agent_name)

    # --- Call the actual agent function ---
    error: Optional[str] = None
    output: Any = None
    try:
        if asyncio.iscoroutinefunction(fn):
            output = await fn(*args, **kwargs)
        else:
            output = fn(*args, **kwargs)
    except Exception as exc:
        error = str(exc)
        log.error("AGX guard caught agent exception in %s: %s", agent_name, exc)
        # Persist error span
        total_ms = (time.monotonic() - start) * 1000
        span = AgxSpan(
            agent_name=agent_name,
            session_id=session_id,
            outcome=RunOutcome.FAILURE,
            input_prompt=raw_prompt,
            output_snapshot=None,
            vaccines_fired=[],
            total_ms=round(total_ms, 3),
            error=error,
        )
        await store.save_span(span)
        raise

    # --- Phase B + B2: cage check + persist ---
    result = await pipeline.execute(
        agent_name=agent_name,
        session_id=session_id,
        input_prompt=raw_prompt,
        output=output,
        vaccines=vaccines,
        start_time=start,
    )

    # --- OTel span emission ---
    _emit_otel_span(result.span)

    if result.blocked:
        raise BlockedByGuardError(
            f"AGX guard blocked output for agent '{agent_name}': "
            + "; ".join(
                v.message
                for v in (result.span.cage_result.verdicts if result.span.cage_result else [])
                if not v.passed
            ),
            output=output,
        )

    return output


def _inject_prompt(
    fn: Callable, args: tuple, kwargs: dict, new_prompt: str
) -> dict:
    """Return a copy of kwargs with the prompt argument replaced by new_prompt."""
    # Check if prompt is already in kwargs
    for name in _PROMPT_ARG_NAMES:
        if name in kwargs:
            return {**kwargs, name: new_prompt}

    # Check positional args by parameter name
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        for i, arg in enumerate(args):
            if i < len(params) and params[i] in _PROMPT_ARG_NAMES and isinstance(arg, str):
                # Can't mutate positional args easily; put in kwargs instead
                return {**kwargs, params[i]: new_prompt}
    except (TypeError, ValueError):
        pass

    return kwargs


def _emit_otel_span(span: AgxSpan) -> None:
    """Emit OTel span if configured. Silently skips if OTel not available."""
    if not settings.otel_enabled:
        return
    try:
        from agx import otel as _otel
        _otel.emit_span(span)
    except Exception as exc:
        log.debug("AGX OTel emission skipped: %s", exc)


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class BlockedByGuardError(RuntimeError):
    """Raised when a BLOCK-severity cage assertion fires.

    The ``output`` attribute holds the original (blocked) agent output so
    callers using ``raise_on_block=False`` can return it without re-invoking
    the agent function.
    """

    def __init__(self, message: str, *, output: Any = None) -> None:
        super().__init__(message)
        self.output = output


# ---------------------------------------------------------------------------
# @agx.protect decorator
# ---------------------------------------------------------------------------


def protect(
    agent_name: str,
    *,
    session_id: Optional[str] = None,
    raise_on_block: bool = True,
) -> Callable[[F], F]:
    """Decorator that adds AGX safety guardrails to an agent function.

    Args:
        agent_name:     Identifier for this agent (used for vaccine lookup + traces).
        session_id:     Optional fixed session ID. Defaults to a new UUID per call.
        raise_on_block: If True (default), raises BlockedByGuardError when a
                        BLOCK assertion fires. If False, logs a warning and
                        returns the raw output anyway.

    Usage::

        @agx.protect(agent_name="my_agent")
        async def my_agent(prompt: str) -> str:
            return await call_llm(prompt)

        # Sync functions work too
        @agx.protect(agent_name="sync_agent")
        def sync_agent(prompt: str) -> str:
            return call_llm_sync(prompt)
    """

    def decorator(fn: F) -> F:
        is_async = asyncio.iscoroutinefunction(fn)

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            sid = session_id or str(uuid4())
            try:
                return await _guarded_call(fn, agent_name, sid, args, kwargs)
            except BlockedByGuardError as exc:
                if raise_on_block:
                    raise
                log.warning("AGX guard blocked output (raise_on_block=False)")
                return exc.output  # return already-computed output; no second call

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            sid = session_id or str(uuid4())
            try:
                try:
                    loop = asyncio.get_running_loop()
                    running = True
                except RuntimeError:
                    running = False

                if running:
                    # Inside an existing event loop (e.g., Jupyter) — run in thread
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(
                            asyncio.run,
                            _guarded_call(fn, agent_name, sid, args, kwargs),
                        )
                        return future.result()
                else:
                    return asyncio.run(
                        _guarded_call(fn, agent_name, sid, args, kwargs)
                    )
            except BlockedByGuardError as exc:
                if raise_on_block:
                    raise
                log.warning("AGX guard blocked output (raise_on_block=False)")
                return exc.output  # return already-computed output; no second call

        if is_async:
            return cast(F, async_wrapper)
        return cast(F, sync_wrapper)

    return decorator
