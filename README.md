# builder

The contracts and the manifest tooling for the LinuxServer v2 playground.

- `CONVENTIONS.md` is the written contract every repository in `linuxserver-playground` follows: repo
  layout, branches, versions and tags, namespaces, labels, the build bundle, release notes, commit and PR
  rules, secrets, events.
- `schemas/` are the machine-checkable contracts: `image.schema.json` (the `image.yml` manifest every image
  repo carries), `mod.schema.json` (`mod.yml` on every docker-mods branch), `bundle.schema.json` (the
  `build.json` record attached to every release), `record.schema.json` (the `record.json` the build derives for
  the hub), `test-report.schema.json` (what the ci harness writes) and `control-event.schema.json` (what the
  workflow tells the control plane).
- `contracts/` is a small Python package that validates data against those schemas, with a CLI.
- `builder/` is the `builder` command line tool: it validates an image repository, renders every generated
  file from `image.yml`, migrates legacy `readme-vars.yml` + `jenkins-vars.yml` pairs and derives a build's
  `record.json`.
- `templates/agents/` are the AGENTS.md templates rendered into image repos and mod branches.
- `examples/` are manifests converted from live repos; the tests keep them valid and rendering.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
python -m contracts image examples/*/image.yml
ruff check . && ruff format --check . && mypy
```

## The `builder` command

Run it from an image repository (or pass the directory). Every command exits 1 on failure and prints the
file, the field (a JSON path into `image.yml`) and the line when it knows it.

| Command | What it does |
| --- | --- |
| `builder check [REPO_DIR]` | Validates `image.yml`, renders every generated file in memory and reports `path: differs (run builder render)` for each one that drifted. Also checks that both Dockerfiles exist and agree apart from `FROM` (warning), that every s6 `run`/`finish`/`check` is executable and starts with `#!/usr/bin/with-contenv bash` + `# shellcheck shell=bash`, that the changelog is newest first, that `release.branch` matches the checked-out branch (error on another live branch, warning on a dev branch) and that nothing looks like a credential. |
| `builder render [REPO_DIR] [--only README.md,...] [--all]` | Writes `README.md`, `AGENTS.md`, `LICENSE` and `.github/workflows/build.yml`. With `--all` it also writes the mkdocs page `docs.md` and `README.lite.md` into `.builder-out/` (the hub does this; image repos never commit them). Prints what changed. |
| `builder migrate [REPO_DIR] [--out image.yml]` | Reads `readme-vars.yml` and `jenkins-vars.yml`, resolves their Jinja placeholders with the vendored legacy variables and writes `image.yml`. Typed version and notes sources are recognised from the common `custom_version_command` shapes; anything else becomes a `command` source with a `# TODO`. Unmapped keys are printed to stderr as TODOs. `--out -` prints to stdout. |
| `builder render-mod [DIR] [--check]` | On a docker-mods branch: writes `README.md`, `AGENTS.md` and `.github/workflows/build.yml` from `mod.yml`; `--check` only reports the files that differ (exit 1). |
| `builder snippets [REPO_DIR] --format compose\|run\|json [--required-only]` | The docker compose / docker run snippet (or a JSON record with both variants and the normalised runtime parameters). The same functions live in `builder.snippets` for the hub to import. |
| `builder record OUT_DIR [--previous DIR] [--no-scan] [--no-base]` | Writes `OUT_DIR/record.json` from a build's bundle files (CONVENTIONS §6): package tables, the diff against the previous release in `--previous` (its `build.json` and `record.json` or syft documents), grype's scan of each architecture and the base image's layer digests. Runs in the shared workflow's publish job; the hub imports the same code from `builder.derive`. |

The links baked into the generated files come from `builder.manifest.Settings`; each field can be overridden
with an environment variable (`BUILDER_CONTROL_URL`, `BUILDER_SITE_URL`, `BUILDER_DOCS_URL`, `BUILDER_HUB_URL`, `BUILDER_ORG`,
`BUILDER_REGISTRY`). The CI badge is served by the control plane at `{control_url}/badge/{name}/{tag}.svg`.

### As a container

The repository builds an internal tool image; `check` and `render` run against the repository mounted at
`/repo`:

```bash
docker run --rm -v "$PWD":/repo ghcr.io/linuxserver-playground/builder:latest check
docker run --rm -v "$PWD":/repo --user "$(id -u):$(id -g)" ghcr.io/linuxserver-playground/builder:latest render
docker run --rm -v "$PWD":/repo --user "$(id -u):$(id -g)" ghcr.io/linuxserver-playground/builder:latest migrate
```

Pass `--user` to the commands that write so the generated files are owned by you. Build the image locally
with `docker build -t builder:local .` (`Dockerfile.aarch64` is the arm64 twin).

### Migration notes

`builder migrate` maps the legacy variables as follows. Markdown text (`project_blurb`, `app_setup_block`,
`optional_parameters`, `*_message`, custom readmes) is copied byte for byte.

| Legacy | `image.yml` |
| --- | --- |
| `external_type` + `repo_vars` (`EXT_*`, `JSON_*`, `DIST_*`) | typed `upstream.version` (`github_release`, `github_commit`, `gitlab_commit`, `alpine_apk`, `json`, `deb_repo`, `blob`, `npm`, `pip`, `os`) |
| `custom_version_command` | `github_release` (+ `transform` for `sed 's\|^v\|\|'`, `awk -F`, `cut -c`), `codeberg_release`, `deb_packages` (a `Packages` index grep), `json` (`curl \| jq`), else `command` verbatim with a TODO |
| `custom_release_notes_command` | `github_release` / `codeberg_release` with a `tag` template when it is a plain `releases/tags/<tag> \| jq .body`; otherwise `command` verbatim with a TODO |
| `release_type`, `release_tag`, `ls_branch`, `development_versions_items` | `release.channel`, `release.tag`, `release.branch`, `release.tags` |
| `DIST_IMAGE`, `BUILD_VERSION_ARG`, `available_architectures`, the Dockerfile `FROM` | `build.base` (`-nginx`, selkies, kasmvnc detected from the base image), `build.version_arg`, `build.platforms`; `build.first_party: false` for `baseimage-*` |
| `CI`, `CI_WEB`, `CI_PORT`, `CI_SSL`, `CI_WEBPATH`, `CI_AUTH`, `CI_DELAY`, `CI_DOCKERENV`, `CI_WEB_SCREENSHOT_DELAY` | `test.enabled`, `test.web.*`, `test.startup_timeout`, `test.env` |
| `param_*` / `opt_param_*`, `custom_params`, `cap_add_*`, `security_opt_*`, `param_usage_include_net` | `runtime.*` (required first, optional entries carry `optional: true`), `env_options` → `options`, `name` → `name`/`title` |
| `readonly_supported/message`, `nonroot_supported/message` | `runtime.readonly`, `runtime.nonroot` |
| `app_setup_block`, `optional_parameters`, `readme_*`, `kasm_blurb`, `selkies_blurb`, `external_application_*`, `full_custom_readme` | `docs.setup`, `docs.optional_parameters`, `docs.notices`, `docs.external_application`, `docs.custom_readme` |
| `project_categories` CSV, `project_logo` | `docs.categories`, `docs.logo` |
| `changelogs` `DD.MM.YY:` | `changelog` with `YYYY-MM-DD` |
| `sponsor_links` | `upstream.sponsor` |
| `unraid_template: false`, `unraid_requirement` | `publish.stores.unraid: false`, `publish.unraid.requirement` |
| `CI_WEB` + `CI_PORT` | `proxy.port`/`scheme` with `subfolder.supported: false` and a TODO |

Dropped on purpose (reported as TODOs): `init_diagram` (generated by the build), the derived URL variables,
armhf/riscv64 build flags, the Jenkins trigger settings and `unraid_template_sync`.

## Design notes

The manifest replaces `readme-vars.yml` and `jenkins-vars.yml`. Its sections are organised by concern:
`upstream` (where the version and release notes come from), `release` (what this branch publishes),
`build`, `test`, `runtime` (every container parameter, typed), `docs`, `proxy` (SWAG samples), `publish`
(stores, Kubernetes) and `changelog`. Version sources are typed; the shell `command` source is an escape
hatch, not the default, because the scheduler and the build must resolve the same version the same way.

The README templates in `builder/templates/` port the legacy README snippets section by section, with the
Jenkins, GitLab, Quay, Docker Hub and Scarf badges replaced by the GitHub release, GHCR package, docs page
and control-plane CI badges. Admonitions are written in the manifest as `?+note[Title](|text|)` and render
as GitHub alerts in `README.md` and as mkdocs admonitions in the docs page.
