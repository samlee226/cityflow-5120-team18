# CityFlow Data Pipeline

This directory contains the initial Data Science workspace for the FIT5120 CityFlow project. It currently provides project structure only; data-source integrations, database operations, analytics, and AI functionality will be added in later iterations.

## Structure

- `src/cityflow_pipeline/`: pipeline package and stage modules
- `tests/`: automated tests
- `notebooks/`: exploratory notebooks
- `data/raw/`: local source data, ignored by Git
- `data/interim/`: local intermediate data, ignored by Git
- `data/processed/`: local generated outputs, ignored by Git
- `data/sample/`: small, non-sensitive sample data that may be committed

## Local setup

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
pytest
```

Keep real data and secrets out of version control.
