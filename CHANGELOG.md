# Changelog

All notable changes to AG-X Community Edition are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] — 2026-04-16

### Added

**Core guardrails**
- `@agx.protect(agent_name)` decorator — wraps sync and async agent functions with deterministic safety checks, prompt patching, and trace persistence
- `DeterministicCage` (`agx.Cage`) — pure-Python assertion runner with three engines: `regex`, `json_schema`, `forbidden_string`
- `AssertionTarget` field on every assertion — enables CoT routing (`final_output` / `chain_of_thought` / `full_output`), with automatic `<thinking>` / `<reasoning>` block extraction
- `BlockedByGuardError` — raised when a `BLOCK`-severity assertion fires; suppressed when `raise_on_block=False`
- Exhaustive assertion execution — all assertions run even after the first failure, giving a complete violation picture

**Vaccine system**
- YAML vaccine format (`~/.agx/vaccines/<agent>.yaml`) — git-committable safety rules combining `CognitivePatch` (prompt injection) and `ExecutableAssertion` (cage checks)
- Hot-reload — vaccine files re-parsed on mtime change with no restart required
- Five built-in heuristic rules: `SCHEMA_VIOLATION`, `HALLUCINATION`, `PROMPT_INJECTION`, `LOOP_DETECTION`, `REFUSAL`

**Local storage**
- `LocalStore` — SQLite (`~/.agx/traces.db`) for traces + YAML directory for vaccines
- In-memory fallback when `AGX_DATA_DIR=""` — zero filesystem writes, CI-safe
- Async API throughout (`aiosqlite`)

**Scanner**
- `agx scan --input logs.jsonl` — offline failure pattern detection (no LLM required)
- Five pattern detectors: schema violations, hallucination phrases, prompt injection patterns, loop detection (repeated 4-grams), refusals
- Optional LLM-enhanced scanning when `OPENAI_API_KEY` or `GROQ_API_KEY` is set
- `--dry-run` flag — print report without writing vaccine files
- `--exit-code` flag — exit non-zero if `BLOCK`-level violations detected (CI/CD gate)
- Heuristic confidence disclaimer printed on every scan (upsell to AG-X Cloud)

**CLI** (`agx` entry point)
- `agx init` — create `~/.agx/` directory structure + sample vaccine
- `agx scan` — analyze log files for failure patterns
- `agx validate` — run cage assertions against a sample output string
- `agx serve` — start local dashboard
- `agx list-vaccines` — show all active vaccines
- `agx runs` — show recent traces from SQLite

**Local dashboard** (`pip install agx-community[dashboard]`)
- FastAPI + Jinja2 + Alpine.js + TailwindCSS CDN — no Node.js / build step required
- `/` — runs list with live SSE updates
- `/runs/<id>` — trace detail (input, output, cage verdict, vaccines fired)
- `/vaccines` — active vaccines loaded from YAML
- `/scanner` — upload logs, view scan results in browser
- "Push to AG-X Cloud" upsell card when `AGX_ENDPOINT` is not set

**OpenTelemetry** (`pip install agx-community[otel]`)
- `agx.setup_otel()` — configure OTLP gRPC exporter
- `gen_ai.*` + `agx.*` semantic convention attributes on every span
- `tg.source = "agx-community"` — distinguishes community spans from cloud spans in mixed fleets
- Graceful fallback to `ConsoleSpanExporter` if OTLP endpoint is unreachable

**Upgrade bridge**
- Set `AGX_ENDPOINT` + `AGX_API_KEY` → `@agx.protect` automatically routes to AG-X Cloud; zero code change required

**Package**
- Installable as `pip install agx-community` (core) or with extras: `[otel]`, `[dashboard]`, `[scan]`, `[all]`
- Python 3.10+ supported
- Apache 2.0 license
- PEP 561 `py.typed` marker — full type-checker support

---

[0.1.0]: https://github.com/agx-community/agx-community/releases/tag/v0.1.0
