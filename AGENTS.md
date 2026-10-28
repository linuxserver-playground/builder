# AGENTS.md - builder

## Purpose

This repo defines the contracts for every other repository in `linuxserver-playground` and holds the `builder`
command line tool that validates image repositories, renders their generated files and migrates legacy
manifests. Changes here affect every image repo, the workflows, the ci harness, the hub and the control plane.
Treat a schema change as an API change and a template change as a change to every README in the fleet.

## Layout

| Path | What |
| --- | --- |
| `CONVENTIONS.md` | The written contract. Read it first. |
| `schemas/*.json` | JSON Schema 2020-12. `$id` is the canonical URL; cross-references use bare filenames. |
| `contracts/` | Validator package and `python -m contracts` CLI. |
| `builder/` | The `builder` CLI: `cli.py` (argparse), `manifest.py` (loading with line numbers, validation, the render context), `render.py`, `check.py`, `snippets.py` (compose/run builders the hub imports), `admonition.py`, `migrate.py` + `legacy_vars.py` (readme-vars/jenkins-vars conversion). |
| `builder/derive/` | Per-build data, derived in the build: `packages` (syft to the package table), `diff` (against the previous release), `vulns` (grype, the stored form of its report, and `document`, the shape of each build's `vulns.json` on its repository's Pages, which the pages assembler in workflows and the hub's counts share), `registry` (a base image's layer digests), `layers` (which layers came from the base, which Dockerfile lines made the others), `documents` (the per-build documents, `report.json`, `tests.json`, `layers.<arch>.json`, `packages.<arch>.json`: the hub renders its routes with them and the pages assembler in workflows writes copies on each repository's Pages; `parts` gathers their inputs from a release's assets), `record` (`builder record`, which writes the release's `record.json`). The hub imports the same modules. |
| `builder/templates/` | Jinja templates for `README.md`, the mkdocs page, `README.lite.md`, the build workflow; `parts/` holds the README sections ported from the legacy snippets. `LICENSE` is copied verbatim. |
| `templates/agents/` | AGENTS.md templates rendered into image repos and mod branches. |
| `examples/` | Converted manifests; tests keep them valid and rendering. Add one when a schema gains a feature. |
| `tests/` | pytest. `tests/golden/` are snapshot renders of the sonarr example; `tests/fixtures/live/` are real readme-vars/jenkins-vars pairs used by the migration tests. |
| `Dockerfile`, `Dockerfile.aarch64`, `entrypoint.sh` | The internal tool image (`ghcr.io/linuxserver-playground/builder`). |

## What you may edit

Everything, by pull request. A schema change needs: the schema, an example exercising it, a test, a line
in `CONVENTIONS.md` if behaviour changes, and a `contracts_version` bump in `pyproject.toml` (MINOR for
additive, MAJOR for breaking; MAJOR also changes each schema's `schema` const).

A template change changes every generated README in the fleet; regenerate the goldens with
`UPDATE_GOLDEN=1 pytest tests/test_render.py` and review the diff of `tests/golden/` before committing.
Keep wording from the legacy snippets where it is still true.

## How to validate

```bash
pip install -e '.[dev]'
ruff check . && ruff format --check . && mypy && pytest
python -m contracts image examples/*/image.yml
builder render examples/sonarr   # writes into the example dir; do not commit the output
docker build -t builder:local . && docker run --rm -v "$PWD/examples/sonarr":/repo builder:local check
```

## Contracts

Produces all six schemas. Consumers: `workflows` (bundle, control-event, image, record through `builder record`, the per-build documents through `builder.derive.documents`),
`ci` (test-report), `hub` (image, mod, bundle, test-report, record, `builder.snippets` and `builder.derive`), `control` (control-event), every `docker-*`
repo (image, through `builder check` in its build), `docker-mods` (mod).

## Pull request rules

Disclose AI use. One change per PR. Commit subjects are short imperative sentences without prefixes,
emoji or trailers. Do not commit generated output (`.builder-out/`, rendered example files) or credentials.
