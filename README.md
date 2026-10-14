# uncorrelated-trading

Advisory bot: finds mutually uncorrelated energy, metals, agriculture and materials equities,
scores their momentum, and emails a daily brief with an `.xlsx` attachment. It never places
orders; positions are entered and exited by hand.

Status: under construction. Full setup, account walkthroughs, commands and scheduling notes
will be written in a later task. Design: `docs/superpowers/specs/2026-10-14-uncorrelated-trading-design.md`.

Quick start (development):

```bash
uv sync --all-extras
cp .env.example .env   # then fill in your own keys; .env is git-ignored
uv run pytest -q
```
