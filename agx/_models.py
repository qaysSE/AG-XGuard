"""
Core data models for AG-X Community Edition.

Adapted from TraceGuard Ω schemas.py + trace.py:
- ExecutableAssertion: a single cage assertion (engine + pattern + severity)
- CognitivePatch: prompt-level vaccine instruction
- StructuralPatch: output-rewriting vaccine instruction
- AgxSpan: a complete trace record for one agent invocation
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class AssertionEngine(str, Enum):
    REGEX = "regex"
    JSON_SCHEMA = "json_schema"
    FORBIDDEN_STRING = "forbidden_string"


class AssertionSeverity(str, Enum):
    WARN = "WARN"
    BLOCK = "BLOCK"
    ROLLBACK = "ROLLBACK"


class AssertionTarget(str, Enum):
    """Which part of the LLM output to run this assertion against.

    Aligns the open-source SDK with the cloud product's CoT routing feature.
    - FINAL_OUTPUT: the final user-visible response (default)
    - CHAIN_OF_THOUGHT: the <thinking> / scratchpad section
    - FULL_OUTPUT: the complete raw output including CoT
    """

    FINAL_OUTPUT = "final_output"
    CHAIN_OF_THOUGHT = "chain_of_thought"
    FULL_OUTPUT = "full_output"


class CognitivePatchType(str, Enum):
    PREPEND = "PREPEND"
    APPEND = "APPEND"
    REPLACE = "REPLACE"
    INJECT_RULE = "INJECT_RULE"


class PatchScope(str, Enum):
    GLOBAL = "GLOBAL"
    SESSION = "SESSION"
    TASK = "TASK"


class FailureCategory(str, Enum):
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    HALLUCINATION = "HALLUCINATION"
    PROMPT_INJECTION = "PROMPT_INJECTION"
    LOOP_DETECTION = "LOOP_DETECTION"
    REFUSAL = "REFUSAL"
    UNKNOWN = "UNKNOWN"


class RunOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    BLOCKED = "BLOCKED"
    WARNED = "WARNED"


# ---------------------------------------------------------------------------
# Assertion
# ---------------------------------------------------------------------------


class ExecutableAssertion(BaseModel):
    """A single deterministic assertion to run against agent output.

    Three engines are supported:
    - regex: re.search(pattern, output) must match (absence=True → must NOT match)
    - json_schema: jsonschema.validate(json.loads(output), pattern)
    - forbidden_string: pattern must not appear as a substring in output

    The optional `target` field enables CoT routing — run this assertion only
    against the specified part of the output (final_output by default).
    """

    engine: AssertionEngine
    pattern: Union[str, Dict[str, Any]]
    severity: AssertionSeverity = AssertionSeverity.BLOCK
    target: AssertionTarget = AssertionTarget.FINAL_OUTPUT
    description: Optional[str] = None
    absence: bool = False  # regex only: True means pattern must NOT match

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, v: Any) -> Any:
        if isinstance(v, (str, dict)):
            return v
        raise ValueError("pattern must be a string or dict (JSON Schema)")


class AssertionVerdict(BaseModel):
    """Result of running a single ExecutableAssertion."""

    assertion: ExecutableAssertion
    passed: bool
    message: str
    target_snippet: Optional[str] = None  # first 200 chars of checked text


# ---------------------------------------------------------------------------
# Cognitive / Structural Patches (vaccine instructions)
# ---------------------------------------------------------------------------


class CognitivePatch(BaseModel):
    """Prompt-level vaccine instruction injected before the LLM call."""

    type: CognitivePatchType = CognitivePatchType.PREPEND
    instruction: str
    priority: int = Field(default=5, ge=1, le=10)
    scope: PatchScope = PatchScope.GLOBAL


class StructuralPatch(BaseModel):
    """Post-generation output rewriting rule."""

    find: str
    replace: str
    is_regex: bool = False
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Vaccine (a complete safety rule combining patches + assertions)
# ---------------------------------------------------------------------------


class Vaccine(BaseModel):
    """One vaccine: a root-cause fix (cognitive patch) + runtime guards (assertions)."""

    id: str = Field(default_factory=lambda: f"vax_{uuid.uuid4().hex[:8]}")
    failure_category: FailureCategory = FailureCategory.UNKNOWN
    root_cause_summary: Optional[str] = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    cognitive_patch: Optional[CognitivePatch] = None
    structural_patch: Optional[StructuralPatch] = None
    executable_assertions: List[ExecutableAssertion] = Field(default_factory=list)


class VaccineManifest(BaseModel):
    """A collection of vaccines for one agent (maps to one YAML file)."""

    agent_name: str
    version: int = 1
    vaccines: List[Vaccine] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# AgxSpan — trace record for one agent invocation
# ---------------------------------------------------------------------------


class CageResult(BaseModel):
    """Outcome of running the cage against one output."""

    passed: bool
    blocked: bool = False
    verdicts: List[AssertionVerdict] = Field(default_factory=list)
    duration_ms: float = 0.0


class AgxSpan(BaseModel):
    """Complete trace record for one @agx.protect invocation.

    Renamed from TraceGuardSpan; stored in SQLite by LocalStore.
    Also emitted as an OTel span when OTel is configured.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_name: str
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    outcome: RunOutcome = RunOutcome.SUCCESS
    input_prompt: Optional[str] = None
    output_snapshot: Optional[str] = None
    cage_result: Optional[CageResult] = None
    vaccines_fired: List[str] = Field(default_factory=list)
    total_ms: float = 0.0
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"ser_json_timedelta": "iso8601"}


# ---------------------------------------------------------------------------
# Scanner models
# ---------------------------------------------------------------------------


class FailureRecord(BaseModel):
    """Parsed log entry representing one agent failure."""

    agent_name: str
    outcome: str = "FAILURE"
    error_class: Optional[str] = None
    input_prompt: Optional[str] = None
    output_snapshot: Optional[str] = None
    timestamp: Optional[str] = None
    raw: Optional[str] = None


class PatternCount(BaseModel):
    """Aggregated count of one failure pattern from the scanner."""

    category: FailureCategory
    count: int
    percentage: float
    sample_outputs: List[str] = Field(default_factory=list)
    suggested_assertion: Optional[ExecutableAssertion] = None


class ScanReport(BaseModel):
    """Full output of agx.scanner.analyze()."""

    agent_name: str
    total_runs: int
    failure_count: int
    patterns: List[PatternCount] = Field(default_factory=list)
    suggested_vaccines: List[Vaccine] = Field(default_factory=list)
    has_block_violations: bool = False  # True if any BLOCK-severity pattern found
