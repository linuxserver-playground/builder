"""
`record.json`: everything the hub stores about one build, derived in the build's own publish job from the bundle
files it already has. The hub ingests a release by reading build.json and this file, instead of downloading the
package documents, layer lists and test reports and computing the diff and the vulnerability scan itself, so a
release reaches the website in the time of one small download. Schema: schemas/record.schema.json.

    builder record out/ [--previous prev/] [--no-scan] [--no-base]

`prev/` holds the previous release of the branch: its build.json, and its record.json or, for a release made
before records existed, its packages.<arch>.json.gz. A part that cannot be derived is left out and its reason
listed under `errors`; the hub derives what is missing itself.
"""

from __future__ import annotations

import datetime
import gzip
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import packages as pkg
from .diff import diff_builds
from .registry import base_layers
from .vulns import ScanError, Scanner, compact

Json = dict[str, Any]

RECORD_SCHEMA = 1
DB_MAX_AGE_HOURS = 30
"""An older vulnerability database (a cache restored from a previous day) is updated before the scan."""


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _age_hours(then: str, now: str) -> float:
    try:
        start = datetime.datetime.fromisoformat(then.replace("Z", "+00:00"))
        end = datetime.datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if start.tzinfo is None:
        start = start.replace(tzinfo=datetime.UTC)
    return (end - start).total_seconds() / 3600


def _json(path: Path) -> Any:
    if not path.is_file():
        return None
    if path.suffix == ".gz":
        return json.loads(gzip.decompress(path.read_bytes()))
    return json.loads(path.read_text(encoding="utf-8"))


def _rows(directory: Path, arch: str) -> list[Json] | None:
    syft = _json(directory / f"packages.{arch}.json.gz")
    return pkg.normalise(syft) if syft is not None else None


def previous_packages(previous: Path) -> dict[str, list[Json]] | None:
    """The previous release's package rows per architecture: from its record.json, else from its syft documents."""
    record = _json(previous / "record.json")
    if isinstance(record, dict) and record.get("packages"):
        return {arch: rows for arch, rows in record["packages"].items()}
    found = {arch: rows for arch in ("amd64", "arm64") if (rows := _rows(previous, arch)) is not None}
    return found or None


def scan(scanner: Scanner, out: Path, arches: list[str], now: Callable[[], str]) -> tuple[dict[str, Json], list[str]]:
    """grype against each architecture's syft document, both scans against one database."""
    version = scanner.version()
    if version is None:
        return {}, ["grype is not available; the hub scans this build instead"]
    db_date = scanner.db_date(update=False)
    if db_date is None or _age_hours(db_date, now()) > DB_MAX_AGE_HOURS:
        db_date = scanner.db_date(update=True)
    if db_date is None:
        return {}, [f"grype {version} has no vulnerability database; the hub scans this build instead"]
    scans: dict[str, Json] = {}
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="lsio-record-") as tmp:
        for arch in arches:
            source = out / f"packages.{arch}.json.gz"
            if not source.is_file():
                errors.append(f"vulns {arch}: no packages.{arch}.json.gz")
                continue
            path = Path(tmp) / f"packages.{arch}.json"
            path.write_bytes(gzip.decompress(source.read_bytes()))
            try:
                scans[arch] = compact(scanner.scan(path), db_date, now())
            except ScanError as e:
                errors.append(f"vulns {arch}: {e}")
    return scans, errors


def build_record(
    out: Path,
    *,
    previous: Path | None = None,
    scanner: Scanner | None = None,
    fetch_base: Callable[[str, str | None], Json] | None = base_layers,
    now: Callable[[], str] = _now,
) -> Json:
    """The record of the build whose bundle files are in `out` (build.json must be there)."""
    bundle = _json(out / "build.json")
    if not isinstance(bundle, dict):
        raise FileNotFoundError(f"{out / 'build.json'} is missing or not a JSON object")
    errors: list[str] = []
    arches = [p["arch"] for p in bundle["platforms"] if p.get("result") == "success"]

    packages = {arch: rows for arch in arches if (rows := _rows(out, arch)) is not None}
    layers = {arch: data for arch in arches if (data := _json(out / f"layers.{arch}.json")) is not None}
    tests = {
        p["arch"]: data for p in bundle["platforms"] if (data := _json(out / f"test.{p['arch']}.json")) is not None
    }
    errors += [f"packages {arch}: no packages.{arch}.json.gz" for arch in arches if arch not in packages]

    old = _json(previous / "build.json") if previous else None
    if previous and old is None:
        errors.append("diff: the previous release's build.json could not be read; diffed as the first build")
    old_packages = previous_packages(previous) if previous and old is not None else None
    diff = diff_builds(bundle, old, packages or None, old_packages)

    vulns: dict[str, Json] = {}
    if scanner is not None:
        vulns, scan_errors = scan(scanner, out, arches, now)
        errors += scan_errors

    base = None
    digest = (bundle.get("base") or {}).get("digest")
    if fetch_base is not None and digest:
        try:
            base = fetch_base(bundle["base"]["image"], digest)
        except Exception as e:
            errors.append(f"base {bundle['base']['image']}: {str(e).splitlines()[0] if str(e) else type(e).__name__}")

    record: Json = {
        "schema": RECORD_SCHEMA,
        "image": bundle["image"],
        "branch": bundle["branch"],
        "tag": bundle["version"]["tag"],
        "derived_at": now(),
        "previous_tag": old["version"]["tag"] if isinstance(old, dict) else None,
        "packages": packages,
        "layers": layers,
        "tests": tests,
        "diff": diff,
        "vulns": vulns,
        "base": base,
        "errors": errors,
    }
    return record


def write_record(out: Path, record: Json) -> Path:
    path = out / "record.json"
    path.write_text(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    return path
