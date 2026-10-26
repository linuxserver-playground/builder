"""
The normalised package table: syft JSON (packages.<arch>.json.gz, packages only since ci dropped file metadata;
older releases still carry file records) reduced to what the API serves and the diff compares: name, version,
type, purl, licenses. Everything else in the syft document (CPEs, locations, metadata) stays on the Release asset.
"""

from __future__ import annotations

import gzip
import json
import re
from typing import Any

Json = dict[str, Any]

_TOKEN = re.compile(r"(\d+|[A-Za-z]+)")


def _license_names(raw: Any) -> list[str]:
    """syft writes licenses as strings (old) or {value, spdxExpression, ...} objects (current)."""
    names: list[str] = []
    for item in raw or []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            value = item.get("spdxExpression") or item.get("value")
            if value:
                names.append(str(value))
    return sorted(set(names))


def normalise(syft: Json) -> list[Json]:
    """Package rows from a syft document, deduplicated by purl (or name/version/type), sorted by type then name."""
    rows: dict[tuple[str, ...], Json] = {}
    for artifact in syft.get("artifacts", []):
        name = str(artifact.get("name", ""))
        if not name:
            continue
        version = str(artifact.get("version", ""))
        kind = str(artifact.get("type", "unknown"))
        purl = artifact.get("purl") or None
        key = (purl,) if purl else (name, version, kind)
        rows.setdefault(key, {"name": name, "version": version, "type": kind, "purl": purl, "licenses": []})
        rows[key]["licenses"] = sorted(set(rows[key]["licenses"]) | set(_license_names(artifact.get("licenses"))))
    return sorted(rows.values(), key=lambda r: (r["type"], r["name"], r["version"]))


def normalise_gz(data: bytes) -> list[Json]:
    return normalise(json.loads(gzip.decompress(data)))


def table(record_name: str, branch: str, release_tag: str, tag: str, arch: str, packages: list[Json]) -> Json:
    """The file stored as packages[.<release tag>].<arch>.json."""
    return {
        "image": record_name,
        "branch": branch,
        "release_tag": release_tag,
        "tag": tag,
        "arch": arch,
        "count": len(packages),
        "packages": packages,
    }


def version_key(version: str) -> list[tuple[int, int | str]]:
    """
    A sort key good enough for apk, deb, pip and npm versions: numeric runs compare as numbers, letters as
    text (so 1.10 > 1.9, 3.22.1-r0 > 3.22.1, 2.0.0b1 < 2.0.0). Not a full implementation of any scheme.
    """
    key: list[tuple[int, int | str]] = []
    for token in _TOKEN.findall(version):
        key.append((1, int(token)) if token.isdigit() else (0, token.lower()))
    return key


def compare_versions(old: str, new: str) -> int:
    """-1 when new is older, 1 when newer, 0 when equal (or indistinguishable)."""
    if old == new:
        return 0
    a, b = version_key(old), version_key(new)
    if a == b:
        return 0
    return 1 if b > a else -1


def slug(name: str) -> str:
    """A filename-safe form of a package name: libstdc++ -> libstdc++ stays; @scope/pkg -> _scope_pkg."""
    cleaned = re.sub(r"[^A-Za-z0-9._+-]+", "_", name).strip("._")
    return cleaned or "_"
