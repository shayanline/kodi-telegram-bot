# Agent Guidelines — Kodi Telegram Bot

## Project Overview

Python 3.11+ async Telegram bot (Telethon) that downloads media and plays it
on Kodi via JSON-RPC. No database, no external state — single process,
in-memory only. Designed to be tiny, readable, and Raspberry Pi friendly.

### Architecture

```
main.py            → startup, graceful shutdown
config.py          → env loading & validation
utils.py           → media detection, disk/memory helpers
kodi.py            → thin JSON-RPC wrapper (notify / play / status / controls / volume / input)
organizer.py       → filename parsing, categorization & final path builder
filemanager.py     → interactive file browser & deletion via Telegram
kodiremote.py      → Kodi remote control UI via inline buttons (/kodi)
kodirestart.py     → /restart_kodi command with confirmation prompt
logger.py          → truncating file logger with size cap
downloader/
   queue.py        → concurrency + FIFO queue worker
   state.py        → DownloadState, message tracking
   buttons.py      → inline keyboard builder
   progress.py     → rate-limited progress callback factory
   manager.py      → orchestration: handlers, retries, success/error flows
   ids.py          → stable short file identifiers for callback data
   list_commands.py→ /downloads and /queue command handlers
tests/             → pytest test suite
```

## Code Principles

These are non-negotiable. Every change must follow them:

1. **Remove dead code.** Never leave unused imports, unreachable branches,
   commented-out code, or unused variables. If code is not called, delete it.

2. **Avoid complex logic.** Prefer flat over nested. If a function needs more
   than two levels of indentation, refactor it. Break complex conditions into
   well-named helper functions or early returns.

3. **Avoid defensive programming and unnecessary checks.** Do not add
   redundant `None` checks, type guards, or try/except blocks "just in case."
   Only handle errors at real boundaries (I/O, network, user input). Trust
   the internal API contracts.

4. **Avoid over-engineering.** No abstractions until they are needed in at
   least two places. No wrapper classes for single-use patterns. No
   configuration for things that don't change.

5. **Less code is better.** Fewer lines, fewer files, fewer abstractions.
   The best code is code you don't have to write. Collapse duplicate branches,
   share logic, and delete anything that doesn't earn its keep.

6. **Cleaner code is better.** Readable names, consistent style, small
   functions with single responsibilities. Code should be obvious at a glance.

## Setup

After cloning, install dependencies and activate pre-commit hooks:

```bash
uv sync
uv run pre-commit install
```

## Formatting & Linting

This project uses **ruff** for both linting and formatting. Pre-commit hooks
run `ruff check --fix` and `ruff format` automatically on every commit, so
staged files are always linted and formatted before they land.

To run manually:

```bash
ruff check --fix .      # lint and auto-fix
ruff format .           # format
```

Both commands must pass with zero errors before committing.

### Configuration

Ruff is configured in `pyproject.toml`. Key rules enabled:
- `E`, `F`, `W` — pycodestyle + pyflakes essentials
- `I` — isort-compatible import sorting
- `UP` — pyupgrade modernizations
- `B` — bugbear (common pitfalls)
- `SIM` — simplification suggestions
- `RUF` — ruff-specific rules

Do not disable rules without a clear comment explaining why.

## Type Checking

```bash
mypy .
```

Configured in `pyproject.toml`. Strict optional enabled. Tests are exempt from
`disallow_untyped_defs`.

## Testing

This project uses **pytest** with **pytest-cov** for coverage.

### Run tests:

```bash
pytest --cov --cov-fail-under=80 -q
```

### Rules:

- **Coverage must be above 80%.** Every PR must maintain or improve coverage.
- Write tests for all new functionality. No exceptions.
- Tests live in `tests/` and follow the `test_<module>.py` naming pattern.
- Use `monkeypatch` for patching config and external dependencies.
- Keep tests fast — no real network calls, no real filesystem side effects
  (use `tempfile.TemporaryDirectory` and mocks).
- Prefer one clear assertion per test over a mega-test that checks everything.

## Verification Checklist

Run these before every commit, in order:

```bash
ruff check --fix .
ruff format .
mypy .
pytest --cov --cov-fail-under=80 -q
```

All four must pass cleanly.

## Git Conventions

- Write concise commit messages focused on **why**, not what.
- Do not commit secrets, `.env` files, or session files.
- Keep commits small and atomic — one logical change per commit.

## Dependencies

This project uses **uv** as the package manager. Dependencies are declared in
`pyproject.toml` and locked in `uv.lock`.

### Common commands:

```bash
uv sync                    # install all deps (including dev)
uv add <package>           # add a runtime dependency
uv add --dev <package>     # add a dev dependency
uv remove <package>        # remove a dependency
```

- Keep dependencies minimal. Check `pyproject.toml` before adding anything.
- Prefer stdlib solutions over third-party libraries when practical.
- Pin minimum versions, not exact versions (`>=` not `==`).
- Do **not** edit `requirements.txt` — it is no longer used.

## Style Notes

- Use `from __future__ import annotations` in all modules.
- Prefer dataclasses with `slots=True` for data containers.
- Use `__all__` exports in every module.
- Keep docstrings brief — one line when possible.
- Do not add comments that restate the code. Comments explain *why*, not *what*.
