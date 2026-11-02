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


def check_repo(repo_dir: Path, settings: Settings | None = None, drift_level: str = "error") -> list[Finding]:
    """Every problem in the repository. `drift_level` is the level of generated files that do not match a fresh
    render: an error on the live branch's own check, a warning for PR and branch builds, where the files are
    re-rendered by the live build after merge and drift only means the builder changed since."""
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
        findings.append(Finding(drift_level, rel, "<file>", f"{state} (run builder render)", None))
    findings.extend(check_dockerfiles(repo_dir, manifest))
    findings.extend(check_s6_scripts(repo_dir))
    findings.extend(check_kind(manifest))
    findings.extend(check_require_assets(manifest))
    findings.extend(check_changelog(manifest))
    findings.extend(check_branch(repo_dir, manifest))
    findings.extend(check_credentials(repo_dir))
    if settings.labs:
        findings.extend(check_labs(repo_dir, manifest, settings))
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


def check_kind(manifest: Manifest) -> list[Finding]:
    """The baseimage-* repos are the base images; the name and the kind must agree so no consumer needs both."""
    name = manifest.data["name"]
    kind = manifest.data.get("kind", "app")
    if name.startswith("baseimage-") == (kind == "base"):
        return []
    if kind == "base":
        message = "kind: base needs a baseimage-* name"
    else:
        message = "baseimage-* repos are base images; set kind: base"
    line = manifest.line_of(("kind",)) or manifest.line_of(("name",))
    return [_error(str(manifest.path), "kind", message, line)]


RELEASE_SOURCES = ("github_release", "codeberg_release", "gitea_release")
"""The version sources whose releases carry assets (workflows' resolver keeps the same list)."""


def check_require_assets(manifest: Manifest) -> list[Finding]:
    """An `{asset: <regex>}` entry reads the resolved release's asset list, so it needs a release source; the
    resolver would refuse it hourly otherwise. A bad regex is caught here rather than at the first check."""
    source = manifest.data.get("upstream", {}).get("version", {})
    entries = source.get("require_assets") or []
    findings: list[Finding] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        field = f"upstream.version.require_assets[{index}].asset"
        line = manifest.line_of(("upstream", "version", "require_assets", index, "asset"))
        if source.get("type") not in RELEASE_SOURCES:
            message = f"asset patterns need one of {', '.join(RELEASE_SOURCES)}, not {source.get('type')!r}"
            findings.append(_error(str(manifest.path), field, message, line))
        try:
            re.compile(str(entry["asset"]).replace("${VERSION}", "V").replace("$VERSION", "V"))
        except re.error as exc:
            findings.append(_error(str(manifest.path), field, f"not a valid regex: {exc}", line))
    return findings


def check_labs(repo_dir: Path, manifest: Manifest, settings: Settings) -> list[Finding]:
    """The Labs profile (workspace docs/labs.md, "The bar"): what of the bar a repository can show offline. Labs takes
    less proven software, so the rules the main fleet leaves to review are errors here."""
    data = manifest.data
    path = str(manifest.path)
    findings: list[Finding] = []
    if not data.get("maintainers"):
        findings.append(
            _error(path, "maintainers", "Labs images name their maintainers (GitHub logins)", manifest.line_of(()))
        )
    kind = data.get("kind", "app")
    if kind != "app":
        findings.append(
            _error(path, "kind", f"Labs takes applications only, not kind: {kind}", manifest.line_of(("kind",)))
        )
    source = data["upstream"].get("version", {})
    source_type = source.get("type")
    version_line = manifest.line_of(("upstream", "version", "type"))
    if source_type == "command":
        message = "Labs images follow stable releases through a typed version source, not a command"
        findings.append(_error(path, "upstream.version.type", message, version_line))
    if source.get("prerelease"):
        line = manifest.line_of(("upstream", "version", "prerelease"))
        findings.append(_error(path, "upstream.version.prerelease", "Labs images follow stable releases only", line))
    if source_type in RELEASE_SOURCES and not source.get("require_assets"):
        message = "name what the Dockerfiles download from the release (an asset pattern, or the source archive URL)"
        findings.append(_error(path, "upstream.version.require_assets", message, version_line))
    findings.extend(check_labs_base(repo_dir, manifest, settings))
    return findings


def check_labs_base(repo_dir: Path, manifest: Manifest, settings: Settings) -> list[Finding]:
    """The image's own stage (the last FROM) starts from one of the playground's base images."""
    dockerfiles = manifest.data["build"].get("dockerfiles", {})
    defaults = {"amd64": "Dockerfile", "arm64": "Dockerfile.aarch64"}
    prefix = f"ghcr.io/{settings.pipeline_org}/baseimage-"
    findings: list[Finding] = []
    for arch in manifest.data["build"]["platforms"]:
        rel = dockerfiles.get(arch, defaults[arch])
        file = repo_dir / rel
        if not file.is_file():
            continue  # check_dockerfiles reports it
        froms = [
            (number, line.split()[1:])
            for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1)
            if line.startswith("FROM ")
        ]
        if not froms:
            continue
        number, words = froms[-1]
        words = [w for w in words if not w.startswith("--")]
        if not words or not words[0].startswith(prefix):
            found = words[0] if words else ""
            message = f"Labs images build on the playground's base images ({prefix}*), not {found!r}"
            findings.append(_error(rel, "FROM", message, number))
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
        # root/defaults ships sample configs (dns plugin ini files with placeholder keys); not secrets.
        if path.relative_to(repo_dir).parts[:2] == ("root", "defaults"):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if "example" in line.lower():
                continue
            for label, pattern in CREDENTIAL_PATTERNS.items():
                if pattern.search(line):
                    findings.append(
                        _error(str(path.relative_to(repo_dir)), "<content>", f"looks like a {label}", number)
                    )
    return findings
