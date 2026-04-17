"""Tests for agx.scanner"""

import os
import tempfile
import json
from pathlib import Path

import pytest

from agx.scanner.analyzer import analyze
from agx.scanner.heuristics import suggest_vaccines, HEURISTIC_RULES
from agx.scanner.yaml_exporter import export_yaml, import_yaml
from agx._models import (
    AssertionSeverity,
    FailureCategory,
    VaccineManifest,
    Vaccine,
    ExecutableAssertion,
    AssertionEngine,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_LOGS = FIXTURES_DIR / "sample_logs.jsonl"


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class TestAnalyzer:
    def test_analyze_sample_logs(self):
        report = analyze(str(SAMPLE_LOGS), agent_name="test_agent")
        assert report.agent_name == "test_agent"
        assert report.total_runs >= 1
        assert report.failure_count >= 1
        assert len(report.patterns) >= 1

    def test_detects_schema_violations(self):
        report = analyze(str(SAMPLE_LOGS), agent_name="test_agent")
        categories = [p.category for p in report.patterns]
        assert FailureCategory.SCHEMA_VIOLATION in categories

    def test_detects_hallucinations(self):
        report = analyze(str(SAMPLE_LOGS), agent_name="test_agent")
        categories = [p.category for p in report.patterns]
        assert FailureCategory.HALLUCINATION in categories

    def test_detects_prompt_injection(self):
        report = analyze(str(SAMPLE_LOGS), agent_name="test_agent")
        categories = [p.category for p in report.patterns]
        assert FailureCategory.PROMPT_INJECTION in categories

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            analyze("/nonexistent/path/logs.jsonl")

    def test_agent_filter(self):
        report = analyze(str(SAMPLE_LOGS), agent_name="other_agent")
        # other_agent only has SUCCESS records
        assert report.failure_count == 0

    def test_suggested_vaccines_generated(self):
        report = analyze(str(SAMPLE_LOGS), agent_name="test_agent")
        assert len(report.suggested_vaccines) >= 1

    def test_has_block_violations_flag(self):
        report = analyze(str(SAMPLE_LOGS), agent_name="test_agent")
        # SCHEMA_VIOLATION maps to BLOCK severity
        assert report.has_block_violations

    def test_inline_jsonl(self):
        """Test with a temporary JSONL file."""
        records = [
            {"agent_name": "inline_agent", "outcome": "FAILURE", "output_snapshot": "probably wrong"},
            {"agent_name": "inline_agent", "outcome": "SUCCESS", "output_snapshot": '{"ok": true}'},
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
            tmp = f.name

        try:
            report = analyze(tmp, agent_name="inline_agent")
            assert report.agent_name == "inline_agent"
            assert report.failure_count == 1
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------


class TestHeuristics:
    def test_all_categories_have_rules(self):
        expected = [
            FailureCategory.SCHEMA_VIOLATION,
            FailureCategory.HALLUCINATION,
            FailureCategory.PROMPT_INJECTION,
            FailureCategory.LOOP_DETECTION,
            FailureCategory.REFUSAL,
        ]
        for cat in expected:
            assert cat in HEURISTIC_RULES

    def test_suggest_vaccines_returns_list(self):
        from agx._models import FailureRecord
        failure_records = [
            FailureRecord(agent_name="x", outcome="FAILURE", output_snapshot="I think maybe yes"),
        ]
        category_samples = {
            FailureCategory.HALLUCINATION: ["I think maybe yes"],
        }
        vaccines = suggest_vaccines("x", failure_records, category_samples)
        assert len(vaccines) >= 1
        assert vaccines[0].failure_category == FailureCategory.HALLUCINATION

    def test_suggest_vaccines_empty_returns_empty(self):
        vaccines = suggest_vaccines("x", [], {})
        assert vaccines == []

    def test_schema_violation_rule_has_block(self):
        rule = HEURISTIC_RULES[FailureCategory.SCHEMA_VIOLATION]
        assert rule.assertion.severity == AssertionSeverity.BLOCK

    def test_injection_rule_has_block(self):
        rule = HEURISTIC_RULES[FailureCategory.PROMPT_INJECTION]
        assert rule.assertion.severity == AssertionSeverity.BLOCK

    def test_hallucination_rule_has_warn(self):
        rule = HEURISTIC_RULES[FailureCategory.HALLUCINATION]
        assert rule.assertion.severity == AssertionSeverity.WARN


# ---------------------------------------------------------------------------
# YAML Exporter / Importer
# ---------------------------------------------------------------------------


class TestYamlExporter:
    def _make_manifest(self) -> VaccineManifest:
        return VaccineManifest(
            agent_name="yaml_test_agent",
            vaccines=[
                Vaccine(
                    id="vax_yaml_001",
                    failure_category=FailureCategory.SCHEMA_VIOLATION,
                    confidence=0.9,
                    executable_assertions=[
                        ExecutableAssertion(
                            engine=AssertionEngine.JSON_SCHEMA,
                            pattern={"type": "object"},
                            severity=AssertionSeverity.BLOCK,
                        )
                    ],
                )
            ],
        )

    def test_roundtrip(self, tmp_path):
        manifest = self._make_manifest()
        path = tmp_path / "vaccines.yaml"
        export_yaml(manifest, path)

        assert path.exists()
        loaded = import_yaml(path)

        assert loaded.agent_name == "yaml_test_agent"
        assert len(loaded.vaccines) == 1
        assert loaded.vaccines[0].id == "vax_yaml_001"
        assert loaded.vaccines[0].confidence == pytest.approx(0.9)

    def test_export_creates_parent_dir(self, tmp_path):
        manifest = self._make_manifest()
        path = tmp_path / "nested" / "dir" / "vaccines.yaml"
        export_yaml(manifest, path)
        assert path.exists()

    def test_import_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            import_yaml("/nonexistent/vaccines.yaml")

    def test_yaml_has_header_comment(self, tmp_path):
        manifest = self._make_manifest()
        path = tmp_path / "v.yaml"
        export_yaml(manifest, path)
        content = path.read_text()
        assert "AG-X Community Edition" in content
        assert "target field values" in content
