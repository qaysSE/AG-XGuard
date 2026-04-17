"""
AG-X Community Edition — Scanner Analyzer

Parses JSONL / plain-text log files and produces a ScanReport with:
  - Failure pattern counts
  - Common forbidden-string candidates
  - JSON schema violation detection
  - Injection-like pattern detection

No LLM required. Optional ML clustering if scikit-learn is installed.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from agx._models import (
    AssertionEngine,
    AssertionSeverity,
    AssertionTarget,
    ExecutableAssertion,
    FailureCategory,
    FailureRecord,
    PatternCount,
    ScanReport,
    Vaccine,
)
from agx.scanner.heuristics import HEURISTIC_RULES, suggest_vaccines

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Log parsers
# ---------------------------------------------------------------------------


def _parse_jsonl(line: str) -> Optional[FailureRecord]:
    """Parse one JSONL log line into a FailureRecord."""
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
        return FailureRecord(
            agent_name=data.get("agent_name", "unknown"),
            outcome=data.get("outcome", "FAILURE"),
            error_class=data.get("error_class"),
            input_prompt=data.get("input_prompt"),
            output_snapshot=data.get("output_snapshot"),
            timestamp=data.get("timestamp"),
            raw=line,
        )
    except json.JSONDecodeError:
        return None


_PLAIN_ERROR_RE = re.compile(
    r"(?:ERROR|WARN|FAILURE|CRITICAL).*?agent[=: ]+([^\s,]+).*?(?:error|exception)[=: ]+(.+)",
    re.IGNORECASE,
)


def _parse_plain_text(line: str) -> Optional[FailureRecord]:
    """Try to extract a failure record from a plain-text log line."""
    line = line.strip()
    if not re.search(r"ERROR|WARN|FAILURE|CRITICAL|Exception", line, re.IGNORECASE):
        return None
    m = _PLAIN_ERROR_RE.search(line)
    return FailureRecord(
        agent_name=m.group(1) if m else "unknown",
        outcome="FAILURE",
        error_class=m.group(2)[:64] if m else None,
        raw=line,
    )


def _load_records(log_path: str, agent_name: Optional[str] = None) -> List[FailureRecord]:
    """Load failure records from a JSONL or plain-text file."""
    path = Path(log_path)
    if not path.exists():
        raise FileNotFoundError(f"Log file not found: {log_path}")

    records: List[FailureRecord] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            rec = _parse_jsonl(raw_line) or _parse_plain_text(raw_line)
            if rec is None:
                continue
            if agent_name and rec.agent_name != agent_name:
                continue
            records.append(rec)

    return records


# ---------------------------------------------------------------------------
# Pattern detection helpers
# ---------------------------------------------------------------------------


def _detect_schema_violations(records: List[FailureRecord]) -> List[str]:
    """Return output_snapshot samples that are not valid JSON objects."""
    samples = []
    for rec in records:
        if not rec.output_snapshot:
            continue
        try:
            val = json.loads(rec.output_snapshot)
            if not isinstance(val, dict):
                samples.append(rec.output_snapshot[:200])
        except json.JSONDecodeError:
            samples.append(rec.output_snapshot[:200])
    return samples


def _detect_hallucinations(records: List[FailureRecord]) -> List[str]:
    """Return samples containing hallucination-indicating phrases."""
    pattern = re.compile(r"\b(I think|probably|maybe|I believe|I'm not sure)\b", re.IGNORECASE)
    return [
        r.output_snapshot[:200]
        for r in records
        if r.output_snapshot and pattern.search(r.output_snapshot)
    ]


def _detect_injections(records: List[FailureRecord]) -> List[str]:
    """Return samples containing prompt-injection-like patterns."""
    pattern = re.compile(
        r"ignore.{0,20}(previous|above|instructions|system)|"
        r"disregard.{0,20}(previous|instructions)|"
        r"you are now|act as|forget.{0,10}(previous|instructions)",
        re.IGNORECASE,
    )
    return [
        (r.input_prompt or r.output_snapshot or "")[:200]
        for r in records
        if (r.input_prompt or r.output_snapshot)
        and pattern.search(r.input_prompt or r.output_snapshot or "")
    ]


def _detect_loops(records: List[FailureRecord]) -> List[str]:
    """Return output samples with repeated fragments (loop detection)."""
    samples = []
    for rec in records:
        if not rec.output_snapshot or len(rec.output_snapshot) < 40:
            continue
        words = rec.output_snapshot.split()
        if len(words) < 8:
            continue
        # Check if any 4-gram repeats more than 2×
        four_grams = [" ".join(words[i : i + 4]) for i in range(len(words) - 3)]
        counts = Counter(four_grams)
        if counts and counts.most_common(1)[0][1] > 2:
            samples.append(rec.output_snapshot[:200])
    return samples


def _common_forbidden_strings(samples: List[str], top_n: int = 5) -> List[str]:
    """Extract the most common 3–6 word phrases across failure samples."""
    phrase_counts: Counter = Counter()
    for sample in samples:
        words = re.findall(r"\w+", sample.lower())
        for n in (3, 4):
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i : i + n])
                if len(phrase) > 8:
                    phrase_counts[phrase] += 1
    return [phrase for phrase, _ in phrase_counts.most_common(top_n) if _ > 1]


# ---------------------------------------------------------------------------
# ML clustering (optional)
# ---------------------------------------------------------------------------


def _cluster_with_sklearn(records: List[FailureRecord]) -> Dict[str, List[FailureRecord]]:
    """Group failure records by TF-IDF + K-Means clustering. Returns category→records map."""
    try:
        from sklearn.cluster import KMeans
        from sklearn.feature_extraction.text import TfidfVectorizer
        import numpy as np
    except ImportError:
        return {}

    texts = [
        (r.output_snapshot or r.error_class or r.raw or "")[:512]
        for r in records
    ]
    if len(texts) < 4:
        return {}

    n_clusters = min(5, len(texts) // 2)
    try:
        vect = TfidfVectorizer(max_features=200, stop_words="english")
        X = vect.fit_transform(texts)
        km = KMeans(n_clusters=n_clusters, n_init=5, random_state=42)
        labels = km.fit_predict(X)
        groups: Dict[str, List[FailureRecord]] = defaultdict(list)
        for rec, label in zip(records, labels):
            groups[f"cluster_{label}"].append(rec)
        return dict(groups)
    except Exception as exc:
        log.debug("sklearn clustering failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Public analyze() function
# ---------------------------------------------------------------------------


def analyze(
    log_path: str,
    agent_name: Optional[str] = None,
    total_run_count: Optional[int] = None,
) -> ScanReport:
    """Parse *log_path* and produce a ScanReport.

    Args:
        log_path:         Path to JSONL or plain-text log file.
        agent_name:       If provided, only analyse records for this agent.
        total_run_count:  Total run count for percentage calculation. Defaults
                          to len(failure records) if not provided.

    Returns:
        ScanReport with patterns, counts, and suggested vaccines.
    """
    records = _load_records(log_path, agent_name)
    effective_name = agent_name or (records[0].agent_name if records else "unknown")

    failure_records = [r for r in records if r.outcome.upper() != "SUCCESS"]
    total = total_run_count or max(len(records), 1)
    failure_count = len(failure_records)

    # Detect categories
    category_samples: Dict[FailureCategory, List[str]] = {
        FailureCategory.SCHEMA_VIOLATION: _detect_schema_violations(failure_records),
        FailureCategory.HALLUCINATION: _detect_hallucinations(failure_records),
        FailureCategory.PROMPT_INJECTION: _detect_injections(failure_records),
        FailureCategory.LOOP_DETECTION: _detect_loops(failure_records),
    }

    patterns: List[PatternCount] = []
    for category, samples in category_samples.items():
        if not samples:
            continue
        count = len(samples)
        pct = round(count / total * 100, 1)
        # Pick the most relevant heuristic assertion for this category
        rule = HEURISTIC_RULES.get(category)
        suggested = rule.assertion if rule else None
        patterns.append(
            PatternCount(
                category=category,
                count=count,
                percentage=pct,
                sample_outputs=samples[:3],
                suggested_assertion=suggested,
            )
        )

    # Sort by count descending
    patterns.sort(key=lambda p: p.count, reverse=True)

    # Build suggested vaccines
    suggested_vaccines = suggest_vaccines(
        effective_name, failure_records, category_samples
    )

    has_block = any(
        p.suggested_assertion and p.suggested_assertion.severity == AssertionSeverity.BLOCK
        for p in patterns
    )

    return ScanReport(
        agent_name=effective_name,
        total_runs=total,
        failure_count=failure_count,
        patterns=patterns,
        suggested_vaccines=suggested_vaccines,
        has_block_violations=has_block,
    )
