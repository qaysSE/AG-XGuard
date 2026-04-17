"""Tests for agx._pipeline helpers and LocalPipeline local execution."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from agx._pipeline import LocalPipeline, _to_str
from agx._models import (
    AssertionEngine,
    AssertionSeverity,
    CognitivePatch,
    CognitivePatchType,
    ExecutableAssertion,
    FailureCategory,
    RunOutcome,
    Vaccine,
    VaccineManifest,
)
from agx.store import LocalStore


# ---------------------------------------------------------------------------
# _to_str helper
# ---------------------------------------------------------------------------


class TestToStr:
    def test_none_returns_empty(self):
        assert _to_str(None) == ""

    def test_string_passthrough(self):
        assert _to_str("hello") == "hello"

    def test_dict_returns_json(self):
        result = _to_str({"key": "value"})
        assert '"key"' in result
        assert '"value"' in result

    def test_list_returns_json(self):
        result = _to_str([1, 2, 3])
        assert result == "[1, 2, 3]"

    def test_int_returns_str(self):
        assert _to_str(42) == "42"

    def test_float_returns_str(self):
        assert _to_str(3.14) == "3.14"


# ---------------------------------------------------------------------------
# LocalPipeline — local execution outcomes
# ---------------------------------------------------------------------------


@pytest.fixture
def mem_store():
    return LocalStore(in_memory=True)


class TestLocalPipelineExecution:
    def test_success_outcome_no_vaccines(self, mem_store):
        pipeline = LocalPipeline(mem_store)
        import time
        result = asyncio.run(pipeline.execute(
            agent_name="pipe_agent",
            session_id="sid-001",
            input_prompt="hello",
            output="world",
            vaccines=None,
            start_time=time.monotonic(),
        ))
        assert not result.blocked
        assert result.span.outcome == RunOutcome.SUCCESS

    def test_blocked_outcome_with_vaccine(self, mem_store):
        import time
        manifest = VaccineManifest(
            agent_name="pipe_agent",
            vaccines=[
                Vaccine(
                    id="vax_pipe_001",
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
        pipeline = LocalPipeline(mem_store)
        result = asyncio.run(pipeline.execute(
            agent_name="pipe_agent",
            session_id="sid-002",
            input_prompt="test",
            output="not json",
            vaccines=manifest,
            start_time=time.monotonic(),
        ))
        assert result.blocked
        assert result.span.outcome == RunOutcome.BLOCKED

    def test_warn_outcome_with_vaccine(self, mem_store):
        import time
        manifest = VaccineManifest(
            agent_name="warn_agent",
            vaccines=[
                Vaccine(
                    id="vax_warn_001",
                    executable_assertions=[
                        ExecutableAssertion(
                            engine=AssertionEngine.FORBIDDEN_STRING,
                            pattern="sorry",
                            severity=AssertionSeverity.WARN,
                        )
                    ],
                )
            ],
        )
        pipeline = LocalPipeline(mem_store)
        result = asyncio.run(pipeline.execute(
            agent_name="warn_agent",
            session_id="sid-003",
            input_prompt="test",
            output="I am sorry about that.",
            vaccines=manifest,
            start_time=time.monotonic(),
        ))
        assert not result.blocked
        assert result.span.outcome == RunOutcome.WARNED


# ---------------------------------------------------------------------------
# Cognitive patch types — APPEND, REPLACE, INJECT_RULE
# ---------------------------------------------------------------------------


class TestCognitivePatchTypes:
    """Verify all CognitivePatch types are applied correctly by the guard."""

    def _make_manifest(self, patch_type: CognitivePatchType) -> VaccineManifest:
        return VaccineManifest(
            agent_name="patch_agent",
            vaccines=[
                Vaccine(
                    id=f"vax_{patch_type.value.lower()}",
                    cognitive_patch=CognitivePatch(
                        type=patch_type,
                        instruction="SAFETY_RULE",
                        priority=5,
                    ),
                )
            ],
        )

    def _run_agent(self, manifest: VaccineManifest):
        from agx.guard import protect, _apply_cognitive_patches
        store = LocalStore(in_memory=True)
        store._mem_vaccines["patch_agent"] = manifest
        # Patch the module-level singleton so guard picks it up
        import agx.store as store_mod
        original = store_mod._store
        store_mod._store = store
        try:
            return _apply_cognitive_patches("original prompt", "patch_agent")
        finally:
            store_mod._store = original

    def test_append_adds_instruction_after(self):
        manifest = self._make_manifest(CognitivePatchType.APPEND)
        result = self._run_agent(manifest)
        assert result.startswith("original prompt")
        assert "SAFETY_RULE" in result

    def test_replace_substitutes_entire_prompt(self):
        manifest = self._make_manifest(CognitivePatchType.REPLACE)
        result = self._run_agent(manifest)
        assert result == "SAFETY_RULE"

    def test_inject_rule_appends_rule_prefix(self):
        manifest = self._make_manifest(CognitivePatchType.INJECT_RULE)
        result = self._run_agent(manifest)
        assert "Rule: SAFETY_RULE" in result
        assert "original prompt" in result
