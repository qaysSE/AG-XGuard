"""
AG-X Community Edition — Optional LLM-powered vaccine doctor

Used by `agx scan` when OPENAI_API_KEY or GROQ_API_KEY is set.
Wraps the failure report and asks an LLM for richer vaccine suggestions.

This module is intentionally optional — if the API key is missing or the
import fails, agx scan falls back to heuristic suggestions without error.
"""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

from agx._models import ScanReport, Vaccine, VaccineManifest

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an AI safety engineer helping to generate safety vaccines for AI agents.
Given a scan report showing failure patterns, suggest concrete, actionable vaccines.

Each vaccine should have:
1. A cognitive_patch (prompt instruction to prevent the failure)
2. One or more executable_assertions (deterministic cage checks)

Use these assertion engines:
- json_schema: for output structure validation
- regex (with absence=true): for forbidden pattern detection
- forbidden_string: for exact string detection

Respond with a JSON array of vaccine objects matching this schema:
{
  "vaccines": [
    {
      "failure_category": "SCHEMA_VIOLATION|HALLUCINATION|PROMPT_INJECTION|LOOP_DETECTION|REFUSAL|UNKNOWN",
      "root_cause_summary": "...",
      "confidence": 0.0-1.0,
      "cognitive_patch": {
        "type": "PREPEND|APPEND|REPLACE|INJECT_RULE",
        "instruction": "...",
        "priority": 1-10,
        "scope": "GLOBAL|SESSION|TASK"
      },
      "executable_assertions": [
        {
          "engine": "json_schema|regex|forbidden_string",
          "pattern": "...",
          "severity": "WARN|BLOCK|ROLLBACK",
          "target": "final_output|chain_of_thought|full_output",
          "absence": false,
          "description": "..."
        }
      ]
    }
  ]
}
"""


def enhance_with_llm(report: ScanReport, agent_name: str) -> List[Vaccine]:
    """Call an LLM to generate additional vaccines beyond heuristic suggestions.

    Returns an empty list if no API key is set or if the call fails.
    Combines with (does not replace) heuristic vaccines.
    """
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY")
    if not api_key:
        return []

    try:
        return _call_openai_compatible(report, api_key)
    except Exception as exc:
        log.warning("AGX LLM doctor failed: %s", exc)
        return []


def _call_openai_compatible(report: ScanReport, api_key: str) -> List[Vaccine]:
    """Call OpenAI or Groq API with the scan report."""
    try:
        import openai
    except ImportError:
        log.debug("openai package not installed; skipping LLM doctor")
        return []

    use_groq = bool(os.environ.get("GROQ_API_KEY")) and not os.environ.get("OPENAI_API_KEY")

    client_kwargs = {"api_key": api_key}
    if use_groq:
        client_kwargs["base_url"] = "https://api.groq.com/openai/v1"

    client = openai.OpenAI(**client_kwargs)
    model = "llama3-70b-8192" if use_groq else "gpt-4o-mini"

    user_message = f"""
Scan Report for agent: {report.agent_name}
Total runs: {report.total_runs}
Failures: {report.failure_count}

Patterns detected:
{json.dumps([p.model_dump() for p in report.patterns], indent=2, default=str)}

Sample failure outputs:
{_format_samples(report)}

Please suggest vaccines to prevent these failures.
"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content or "{}"
    data = json.loads(content)
    vaccines_raw = data.get("vaccines", [])

    vaccines = []
    for raw in vaccines_raw:
        try:
            vaccines.append(Vaccine.model_validate(raw))
        except Exception as exc:
            log.debug("Failed to parse LLM vaccine: %s — %s", exc, raw)

    return vaccines


def _format_samples(report: ScanReport) -> str:
    """Format a few sample outputs from the report for the LLM prompt."""
    lines = []
    for pattern in report.patterns[:3]:
        for sample in pattern.sample_outputs[:2]:
            lines.append(f"  [{pattern.category.value}] {sample[:150]}")
    return "\n".join(lines) if lines else "  (no samples)"
