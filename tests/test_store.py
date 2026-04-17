"""Tests for agx.store.LocalStore"""

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from agx.store import LocalStore
from agx._models import (
    AgxSpan,
    RunOutcome,
    Vaccine,
    VaccineManifest,
    ExecutableAssertion,
    AssertionEngine,
    AssertionSeverity,
    FailureCategory,
)


def _make_span(agent_name: str = "test_agent", outcome: RunOutcome = RunOutcome.SUCCESS) -> AgxSpan:
    return AgxSpan(
        agent_name=agent_name,
        input_prompt="hello",
        output_snapshot="world",
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# Fixture: isolated on-disk store per test
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield LocalStore(data_dir=Path(tmpdir))


# ---------------------------------------------------------------------------
# 1. Save a span then retrieve it by ID
# ---------------------------------------------------------------------------


class TestSaveAndRetrieve:
    def test_save_and_get_run(self, temp_store):
        span = _make_span()
        asyncio.run(temp_store.save_span(span))

        row = asyncio.run(temp_store.get_run(span.id))
        assert row is not None
        assert row["id"] == span.id
        assert row["agent_name"] == "test_agent"
        assert row["outcome"] == "SUCCESS"

    def test_get_run_missing_returns_none(self, temp_store):
        assert asyncio.run(temp_store.get_run("nonexistent-id")) is None


# ---------------------------------------------------------------------------
# 2. list_runs — agent_name filter, outcome filter, limit
# ---------------------------------------------------------------------------


class TestListRunsFiltering:
    def test_agent_name_filter(self, temp_store):
        asyncio.run(temp_store.save_span(_make_span("agent_a")))
        asyncio.run(temp_store.save_span(_make_span("agent_b")))

        rows = asyncio.run(temp_store.list_runs(agent_name="agent_a"))
        assert len(rows) == 1
        assert rows[0]["agent_name"] == "agent_a"

    def test_outcome_filter(self, temp_store):
        asyncio.run(temp_store.save_span(_make_span(outcome=RunOutcome.SUCCESS)))
        asyncio.run(temp_store.save_span(_make_span(outcome=RunOutcome.FAILURE)))

        failures = asyncio.run(temp_store.list_runs(outcome="FAILURE"))
        assert len(failures) == 1
        assert failures[0]["outcome"] == "FAILURE"

    def test_limit(self, temp_store):
        for _ in range(5):
            asyncio.run(temp_store.save_span(_make_span()))

        rows = asyncio.run(temp_store.list_runs(limit=3))
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# 3. In-memory fallback — no files created
# ---------------------------------------------------------------------------


class TestInMemoryFallback:
    def test_in_memory_uses_dicts_not_disk(self, tmp_path):
        store = LocalStore(in_memory=True)

        span = _make_span()
        asyncio.run(store.save_span(span))

        # Verify nothing was written to the tmp_path (store never touches it)
        assert store._traces_db_path is None
        assert store._vaccines_dir is None

        # Verify the span is retrievable from in-memory dict
        row = asyncio.run(store.get_run(span.id))
        assert row is not None
        assert row["agent_name"] == "test_agent"

    def test_in_memory_list_runs(self):
        store = LocalStore(in_memory=True)
        asyncio.run(store.save_span(_make_span("mem_agent")))
        asyncio.run(store.save_span(_make_span("mem_agent")))

        rows = asyncio.run(store.list_runs(agent_name="mem_agent"))
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# 4. Vaccine hot-reload — picks up mtime change
# ---------------------------------------------------------------------------


class TestVaccineHotReload:
    def test_hot_reload_on_mtime_change(self, temp_store):
        manifest_v1 = VaccineManifest(agent_name="reload_agent", vaccines=[], version=1)
        temp_store.save_vaccines(manifest_v1)

        # First load
        loaded_v1 = temp_store.load_vaccines("reload_agent")
        assert len(loaded_v1.vaccines) == 0

        # Simulate a file update by saving a new version and advancing mtime
        manifest_v2 = VaccineManifest(
            agent_name="reload_agent",
            version=2,
            vaccines=[
                Vaccine(
                    id="vax_hot_001",
                    failure_category=FailureCategory.SCHEMA_VIOLATION,
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
        # Force an mtime advance by briefly sleeping then saving
        time.sleep(0.01)
        temp_store.save_vaccines(manifest_v2)

        # Invalidate cache by clearing stored mtime
        temp_store._vaccine_mtime.pop("reload_agent", None)

        loaded_v2 = temp_store.load_vaccines("reload_agent")
        assert len(loaded_v2.vaccines) == 1
        assert loaded_v2.vaccines[0].id == "vax_hot_001"


# ---------------------------------------------------------------------------
# 5. Vaccine save / load roundtrip
# ---------------------------------------------------------------------------


class TestVaccineSaveLoadRoundtrip:
    def test_roundtrip_preserves_all_fields(self, temp_store):
        manifest = VaccineManifest(
            agent_name="roundtrip_agent",
            version=3,
            vaccines=[
                Vaccine(
                    id="vax_rt_001",
                    failure_category=FailureCategory.PROMPT_INJECTION,
                    confidence=0.88,
                    executable_assertions=[
                        ExecutableAssertion(
                            engine=AssertionEngine.REGEX,
                            pattern=r"ignore.*instructions",
                            severity=AssertionSeverity.BLOCK,
                            absence=True,
                        )
                    ],
                )
            ],
        )

        temp_store.save_vaccines(manifest)
        loaded = temp_store.load_vaccines("roundtrip_agent")

        assert loaded.agent_name == "roundtrip_agent"
        assert loaded.version == 3
        assert len(loaded.vaccines) == 1
        vax = loaded.vaccines[0]
        assert vax.id == "vax_rt_001"
        assert vax.confidence == pytest.approx(0.88)
        assert vax.failure_category == FailureCategory.PROMPT_INJECTION
        assert vax.executable_assertions[0].absence is True

    def test_save_vaccines_in_memory(self):
        store = LocalStore(in_memory=True)
        manifest = VaccineManifest(agent_name="mem_vax_agent")
        path = store.save_vaccines(manifest)

        assert path is None  # no file written
        loaded = store.load_vaccines("mem_vax_agent")
        assert loaded.agent_name == "mem_vax_agent"


# ---------------------------------------------------------------------------
# 6. Basic concurrent save safety (multiple spans in quick succession)
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    def test_multiple_spans_saved_correctly(self, temp_store):
        async def _save_many():
            spans = [_make_span() for _ in range(10)]
            for span in spans:
                await temp_store.save_span(span)
            return spans

        spans = asyncio.run(_save_many())
        rows = asyncio.run(temp_store.list_runs(limit=20))
        assert len(rows) == 10
        saved_ids = {r["id"] for r in rows}
        for span in spans:
            assert span.id in saved_ids
