"""
`builder check`: everything a pull request to an image repository must satisfy. Each finding carries the file,
the field (a JSON path for manifest problems) and, when known, the line.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .manifest import Manifest, ManifestError, Settings, load_manifest, validate_manifest
from .render import OUT_DIR, drift, render_all

S6_DIR = Path("root/etc/s6-overlay/s6-rc.d")
S6_SCRIPTS = ("run", "finish", "check")
S6_HEADER = ("#!/usr/bin/with-contenv bash", "# shellcheck shell=bash")

CREDENTIAL_PATTERNS = {
    "GitHub token": re.compile(r"ghp_[A-Za-z0-9]{36}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
SKIP_DIRS = {".git", OUT_DIR, "node_modules", "__pycache__", ".venv"}


@dataclass
class Finding:
    level: str  # "error" | "warning"
    file: str
    field: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        where = f" (line {self.line})" if self.line else ""
        prefix = "warning: " if self.level == "warning" else ""
        return f"{prefix}{self.file}: {self.field}: {self.message}{where}"


def _error(file: str, field: str, message: str, line: int | None = None) -> Finding:
    return Finding("error", file, field, message, line)


def _warn(file: str, field: str, message: str, line: int | None = None) -> Finding:
    return Finding("warning", file, field, message, line)


def check_repo(repo_dir: Path, settings: Settings | None = None) -> list[Finding]:
    settings = settings or Settings.from_env()
    findings: list[Finding] = []
    manifest_path = repo_dir / "image.yml"
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        return [_error(exc.file, exc.field, exc.message, exc.line)]
    problems = validate_manifest(manifest)
    if problems:
        return [_error(p.file, p.field, p.message, p.line) for p in problems]

    files = render_all(manifest.data, settings)
    for rel, state in drift(repo_dir, files):
        findings.append(_error(rel, "<file>", f"{state} (run builder render)"))
    findings.extend(check_dockerfiles(repo_dir, manifest))
    findings.extend(check_s6_scripts(repo_dir))
    findings.extend(check_changelog(manifest))
    findings.extend(check_branch(repo_dir, manifest))
    findings.extend(check_credentials(repo_dir))
    return findings


def check_dockerfiles(repo_dir: Path, manifest: Manifest) -> list[Finding]:
    dockerfiles = manifest.data["build"].get("dockerfiles", {})
    names = {
        "amd64": dockerfiles.get("amd64", "Dockerfile"),
        "arm64": dockerfiles.get("arm64", "Dockerfile.aarch64"),
    }
    platforms = manifest.data["build"]["platforms"]
    findings = []
    present: dict[str, list[str]] = {}
    for arch, rel in names.items():
        if arch not in platforms:
            continue
        path = repo_dir / rel
        if not path.is_file():
            findings.append(_error(rel, "<file>", f"missing (build.platforms names {arch})"))
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        present[rel] = [line for line in lines if not line.startswith("FROM ")]
        if not any(line.startswith("FROM ") for line in lines):
            findings.append(_error(rel, "FROM", "no FROM line"))
    if len(present) == 2:
        a, b = present.values()
        if a != b:
            first = next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
            findings.append(
                _warn(" and ".join(present), "<file>", f"non-FROM lines differ (first at non-FROM line {first + 1})")
            )
    return findings


def check_s6_scripts(repo_dir: Path) -> list[Finding]:
    findings: list[Finding] = []
    base = repo_dir / S6_DIR
    if not base.is_dir():
        return findings
    for path in sorted(base.rglob("*")):
        if path.name not in S6_SCRIPTS or not path.is_file():
            continue
        rel = str(path.relative_to(repo_dir))
        if not path.stat().st_mode & stat.S_IXUSR:
            findings.append(_error(rel, "mode", "not executable"))
        if path.name == "check":
            # s6-notifyoncheck readiness probes are plain scripts (often #!/bin/bash); only the bit matters.
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, expected in enumerate(S6_HEADER):
            actual = lines[index] if index < len(lines) else ""
            if actual.strip() != expected:
                findings.append(_error(rel, f"line {index + 1}", f"expected {expected!r}, found {actual!r}", index + 1))
    return findings


def check_changelog(manifest: Manifest) -> list[Finding]:
    entries = manifest.data.get("changelog", [])
    findings = []
    for index in range(1, len(entries)):
        if entries[index]["date"] > entries[index - 1]["date"]:
            path = ("changelog", index, "date")
            findings.append(
                _error(
                    str(manifest.path),
                    "/".join(str(p) for p in path),
                    f"{entries[index]['date']} is newer than the entry above it; changelog is newest first",
                    manifest.line_of(path),
                )
            )
    return findings


def current_branch(repo_dir: Path) -> str | None:
    """The checked-out branch, or None outside a git checkout / on a detached HEAD."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def check_branch(repo_dir: Path, manifest: Manifest) -> list[Finding]:
    branch = current_branch(repo_dir)
    if branch is None:
        return []
    release = manifest.data["release"]
    if branch == release["branch"]:
        return []
    live = {t["tag"] for t in release.get("tags", [])}
    file, field, line = str(manifest.path), "release/branch", manifest.line_of(("release", "branch"))
    if branch in live:
        return [
            _error(
                file, field, f"manifest says {release['branch']!r} but the live branch {branch!r} is checked out", line
            )
        ]
    return [
        _warn(
            file,
            field,
            f"checked out branch {branch!r} is not the manifest branch {release['branch']!r} (dev build)",
            line,
        )
    ]


def check_credentials(repo_dir: Path) -> list[Finding]:
    findings = []
    for path in sorted(repo_dir.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(repo_dir).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in CREDENTIAL_PATTERNS.items():
                if pattern.search(line):
                    findings.append(
                        _error(str(path.relative_to(repo_dir)), "<content>", f"looks like a {label}", number)
                    )
    return findings
