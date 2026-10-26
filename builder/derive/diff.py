"""
What changed between a build and the previous build of the same branch: packages per architecture grouped
by ecosystem, container config, base digest, sizes, the Dockerfile as a unified diff, and a one-word
classification of why the image changed. Pure functions over bundle records and normalised package tables.
"""

from __future__ import annotations

import difflib
from typing import Any

from .packages import compare_versions

Json = dict[str, Any]
Packages = dict[str, list[Json]]
"""Normalised package rows per architecture."""


def _env_map(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        name, _, value = item.partition("=")
        out[name] = value
    return out


def _list_diff(old: list[str] | None, new: list[str] | None) -> Json:
    before, after = set(old or []), set(new or [])
    return {"added": sorted(after - before), "removed": sorted(before - after)}


def _env_diff(old: list[str] | None, new: list[str] | None) -> Json:
    before, after = _env_map(old), _env_map(new)
    common = sorted(before.keys() & after.keys())
    changed = [{"name": n, "from": before[n], "to": after[n]} for n in common if before[n] != after[n]]
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    return {"added": added, "removed": removed, "changed": changed}


def _sizes(bundle: Json) -> dict[str, int | None]:
    return {p["arch"]: p.get("size") for p in bundle.get("platforms", []) if p.get("result") == "success"}


def _size_diff(old: Json | None, new: Json) -> dict[str, Json]:
    before = _sizes(old) if old else {}
    after = _sizes(new)
    out: dict[str, Json] = {}
    for arch in sorted(before.keys() | after.keys()):
        a, b = before.get(arch), after.get(arch)
        out[arch] = {"from": a, "to": b, "delta": (b - a) if a is not None and b is not None else None}
    return out


def dockerfile_diff(old: str | None, new: str, name: str) -> str:
    if old is None or old == new:
        return ""
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"a/{name}", tofile=f"b/{name}"
    )
    text = "".join(lines)
    return text if text.endswith("\n") else text + "\n"


def _identity(row: Json) -> str:
    """A purl without its version, so an upgrade is one change rather than a removal and an addition."""
    purl = str(row.get("purl") or "")
    if purl:
        base, _, _ = purl.partition("?")
        if "@" in base.rsplit("/", 1)[-1]:
            base = base.rsplit("@", 1)[0]
        return base
    return f"{row['type']}:{row['name']}"


def package_diff(old: list[Json], new: list[Json]) -> dict[str, Json]:
    """Per ecosystem (syft type): added, removed, upgraded, downgraded. Ecosystems without changes are left out."""
    before = {_identity(r): r for r in old}
    after = {_identity(r): r for r in new}
    per_type: dict[str, Json] = {}

    def bucket(kind: str) -> Json:
        return per_type.setdefault(kind, {"added": [], "removed": [], "upgraded": [], "downgraded": []})

    for ident in sorted(after.keys() - before.keys()):
        row = after[ident]
        bucket(row["type"])["added"].append({"name": row["name"], "version": row["version"], "purl": row.get("purl")})
    for ident in sorted(before.keys() - after.keys()):
        row = before[ident]
        bucket(row["type"])["removed"].append({"name": row["name"], "version": row["version"], "purl": row.get("purl")})
    for ident in sorted(before.keys() & after.keys()):
        a, b = before[ident], after[ident]
        if a["version"] == b["version"]:
            continue
        direction = "upgraded" if compare_versions(a["version"], b["version"]) >= 0 else "downgraded"
        bucket(b["type"])[direction].append(
            {"name": b["name"], "from": a["version"], "to": b["version"], "purl": b.get("purl")}
        )
    return dict(sorted(per_type.items()))


def _totals(packages: dict[str, dict[str, Json]] | None) -> Json:
    totals = {"added": 0, "removed": 0, "upgraded": 0, "downgraded": 0}
    for per_type in (packages or {}).values():
        for changes in per_type.values():
            for kind in totals:
                totals[kind] += len(changes[kind])
    return totals


def classify(
    *,
    initial: bool,
    upstream_changed: bool,
    dockerfile_changed: bool,
    base_changed: bool,
    packages_changed: bool,
    reason: str,
) -> tuple[str, list[str]]:
    if initial:
        return "initial", ["first build of this branch in the hub"]
    reasons: list[str] = []
    kinds: list[str] = []
    if upstream_changed:
        kinds.append("upstream")
        reasons.append("upstream version changed")
    if dockerfile_changed:
        kinds.append("linuxserver")
        reasons.append("Dockerfile changed")
    elif reason == "push" and not upstream_changed and not base_changed:
        kinds.append("linuxserver")
        reasons.append("built from a push with the same upstream and base (root/ or build settings changed)")
    if base_changed:
        kinds.append("base")
        reasons.append("base image digest changed")
    if packages_changed:
        reasons.append("packages changed")
    if len(kinds) > 1:
        return "mixed", reasons
    if kinds:
        return kinds[0], reasons
    if packages_changed:
        return "packages", reasons
    return "none", reasons or ["nothing observable changed"]


def diff_builds(new: Json, old: Json | None, new_packages: Packages | None, old_packages: Packages | None) -> Json:
    """The diff record stored next to a build. `old` is None for the first build of a branch."""
    initial = old is None
    old_version = old["version"]["upstream"] if old else None
    upstream_changed = bool(old) and old_version != new["version"]["upstream"]
    old_base = old["base"] if old else None
    base_changed = bool(old) and (old_base or {}).get("digest") != new["base"].get("digest")
    dockerfiles = {
        arch: dockerfile_diff((old or {}).get("dockerfile", {}).get(arch), text, name)
        for arch, text in sorted(new.get("dockerfile", {}).items())
        for name in [("Dockerfile" if arch == "amd64" else "Dockerfile.aarch64")]
    }
    dockerfile_changed = any(dockerfiles.values())
    packages: dict[str, dict[str, Json]] | None = None
    if old is not None and new_packages is not None and old_packages is not None:
        packages = {}
        for arch in sorted(new_packages.keys() & old_packages.keys()):
            packages[arch] = package_diff(old_packages[arch], new_packages[arch])
    totals = _totals(packages)
    packages_changed = any(totals.values())
    classification, reasons = classify(
        initial=initial,
        upstream_changed=upstream_changed,
        dockerfile_changed=dockerfile_changed,
        base_changed=base_changed,
        packages_changed=packages_changed,
        reason=str(new.get("reason", "")),
    )
    old_config = (old or {}).get("config", {})
    new_config = new.get("config", {})
    return {
        "image": new["image"],
        "branch": new["branch"],
        "tag": new["version"]["tag"],
        "previous_tag": old["version"]["tag"] if old else None,
        "classification": classification,
        "reasons": reasons,
        "upstream": {"from": old_version, "to": new["version"]["upstream"], "changed": upstream_changed},
        "base": {"from": old_base, "to": new["base"], "changed": base_changed},
        "size": _size_diff(old, new),
        "config": {
            "env": _env_diff(old_config.get("env"), new_config.get("env")),
            "exposed_ports": _list_diff(old_config.get("exposed_ports"), new_config.get("exposed_ports")),
            "volumes": _list_diff(old_config.get("volumes"), new_config.get("volumes")),
        },
        "dockerfile": dockerfiles,
        "packages": packages,
        "totals": totals,
    }
