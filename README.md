# builder

The contracts and the manifest tooling for the LinuxServer v2 playground.

- `CONVENTIONS.md` is the written contract every repository in `linuxserver-playground` follows: repo
  layout, branches, versions and tags, namespaces, labels, the build bundle, release notes, commit and PR
  rules, secrets, events.
- `schemas/` are the machine-checkable contracts: `image.schema.json` (the `image.yml` manifest every image
  repo carries), `mod.schema.json` (`mod.yml` on every docker-mods branch), `bundle.schema.json` (the
  `build.json` record attached to every release), `test-report.schema.json` (what the ci harness writes)
  and `control-event.schema.json` (what the workflow tells the control plane).
- `contracts/` is a small Python package that validates data against those schemas, with a CLI.
- `templates/agents/` are the AGENTS.md templates rendered into image repos and mod branches.
- `examples/` are manifests converted from live repos; the tests keep them valid.

The renderer (`builder check`, `builder render`, `builder migrate`) is the next increment and lives in this
repo too.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
python -m contracts image examples/*/image.yml
ruff check . && ruff format --check . && mypy contracts tests
```

## Design notes

The manifest replaces `readme-vars.yml` and `jenkins-vars.yml`. Its sections are organised by concern:
`upstream` (where the version and release notes come from), `release` (what this branch publishes),
`build`, `test`, `runtime` (every container parameter, typed), `docs`, `proxy` (SWAG samples), `publish`
(stores, Kubernetes) and `changelog`. Version sources are typed; the shell `command` source is an escape
hatch, not the default, because the scheduler and the build must resolve the same version the same way.
