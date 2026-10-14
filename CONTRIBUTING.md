# Contributing

## Ground rules

- **No order placement.** The project is advisory only; pull requests adding order
  submission, modification or cancellation code are not accepted.
- **No secrets in git.** Keys live in `.env` (git-ignored). Tests use fakes, recorded
  fixtures under `tests/fixtures`, or the `integration` marker with keys from the environment.
- No new runtime dependencies without discussion; free data sources only.

## Setup

```bash
uv sync --all-extras
cp .env.example .env    # only needed for live runs and integration tests
```

## Workflow

1. Write a failing test first (unit, property or contract as appropriate), then the code.
2. Before pushing, run what CI runs:

   ```bash
   uv run ruff check .
   uv run ruff format --check .        # `uv run ruff format .` to fix
   uv run mypy src                     # strict mode
   uv run pytest -m "not integration" -q --cov --cov-fail-under=85
   ```

3. Optionally, with keys in the environment: `uv run pytest -m integration -q`.
4. Use conventional-commit subjects (`feat(scope): ...`, `fix: ...`, `docs: ...`,
   `test: ...`, `ci: ...`, `chore: ...`) and add a line to `CHANGELOG.md` under
   `[Unreleased]`.

## Code conventions

- Python 3.11+, full type hints, NumPy-style docstrings on every public function and class.
- Timezone-aware datetimes only (ruff `DTZ`); `pathlib` instead of `os.path` (ruff `PTH`).
- No `print` in `src/`; use `logging`. Command output in `cli.py` goes through `_out`.
- Domain records are the frozen dataclasses in `models.py`; errors derive from
  `errors.UncorrelatedTradingError`.
- Jobs receive their collaborators through `app.Container`; only `app.build_container`
  constructs concrete adapters. Tests inject fakes and the frozen clock from
  `tests/conftest.py`.
- Unit, property, contract and end-to-end tests must not touch the network (a fixture
  blocks sockets). Tests that need real APIs are marked `@pytest.mark.integration` and skip
  themselves when their keys are missing.

## Test layout

| Directory | Contents |
|---|---|
| `tests/unit` | one test module per source module, fakes and frozen clock |
| `tests/property` | Hypothesis properties (analysis invariants, as-of, rate limiter, store, workbook) |
| `tests/contract` | adapters against recorded JSON responses and error mapping |
| `tests/e2e` | `trader daily --offline --dry-run` on CSV fixtures |
| `tests/integration` | real Alpaca and Twelve Data calls (marker `integration`) |
| `tests/golden` | expected report output |
