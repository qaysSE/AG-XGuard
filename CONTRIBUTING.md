# Contributing to AG-X Community Edition

Thank you for your interest in contributing! This document covers everything you need to get started.

---

## Table of Contents

1. [Code of Conduct](#code-of-conduct)
2. [Getting Started](#getting-started)
3. [Development Setup](#development-setup)
4. [Running Tests](#running-tests)
5. [Submitting Changes](#submitting-changes)
6. [Code Style](#code-style)
7. [Issue Reporting](#issue-reporting)

---

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you agree to uphold its terms.

---

## Getting Started

1. Fork the repository on GitHub
2. Clone your fork:
   ```bash
   git clone https://github.com/<your-username>/agx-community.git
   cd agx-community
   ```
3. Create a feature branch:
   ```bash
   git checkout -b feature/your-feature-name
   ```

---

## Development Setup

**Requirements:** Python 3.10+, pip

```bash
# Install in editable mode with all extras + test dependencies
pip install -e ".[all]"
pip install pytest pytest-asyncio ruff

# Verify setup
agx --version
pytest tests/ -v
```

No Node.js, no Docker, no databases required. The entire SDK runs locally with SQLite.

---

## Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_cage.py -v

# Run with in-memory store (no ~/.agx writes)
AGX_DATA_DIR="" pytest tests/ -v

# Check types (optional, requires mypy)
pip install mypy
mypy agx/ --ignore-missing-imports
```

All 45 tests must pass before a PR can be merged.

---

## Submitting Changes

1. **Write tests** for any new behavior — tests live in `tests/`
2. **Run the full suite**: `pytest tests/ -v`
3. **Run the linter**: `ruff check agx/ && ruff format agx/ --check`
4. **Update `CHANGELOG.md`** with a concise entry under `[Unreleased]`
5. Open a pull request against `main` with a clear title and description

### PR Checklist

- [ ] Tests added / updated
- [ ] All 45+ tests pass (`pytest tests/ -v`)
- [ ] Linter clean (`ruff check agx/`)
- [ ] `CHANGELOG.md` updated
- [ ] Docstrings updated if public API changed

---

## Code Style

- **Formatter:** `ruff format` (Black-compatible)
- **Linter:** `ruff check` (E, F, W rules; E501 line-length ignored)
- **Types:** All public functions must have type annotations
- **Imports:** Standard library → third-party → local (one blank line between groups)
- **No print():** Use `logging.getLogger(__name__)` in library code; `rich.console.Console` in CLI code
- **No secrets:** Never commit API keys, tokens, or credentials

---

## Issue Reporting

Before opening an issue:
- Check [existing issues](https://github.com/agx-community/agx-community/issues) to avoid duplicates
- Include your Python version (`python3 --version`) and OS

For bugs, include a minimal reproducible example. For feature requests, explain the use-case before the proposed solution.

---

## Project Structure

```
agx/
├── __init__.py        # Public API surface
├── _config.py         # Settings (pydantic-settings, AGX_ env vars)
├── _models.py         # All Pydantic data models
├── cage.py            # DeterministicCage assertion runner
├── guard.py           # @agx.protect decorator
├── store.py           # LocalStore (SQLite + YAML)
├── _pipeline.py       # LocalPipeline (Phase B + B2)
├── otel.py            # OpenTelemetry hooks
├── cli.py             # Click CLI (agx command)
├── scanner/           # Offline log scanner
│   ├── analyzer.py
│   ├── heuristics.py
│   ├── yaml_exporter.py
│   └── llm_doctor.py  # Optional LLM enhancement
└── dashboard/         # FastAPI + Jinja2 local dashboard
    ├── server.py
    └── templates/
tests/
├── test_cage.py
├── test_guard.py
├── test_scanner.py
└── fixtures/
    └── sample_logs.jsonl
```
