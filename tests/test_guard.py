"""Tests for agx.guard.protect decorator"""

import asyncio
import os
import pytest

os.environ.setdefault("AGX_DATA_DIR", "")  # in-memory mode for tests


from agx.guard import protect, BlockedByGuardError
from agx._models import ExecutableAssertion, AssertionEngine, AssertionSeverity, Vaccine, VaccineManifest
from agx.store import get_store


# ---------------------------------------------------------------------------
# Basic wrapping
# ---------------------------------------------------------------------------


class TestSyncGuard:
    def test_sync_function_passes_through(self):
        @protect(agent_name="test_sync")
        def agent(prompt: str) -> str:
            return f"answer to: {prompt}"

        result = agent("hello")
        assert result == "answer to: hello"

    def test_sync_function_exception_propagates(self):
        @protect(agent_name="test_sync_exc")
        def agent(prompt: str) -> str:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            agent("trigger")


class TestAsyncGuard:
    def test_async_function_passes_through(self):
        @protect(agent_name="test_async")
        async def agent(prompt: str) -> str:
            return f"async: {prompt}"

        result = asyncio.run(agent("world"))
        assert result == "async: world"

    def test_async_function_exception_propagates(self):
        @protect(agent_name="test_async_exc")
        async def agent(prompt: str) -> str:
            raise RuntimeError("async boom")

        with pytest.raises(RuntimeError, match="async boom"):
            asyncio.run(agent("trigger"))


# ---------------------------------------------------------------------------
# Trace storage
# ---------------------------------------------------------------------------


class TestTraceStorage:
    def test_run_stored_after_call(self):
        @protect(agent_name="test_store_agent")
        async def agent(prompt: str) -> str:
            return "stored"

        asyncio.run(agent("test input"))

        store = get_store()
        runs = asyncio.run(store.list_runs(agent_name="test_store_agent"))
        assert len(runs) >= 1
        assert runs[0]["outcome"] == "SUCCESS"
        assert runs[0]["input_prompt"] == "test input"

    def test_exception_stored_as_failure(self):
        @protect(agent_name="test_failure_agent")
        async def agent(prompt: str) -> str:
            raise ValueError("intentional")

        with pytest.raises(ValueError):
            asyncio.run(agent("bad input"))

        store = get_store()
        runs = asyncio.run(store.list_runs(agent_name="test_failure_agent"))
        assert any(r["outcome"] == "FAILURE" for r in runs)


# ---------------------------------------------------------------------------
# Vaccine-driven cage blocking
# ---------------------------------------------------------------------------


class TestVaccineBlocking:
    def setup_method(self):
        """Inject a vaccine manifest into the in-memory store."""
        manifest = VaccineManifest(
            agent_name="blocking_agent",
            vaccines=[
                Vaccine(
                    id="vax_block_test",
                    executable_assertions=[
                        ExecutableAssertion(
                            engine=AssertionEngine.JSON_SCHEMA,
                            pattern={"type": "object", "required": ["result"]},
                            severity=AssertionSeverity.BLOCK,
                        )
                    ],
                )
            ],
        )
        store = get_store()
        store._mem_vaccines["blocking_agent"] = manifest

    def test_valid_output_passes(self):
        @protect(agent_name="blocking_agent")
        async def agent(prompt: str) -> str:
            return '{"result": "ok"}'

        result = asyncio.run(agent("test"))
        assert result == '{"result": "ok"}'

    def test_invalid_output_blocked(self):
        @protect(agent_name="blocking_agent")
        async def agent(prompt: str) -> str:
            return "not json at all"

        with pytest.raises(BlockedByGuardError):
            asyncio.run(agent("test"))

    def test_raise_on_block_false_returns_output(self):
        @protect(agent_name="blocking_agent", raise_on_block=False)
        async def agent(prompt: str) -> str:
            return "not json"

        # Should not raise, returns raw output
        result = asyncio.run(agent("test"))
        assert result == "not json"


# ---------------------------------------------------------------------------
# Prompt extraction
# ---------------------------------------------------------------------------


class TestPromptExtraction:
    def test_extracts_prompt_kwarg(self):
        @protect(agent_name="prompt_kwarg_agent")
        async def agent(prompt: str) -> str:
            return "ok"

        asyncio.run(agent(prompt="extracted prompt"))

        store = get_store()
        runs = asyncio.run(store.list_runs(agent_name="prompt_kwarg_agent", limit=1))
        assert runs[0]["input_prompt"] == "extracted prompt"

    def test_extracts_first_string_positional(self):
        @protect(agent_name="prompt_pos_agent")
        async def agent(text: str) -> str:
            return "ok"

        asyncio.run(agent("positional prompt"))

        store = get_store()
        runs = asyncio.run(store.list_runs(agent_name="prompt_pos_agent", limit=1))
        assert runs[0]["input_prompt"] == "positional prompt"
