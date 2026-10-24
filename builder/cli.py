"""The ``builder`` console script: check, render, migrate, snippets."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .check import check_repo
from .manifest import ManifestError, Settings, load_valid
from .render import ALL_FILES, REPO_FILES, render_all, write_files


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="builder", description="Validate, render and migrate LinuxServer image manifests."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate image.yml and everything generated from it; exit 1 on failure")
    check.add_argument("repo_dir", nargs="?", default=".")

    render = sub.add_parser("render", help="write README.md, AGENTS.md, LICENSE and the build workflow")
    render.add_argument("repo_dir", nargs="?", default=".")
    render.add_argument("--only", help="comma separated list of generated paths to write", default=None)
    render.add_argument(
        "--all", action="store_true", help="also write the hub/docs extras into .builder-out/ (not for image repos)"
    )

    migrate = sub.add_parser("migrate", help="convert readme-vars.yml + jenkins-vars.yml into image.yml")
    migrate.add_argument("repo_dir", nargs="?", default=".")
    migrate.add_argument("--out", default="image.yml", help="output path relative to repo_dir ('-' for stdout)")

    snippets = sub.add_parser("snippets", help="print the compose or docker run snippet")
    snippets.add_argument("repo_dir", nargs="?", default=".")
    snippets.add_argument("--format", choices=["compose", "run", "json"], default="compose")
    snippets.add_argument("--required-only", action="store_true", help="omit optional parameters")
    return parser


def cmd_check(repo_dir: Path, settings: Settings) -> int:
    findings = check_repo(repo_dir, settings)
    for finding in findings:
        print(finding, file=sys.stderr if finding.level == "error" else sys.stdout)
    errors = [f for f in findings if f.level == "error"]
    if errors:
        print(f"{len(errors)} problem(s) found", file=sys.stderr)
        return 1
    print("ok")
    return 0


def cmd_render(repo_dir: Path, settings: Settings, only: str | None, everything: bool = False) -> int:
    manifest = load_valid(repo_dir / "image.yml")
    files = render_all(manifest.data, settings)
    # Image repos commit exactly REPO_FILES; the docs page and lite README are rendered by the hub, so
    # they are written only on request and never land in an image repository by accident.
    wanted: list[str] | None = None if everything else list(REPO_FILES)
    if only:
        wanted = [p.strip() for p in only.split(",") if p.strip()]
        unknown = [p for p in wanted if p not in ALL_FILES]
        if unknown:
            print(
                f"unknown generated file(s): {', '.join(unknown)}; choose from {', '.join(ALL_FILES)}", file=sys.stderr
            )
            return 2
    changed = write_files(repo_dir, files, wanted)
    for rel in changed:
        print(f"wrote {rel}")
    if not changed:
        print("nothing changed")
    return 0


def cmd_migrate(repo_dir: Path, out: str) -> int:
    from .migrate import migrate_repo

    text, todos = migrate_repo(repo_dir)
    if out == "-":
        sys.stdout.write(text)
    else:
        target = repo_dir / out
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target}")
    for todo in todos:
        print(f"TODO: {todo}", file=sys.stderr)
    return 0


def cmd_snippets(repo_dir: Path, settings: Settings, fmt: str, required_only: bool) -> int:
    from . import snippets

    manifest = load_valid(repo_dir / "image.yml")
    if fmt == "compose":
        sys.stdout.write(snippets.compose(manifest.data, settings, full=not required_only))
    elif fmt == "run":
        sys.stdout.write(snippets.docker_run(manifest.data, settings, full=not required_only))
    else:
        sys.stdout.write(snippets.as_json(manifest.data, settings))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_dir = Path(args.repo_dir).resolve()
    settings = Settings.from_env()
    try:
        if args.command == "check":
            return cmd_check(repo_dir, settings)
        if args.command == "render":
            return cmd_render(repo_dir, settings, args.only, args.all)
        if args.command == "migrate":
            return cmd_migrate(repo_dir, args.out)
        return cmd_snippets(repo_dir, settings, args.format, args.required_only)
    except ManifestError as exc:
        print(exc, file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"{exc.filename}: not found", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
