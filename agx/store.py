"""
AG-X Community Edition — LocalStore

Replaces Postgres + Redis + FalkorDB with two simple local backends:
  - Traces  → SQLite via aiosqlite  (~/.agx/traces.db)
  - Vaccines → YAML directory        (~/.agx/vaccines/<agent>.yaml)

In-memory fallback (AGX_DATA_DIR="") uses plain Python dicts — zero config,
useful for CI and unit tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from agx._config import settings
from agx._models import AgxSpan, RunOutcome, Vaccine, VaccineManifest

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_CREATE_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    agent_name   TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    input_prompt TEXT,
    output_snapshot TEXT,
    cage_passed  INTEGER,
    total_ms     REAL,
    vaccines_fired TEXT,
    error        TEXT,
    metadata     TEXT,
    timestamp    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs(agent_name);
CREATE INDEX IF NOT EXISTS idx_runs_ts    ON runs(timestamp);
"""

# ---------------------------------------------------------------------------
# LocalStore
# ---------------------------------------------------------------------------


class LocalStore:
    """Thread-safe local storage for traces (SQLite) and vaccines (YAML).

    Instantiate once and reuse — it holds the aiosqlite connection as a
    cached lazy resource. The `_conn` is created on first async use.

    Args:
        data_dir: Explicit storage root. When provided this overrides the
                  global ``settings.resolved_data_dir``. Pass a real
                  ``pathlib.Path`` to store data there (useful in tests).
                  Leave as ``None`` to use the global settings value.
        in_memory: When ``True``, forces in-memory-only mode regardless of
                   ``data_dir`` or settings. No filesystem writes occur.
                   Useful for unit tests that should not touch disk.
    """

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        *,
        in_memory: bool = False,
    ) -> None:
        self._data_dir_override = data_dir
        self._force_in_memory = in_memory

        self._conn = None  # aiosqlite.Connection, created lazily
        self._lock = asyncio.Lock()

        # In-memory fallbacks (used when resolved storage path is None)
        self._mem_runs: Dict[str, dict] = {}
        self._mem_vaccines: Dict[str, VaccineManifest] = {}

        # Vaccine hot-reload: track file mtimes
        self._vaccine_mtime: Dict[str, float] = {}
        self._vaccine_cache: Dict[str, VaccineManifest] = {}

        self._init_dirs()

    # -----------------------------------------------------------------------
    # Storage path helpers (respect data_dir override and in_memory flag)
    # -----------------------------------------------------------------------

    @property
    def _resolved_data_dir(self) -> Optional[Path]:
        if self._force_in_memory:
            return None
        if self._data_dir_override is not None:
            return self._data_dir_override
        return settings.resolved_data_dir

    @property
    def _traces_db_path(self) -> Optional[Path]:
        d = self._resolved_data_dir
        return d / "traces.db" if d else None

    @property
    def _vaccines_dir(self) -> Optional[Path]:
        d = self._resolved_data_dir
        return d / "vaccines" if d else None

    def _init_dirs(self) -> None:
        d = self._resolved_data_dir
        if d is None:
            return
        d.mkdir(parents=True, exist_ok=True)
        vd = self._vaccines_dir
        if vd:
            vd.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # SQLite connection lifecycle
    # -----------------------------------------------------------------------

    async def _get_conn(self):
        """Return (and lazily create) the aiosqlite connection."""
        if self._traces_db_path is None:
            return None  # in-memory mode

        if self._conn is None:
            async with self._lock:
                if self._conn is None:
                    try:
                        import aiosqlite
                    except ImportError:
                        log.error("aiosqlite not installed; run: pip install aiosqlite")
                        return None

                    self._conn = await aiosqlite.connect(str(self._traces_db_path))
                    self._conn.row_factory = aiosqlite.Row
                    await self._conn.executescript(_CREATE_RUNS_TABLE)
                    await self._conn.commit()
        return self._conn

    async def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # -----------------------------------------------------------------------
    # Trace storage
    # -----------------------------------------------------------------------

    async def save_span(self, span: AgxSpan) -> None:
        """Persist one AgxSpan. Non-blocking; silently continues on error."""
        conn = await self._get_conn()

        row = {
            "id": span.id,
            "agent_name": span.agent_name,
            "session_id": span.session_id,
            "outcome": span.outcome.value,
            "input_prompt": span.input_prompt,
            "output_snapshot": span.output_snapshot,
            "cage_passed": int(span.cage_result.passed) if span.cage_result else None,
            "total_ms": span.total_ms,
            "vaccines_fired": json.dumps(span.vaccines_fired),
            "error": span.error,
            "metadata": json.dumps(span.metadata),
            "timestamp": span.timestamp.isoformat(),
        }

        if conn is None:
            # In-memory fallback
            self._mem_runs[span.id] = row
            return

        try:
            await conn.execute(
                """
                INSERT OR REPLACE INTO runs
                  (id, agent_name, session_id, outcome, input_prompt,
                   output_snapshot, cage_passed, total_ms, vaccines_fired,
                   error, metadata, timestamp)
                VALUES
                  (:id, :agent_name, :session_id, :outcome, :input_prompt,
                   :output_snapshot, :cage_passed, :total_ms, :vaccines_fired,
                   :error, :metadata, :timestamp)
                """,
                row,
            )
            await conn.commit()
        except Exception as exc:
            log.warning("AGX store.save_span failed: %s", exc)

    async def list_runs(
        self,
        agent_name: Optional[str] = None,
        limit: int = 100,
        outcome: Optional[str] = None,
    ) -> List[dict]:
        """Return recent runs as plain dicts, newest first."""
        conn = await self._get_conn()

        if conn is None:
            rows = list(self._mem_runs.values())
            if agent_name:
                rows = [r for r in rows if r["agent_name"] == agent_name]
            if outcome:
                rows = [r for r in rows if r["outcome"] == outcome]
            rows.sort(key=lambda r: r["timestamp"], reverse=True)
            return rows[:limit]

        clauses, params = [], []
        if agent_name:
            clauses.append("agent_name = ?")
            params.append(agent_name)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        try:
            async with conn.execute(
                f"SELECT * FROM runs {where} ORDER BY timestamp DESC LIMIT ?",
                params,
            ) as cursor:
                return [dict(row) async for row in cursor]
        except Exception as exc:
            log.warning("AGX store.list_runs failed: %s", exc)
            return []

    async def get_run(self, run_id: str) -> Optional[dict]:
        """Fetch one run by ID."""
        conn = await self._get_conn()

        if conn is None:
            return self._mem_runs.get(run_id)

        try:
            async with conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None
        except Exception as exc:
            log.warning("AGX store.get_run failed: %s", exc)
            return None

    # -----------------------------------------------------------------------
    # Vaccine storage (YAML hot-reload)
    # -----------------------------------------------------------------------

    def _vaccine_path(self, agent_name: str) -> Optional[Path]:
        vd = self._vaccines_dir
        return vd / f"{agent_name}.yaml" if vd else None

    def load_vaccines(self, agent_name: str) -> VaccineManifest:
        """Load vaccines for *agent_name* from YAML, using hot-reload cache.

        Falls back to empty manifest if no file exists or data_dir is disabled.
        """
        path = self._vaccine_path(agent_name)

        if path is None:
            # In-memory mode: check dict cache
            return self._mem_vaccines.get(agent_name, VaccineManifest(agent_name=agent_name))

        if not path.exists():
            return VaccineManifest(agent_name=agent_name)

        mtime = path.stat().st_mtime
        if (
            agent_name not in self._vaccine_cache
            or self._vaccine_mtime.get(agent_name) != mtime
        ):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                manifest = VaccineManifest.model_validate(raw)
                self._vaccine_cache[agent_name] = manifest
                self._vaccine_mtime[agent_name] = mtime
                log.debug("AGX loaded vaccines for %s (%d entries)", agent_name, len(manifest.vaccines))
            except Exception as exc:
                log.warning("AGX failed to load vaccines for %s: %s", agent_name, exc)
                return VaccineManifest(agent_name=agent_name)

        return self._vaccine_cache[agent_name]

    def save_vaccines(self, manifest: VaccineManifest) -> Optional[Path]:
        """Write a VaccineManifest to YAML. Returns path written, or None in memory mode."""
        path = self._vaccine_path(manifest.agent_name)

        if path is None:
            self._mem_vaccines[manifest.agent_name] = manifest
            return None

        self._init_dirs()
        data = json.loads(manifest.model_dump_json())
        path.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self._vaccine_cache[manifest.agent_name] = manifest
        self._vaccine_mtime[manifest.agent_name] = path.stat().st_mtime
        log.debug("AGX saved vaccines for %s → %s", manifest.agent_name, path)
        return path

    def list_vaccine_files(self) -> List[Path]:
        """Return all .yaml files in the vaccines directory."""
        vd = self._vaccines_dir
        if vd is None or not vd.exists():
            return []
        return sorted(vd.glob("*.yaml"))

    def list_all_vaccines(self) -> List[VaccineManifest]:
        """Load and return all vaccine manifests."""
        return [
            self.load_vaccines(p.stem) for p in self.list_vaccine_files()
        ]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_store: Optional[LocalStore] = None
_store_lock = asyncio.Lock()


def get_store() -> LocalStore:
    """Return the module-level LocalStore singleton (creates on first call)."""
    global _store
    if _store is None:
        _store = LocalStore()
    return _store
