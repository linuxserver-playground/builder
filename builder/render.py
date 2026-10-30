"""Render every generated file of an image repository from its manifest."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import admonition
from .manifest import Json, Settings, build_context
from .snippets import compose, docker_run

PACKAGE_TEMPLATES = Path(__file__).resolve().parent / "templates"
SHARED_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
OUT_DIR = ".builder-out"

# Files committed to the repo, in the order they are reported.
REPO_FILES = (
    "README.md",
    "AGENTS.md",
    "LICENSE",
    ".github/FUNDING.yml",
    "root/donate.txt",
    ".github/workflows/build.yml",
    ".github/workflows/pr-report.yml",
)
# Committed files that exist only when the manifest calls for them: rendered when it does, removed when it does not.
OPTIONAL_FILES = ("root/donate.txt",)
# Files only written to .builder-out/ (consumed by the hub and the docs site).
OUT_FILES = (f"{OUT_DIR}/docs.md", f"{OUT_DIR}/README.lite.md")
ALL_FILES = REPO_FILES + OUT_FILES


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader([str(PACKAGE_TEMPLATES), str(SHARED_TEMPLATES)]),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
        autoescape=False,
    )
    env.filters["admonition"] = admonition.admonition
    env.filters["noter"] = admonition.convert
    env.filters["yaml_string"] = _yaml_string
    return env


def render_all(data: Json, settings: Settings | None = None) -> dict[str, str]:
    """Every generated file as {relative path: content}."""
    settings = settings or Settings.from_env()
    ctx = build_context(data, settings)
    env = environment()
    ctx["compose_snippet"] = compose(ctx)
    ctx["run_snippet"] = docker_run(ctx)
    files = {
        "README.md": env.get_template("README.md.j2").render(ctx),
        "AGENTS.md": env.get_template("agents/image.md.j2").render(ctx),
        "LICENSE": (PACKAGE_TEMPLATES / "LICENSE").read_text(encoding="utf-8"),
        # The GitHub sponsor buttons: ours, then the upstream project's donation pages (up to 4 custom URLs).
        ".github/FUNDING.yml": env.get_template("FUNDING.yml.j2").render(ctx),
        # Printed by the base images' init-adduser banner, "To support the app dev(s) visit:".
        "root/donate.txt": env.get_template("donate.txt.j2").render(ctx) if ctx["upstream"].get("sponsor") else None,
        ".github/workflows/build.yml": env.get_template("build.yml.j2").render(ctx),
        ".github/workflows/pr-report.yml": env.get_template("pr-report.yml.j2").render(ctx),
        f"{OUT_DIR}/docs.md": env.get_template("docs.md.j2").render(ctx),
        f"{OUT_DIR}/README.lite.md": env.get_template("README.lite.md.j2").render(ctx),
    }
    return {path: _tidy(content) for path, content in files.items() if content is not None}


def _tidy(text: str) -> str:
    """Collapse runs of blank lines left behind by skipped sections; exactly one trailing newline."""
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.strip() == "" and out and out[-1].strip() == "":
            continue
        out.append(line)
    return "\n".join(out).strip("\n") + "\n"


def write_files(repo_dir: Path, files: dict[str, str], only: Iterable[str] | None = None) -> list[str]:
    """Write files that differ and remove optional files the manifest no longer calls for; returns the relative
    paths that changed."""
    wanted = set(only) if only is not None else set(files) | set(OPTIONAL_FILES)
    changed = []
    for rel, content in files.items():
        if rel not in wanted:
            continue
        target = repo_dir / rel
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        changed.append(rel)
    for rel in OPTIONAL_FILES:
        if rel in wanted and rel not in files and (repo_dir / rel).is_file():
            (repo_dir / rel).unlink()
            changed.append(rel)
    return changed


def drift(repo_dir: Path, files: dict[str, str]) -> list[tuple[str, str]]:
    """(path, 'differs'|'missing'|'stale') for every committed generated file that does not match; stale is an
    optional file the manifest no longer calls for."""
    out = []
    for rel in REPO_FILES:
        target = repo_dir / rel
        if rel not in files:
            if target.is_file():
                out.append((rel, "stale"))
        elif not target.is_file():
            out.append((rel, "missing"))
        elif target.read_text(encoding="utf-8") != files[rel]:
            out.append((rel, "differs"))
    return out
