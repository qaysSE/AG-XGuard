"""
AG-X Community Edition — DeterministicCage

Runs a list of ExecutableAssertions against agent output using three pure-Python
engines (regex, json_schema, forbidden_string). No LLM, no network, no DB.

Adapted from TraceGuard Ω traceguard/cage/deterministic.py.
Import path for ExecutableAssertion updated to agx._models.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import List, Optional, Union

from agx._models import (
    AssertionEngine,
    AssertionSeverity,
    AssertionTarget,
    AssertionVerdict,
    CageResult,
    ExecutableAssertion,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engine implementations
# ---------------------------------------------------------------------------


def _run_regex(assertion: ExecutableAssertion, text: str) -> AssertionVerdict:
    """regex engine: pattern must match (or must NOT match if absence=True)."""
    try:
        pattern = str(assertion.pattern)
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if assertion.absence:
            passed = match is None
            msg = (
                "Pattern absent as required"
                if passed
                else f"Forbidden pattern found: {match.group()!r}"
            )
        else:
            passed = match is not None
            msg = (
                f"Pattern matched: {match.group()!r}"
                if passed
                else f"Required pattern not found: {pattern!r}"
            )
    except re.error as exc:
        passed = False
        msg = f"Invalid regex pattern: {exc}"

    return AssertionVerdict(
        assertion=assertion,
        passed=passed,
        message=msg,
        target_snippet=text[:200] if not passed else None,
    )


def _run_json_schema(assertion: ExecutableAssertion, text: str) -> AssertionVerdict:
    """json_schema engine: parse text as JSON, validate against schema dict."""
    try:
        import jsonschema
    except ImportError:
        return AssertionVerdict(
            assertion=assertion,
            passed=False,
            message="jsonschema not installed; run: pip install jsonschema",
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return AssertionVerdict(
            assertion=assertion,
            passed=False,
            message=f"Output is not valid JSON: {exc}",
            target_snippet=text[:200],
        )

    schema = assertion.pattern if isinstance(assertion.pattern, dict) else {}
    try:
        jsonschema.validate(instance=data, schema=schema)
        return AssertionVerdict(assertion=assertion, passed=True, message="JSON schema valid")
    except jsonschema.ValidationError as exc:
        return AssertionVerdict(
            assertion=assertion,
            passed=False,
            message=f"JSON schema violation: {exc.message}",
            target_snippet=text[:200],
        )


def _run_forbidden_string(assertion: ExecutableAssertion, text: str) -> AssertionVerdict:
    """forbidden_string engine: pattern (string) must not appear in text."""
    needle = str(assertion.pattern)
    found = needle.lower() in text.lower()
    passed = not found
    msg = (
        f"Forbidden string not found (OK)"
        if passed
        else f"Forbidden string detected: {needle!r}"
    )
    return AssertionVerdict(
        assertion=assertion,
        passed=passed,
        message=msg,
        target_snippet=text[:200] if not passed else None,
    )


# ---------------------------------------------------------------------------
# Output target extraction
# ---------------------------------------------------------------------------

# Patterns used to split CoT from final output.
# Supports <thinking>…</thinking> and <reasoning>…</reasoning> blocks.
_COT_PATTERN = re.compile(
    r"<(?:thinking|reasoning)>(.*?)</(?:thinking|reasoning)>",
    re.IGNORECASE | re.DOTALL,
)


def _extract_target(output: str, target: AssertionTarget) -> str:
    """Extract the relevant portion of output for the given target."""
    if target == AssertionTarget.FULL_OUTPUT:
        return output

    cot_match = _COT_PATTERN.search(output)

    if target == AssertionTarget.CHAIN_OF_THOUGHT:
        return cot_match.group(1).strip() if cot_match else ""

    # FINAL_OUTPUT: strip out any CoT block
    if cot_match:
        return _COT_PATTERN.sub("", output).strip()
    return output


# ---------------------------------------------------------------------------
# DeterministicCage
# ---------------------------------------------------------------------------


class DeterministicCage:
    """Run a list of assertions against agent output deterministically.

    Usage::

        from agx import Cage, Assertion

        cage = Cage(assertions=[
            Assertion(engine="json_schema",
                      pattern={"type": "object", "required": ["result"]},
                      severity="BLOCK"),
            Assertion(engine="forbidden_string",
                      pattern="I cannot help",
                      severity="WARN"),
        ])

        result = cage.run('{"result": "hello"}')
        print(result.passed)   # True
        print(result.blocked)  # False
    """

    def __init__(self, assertions: Optional[List[ExecutableAssertion]] = None) -> None:
        self.assertions: List[ExecutableAssertion] = assertions or []

    def add(self, assertion: ExecutableAssertion) -> "DeterministicCage":
        """Fluent method to add an assertion."""
        self.assertions.append(assertion)
        return self

    def run(self, output: str) -> CageResult:
        """Run all assertions against *output*. Returns a CageResult.

        Execution is always exhaustive — every assertion is evaluated even after
        a failure, so callers get a complete picture of all violations.
        """
        start = time.monotonic()
        verdicts: List[AssertionVerdict] = []
        blocked = False

        for assertion in self.assertions:
            target_text = _extract_target(output, assertion.target)
            verdict = self._run_one(assertion, target_text)
            verdicts.append(verdict)

            if not verdict.passed and assertion.severity in (
                AssertionSeverity.BLOCK,
                AssertionSeverity.ROLLBACK,
            ):
                blocked = True
                log.warning(
                    "AGX CAGE BLOCK [%s/%s]: %s",
                    assertion.engine.value,
                    assertion.target.value,
                    verdict.message,
                )
            elif not verdict.passed and assertion.severity == AssertionSeverity.WARN:
                log.warning(
                    "AGX CAGE WARN [%s/%s]: %s",
                    assertion.engine.value,
                    assertion.target.value,
                    verdict.message,
                )

        duration_ms = (time.monotonic() - start) * 1000
        all_passed = all(v.passed for v in verdicts)

        return CageResult(
            passed=all_passed,
            blocked=blocked,
            verdicts=verdicts,
            duration_ms=round(duration_ms, 3),
        )

    @staticmethod
    def _run_one(assertion: ExecutableAssertion, text: str) -> AssertionVerdict:
        engine = assertion.engine
        if engine == AssertionEngine.REGEX:
            return _run_regex(assertion, text)
        elif engine == AssertionEngine.JSON_SCHEMA:
            return _run_json_schema(assertion, text)
        elif engine == AssertionEngine.FORBIDDEN_STRING:
            return _run_forbidden_string(assertion, text)
        else:
            return AssertionVerdict(
                assertion=assertion,
                passed=False,
                message=f"Unknown engine: {engine}",
            )

    def __repr__(self) -> str:
        return f"DeterministicCage(assertions={len(self.assertions)})"
