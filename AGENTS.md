# AGENTS.md - builder

## Purpose

This repo defines the contracts for every other repository in `linuxserver-playground` and will hold the
manifest renderer. Changes here affect every image repo, the workflows, the ci harness, the hub and the
control plane. Treat a schema change as an API change.

## Layout

| Path | What |
| --- | --- |
| `CONVENTIONS.md` | The written contract. Read it first. |
| `schemas/*.json` | JSON Schema 2020-12. `$id` is the canonical URL; cross-references use bare filenames. |
| `contracts/` | Validator package and `python -m contracts` CLI. |
| `templates/agents/` | AGENTS.md templates rendered into image repos and mod branches. |
| `examples/` | Converted manifests; tests keep them valid. Add one when a schema gains a feature. |
| `tests/` | pytest. |

## What you may edit

Everything, by pull request. A schema change needs: the schema, an example exercising it, a test, a line
in `CONVENTIONS.md` if behaviour changes, and a `contracts_version` bump in `pyproject.toml` (MINOR for
additive, MAJOR for breaking; MAJOR also changes each schema's `schema` const).

## How to validate

```bash
pip install -e '.[dev]'
ruff check . && ruff format --check . && mypy contracts tests && pytest
python -m contracts image examples/*/image.yml
```

## Contracts

Produces all five schemas. Consumers: `workflows` (bundle, control-event, image), `ci` (test-report),
`hub` (image, mod, bundle, test-report), `control` (control-event), every `docker-*` repo (image),
`docker-mods` (mod).

## Pull request rules

Disclose AI use. One change per PR. Commit subjects are short imperative sentences without prefixes,
emoji or trailers. Do not commit generated output or credentials.
