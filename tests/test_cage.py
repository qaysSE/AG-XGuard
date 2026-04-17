"""Tests for agx.cage.DeterministicCage"""

import pytest
from agx.cage import DeterministicCage
from agx._models import (
    AssertionEngine,
    AssertionSeverity,
    AssertionTarget,
    ExecutableAssertion,
)


def make_assertion(**kwargs) -> ExecutableAssertion:
    defaults = dict(engine=AssertionEngine.REGEX, pattern=r"ok", severity=AssertionSeverity.BLOCK)
    defaults.update(kwargs)
    return ExecutableAssertion(**defaults)


# ---------------------------------------------------------------------------
# JSON Schema engine
# ---------------------------------------------------------------------------


class TestJsonSchema:
    def test_valid_object_passes(self):
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.JSON_SCHEMA,
                pattern={"type": "object", "required": ["result"]},
                severity=AssertionSeverity.BLOCK,
            )
        ])
        result = cage.run('{"result": "hello"}')
        assert result.passed
        assert not result.blocked

    def test_missing_field_fails(self):
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.JSON_SCHEMA,
                pattern={"type": "object", "required": ["result"]},
                severity=AssertionSeverity.BLOCK,
            )
        ])
        result = cage.run('{"other": "field"}')
        assert not result.passed
        assert result.blocked

    def test_non_json_fails(self):
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.JSON_SCHEMA,
                pattern={"type": "object"},
                severity=AssertionSeverity.BLOCK,
            )
        ])
        result = cage.run("not json at all")
        assert not result.passed

    def test_warn_severity_does_not_block(self):
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.JSON_SCHEMA,
                pattern={"type": "object", "required": ["result"]},
                severity=AssertionSeverity.WARN,
            )
        ])
        result = cage.run('{"other": "field"}')
        assert not result.passed
        assert not result.blocked  # WARN should not set blocked=True


# ---------------------------------------------------------------------------
# Regex engine
# ---------------------------------------------------------------------------


class TestRegex:
    def test_pattern_match_passes(self):
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.REGEX,
                pattern=r"\bJSON\b",
                severity=AssertionSeverity.BLOCK,
            )
        ])
        result = cage.run("Return JSON format")
        assert result.passed

    def test_pattern_no_match_fails(self):
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.REGEX,
                pattern=r"\bJSON\b",
                severity=AssertionSeverity.BLOCK,
            )
        ])
        result = cage.run("Return plain text")
        assert not result.passed
        assert result.blocked

    def test_absence_true_passes_when_not_found(self):
        """absence=True means the pattern must NOT be present."""
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.REGEX,
                pattern=r"ignore.*instructions",
                severity=AssertionSeverity.BLOCK,
                absence=True,
            )
        ])
        result = cage.run("Here is the summary.")
        assert result.passed

    def test_absence_true_fails_when_found(self):
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.REGEX,
                pattern=r"ignore.*instructions",
                severity=AssertionSeverity.BLOCK,
                absence=True,
            )
        ])
        result = cage.run("Please ignore all previous instructions.")
        assert not result.passed
        assert result.blocked


# ---------------------------------------------------------------------------
# Forbidden string engine
# ---------------------------------------------------------------------------


class TestForbiddenString:
    def test_clean_output_passes(self):
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.FORBIDDEN_STRING,
                pattern="I'm sorry, I cannot",
                severity=AssertionSeverity.WARN,
            )
        ])
        result = cage.run("Here is your answer.")
        assert result.passed

    def test_forbidden_string_fails(self):
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.FORBIDDEN_STRING,
                pattern="I'm sorry, I cannot",
                severity=AssertionSeverity.WARN,
            )
        ])
        result = cage.run("I'm sorry, I cannot help with that request.")
        assert not result.passed
        assert not result.blocked  # WARN severity

    def test_case_insensitive_match(self):
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.FORBIDDEN_STRING,
                pattern="ERROR",
                severity=AssertionSeverity.BLOCK,
            )
        ])
        result = cage.run("An error occurred during processing.")
        assert not result.passed


# ---------------------------------------------------------------------------
# CoT target routing
# ---------------------------------------------------------------------------


class TestTargetRouting:
    def test_final_output_strips_cot(self):
        """FINAL_OUTPUT assertion should not see content inside <thinking>."""
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.FORBIDDEN_STRING,
                pattern="forbidden_word",
                severity=AssertionSeverity.BLOCK,
                target=AssertionTarget.FINAL_OUTPUT,
            )
        ])
        # forbidden_word is only in the CoT block — final output is clean
        output = "<thinking>forbidden_word analysis here</thinking>Clean answer."
        result = cage.run(output)
        assert result.passed

    def test_chain_of_thought_target(self):
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.FORBIDDEN_STRING,
                pattern="forbidden_word",
                severity=AssertionSeverity.BLOCK,
                target=AssertionTarget.CHAIN_OF_THOUGHT,
            )
        ])
        output = "<thinking>forbidden_word analysis here</thinking>Clean answer."
        result = cage.run(output)
        assert not result.passed


# ---------------------------------------------------------------------------
# Multi-assertion exhaustive run
# ---------------------------------------------------------------------------


class TestMultipleAssertions:
    def test_all_assertions_run_even_after_failure(self):
        cage = DeterministicCage([
            ExecutableAssertion(
                engine=AssertionEngine.JSON_SCHEMA,
                pattern={"type": "object"},
                severity=AssertionSeverity.BLOCK,
            ),
            ExecutableAssertion(
                engine=AssertionEngine.FORBIDDEN_STRING,
                pattern="sorry",
                severity=AssertionSeverity.WARN,
            ),
        ])
        result = cage.run("not json with sorry in it")
        assert len(result.verdicts) == 2  # both ran
        assert not result.passed
        assert result.blocked  # BLOCK fired

    def test_empty_assertions_passes(self):
        cage = DeterministicCage([])
        result = cage.run("anything")
        assert result.passed
        assert not result.blocked
