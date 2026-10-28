"""
The vulnerability scan: grype against a build's syft document in the publish job of every build, reduced to the
form record.json stores (one row per vulnerability, package and version, the database date and grype's version),
and the one shaping of those scans for readers: `document`, a build's `vulns.json` on its repository's Pages
(CONVENTIONS §6, "Repo Pages"), and `summary_of`, the counts the hub keeps for its builds list. The scan is the
state of the image as it was released: nothing rescans it later.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Protocol

Json = dict[str, Any]

SEVERITIES = ("critical", "high", "medium", "low", "negligible", "unknown")
FIX_STATES = ("fixed", "not-fixed", "wont-fix", "unknown")


class ScanError(Exception):
    pass


class Scanner(Protocol):
    def version(self) -> str | None:
        """grype's version, or None when it cannot run."""

    def db_date(self, *, update: bool) -> str | None:
        """When the vulnerability database was built (ISO 8601), updating it first when asked; None without one."""

    def scan(self, sbom: Path) -> Json:
        """grype's JSON report for a syft document."""


class Grype:
    """The real binary. `GRYPE` names it (default: `grype` on PATH); the database lives where grype keeps it."""

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or os.environ.get("GRYPE") or "grype"
        self.env = {**os.environ, "GRYPE_CHECK_FOR_APP_UPDATE": "false"}
        self.errors: list[str] = []
        """Why a database update failed, for the caller to report."""

    def _run(self, *args: str, check: bool = True, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args], capture_output=True, text=True, check=check, env={**self.env, **env}, timeout=1800
        )

    def version(self) -> str | None:
        try:
            out = json.loads(self._run("version", "-o", "json").stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        version = out.get("version")
        return str(version) if version else None

    def db_date(self, *, update: bool) -> str | None:
        """Updates when asked, or when there is no usable database at all (a runner without the cache)."""
        if update:
            self._update()
        built = self._status()
        if built is None and not update:
            self._update()
            built = self._status()
        return built

    def _update(self) -> None:
        try:
            self._run("db", "update")
        except (OSError, subprocess.SubprocessError) as e:
            self.errors.append(f"grype db update failed: {stderr(e)}")

    def _status(self) -> str | None:
        try:
            status = json.loads(self._run("db", "status", "-o", "json").stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        built = status.get("built")
        return str(built) if built and status.get("valid", True) else None

    def scan(self, sbom: Path) -> Json:
        try:
            # The database is updated by db_date(); a scan must not change it halfway through a run.
            out = self._run(f"sbom:{sbom}", "-o", "json", "-q", GRYPE_DB_AUTO_UPDATE="false").stdout
            report: Json = json.loads(out)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            raise ScanError(stderr(e)) from e
        return report


def stderr(e: BaseException) -> str:
    err = getattr(e, "stderr", None)
    return str(err).strip() if err else str(e)


def _severity(value: Any) -> str:
    text = str(value or "").lower()
    return text if text in SEVERITIES else "unknown"


def _fix_state(value: Any) -> str:
    text = str(value or "").lower()
    return text if text in FIX_STATES else "unknown"


def _cvss(entries: Any) -> float | None:
    scores = [e.get("metrics", {}).get("baseScore") for e in entries or [] if isinstance(e, dict)]
    numbers = [float(s) for s in scores if isinstance(s, int | float)]
    return max(numbers) if numbers else None


def db_date_of(report: Json) -> str | None:
    """`descriptor.db.status.built` (grype >= 0.87) or `descriptor.db.built` (older)."""
    db = report.get("descriptor", {}).get("db") or {}
    built = (db.get("status") or {}).get("built") or db.get("built")
    return str(built) if built else None


def compact(report: Json, db_date: str | None = None, scanned_at: str | None = None) -> Json:
    """
    The stored form of a grype report: one row per vulnerability, package and version (syft lists a package once
    per location, grype matches each), the database date, grype's version and, when given, when the scan ran.
    """
    matches: dict[tuple[str, str, str], Json] = {}
    for m in report.get("matches", []):
        v, a = m.get("vulnerability", {}), m.get("artifact", {})
        fix = v.get("fix") or {}
        urls = [v["dataSource"]] if v.get("dataSource") else []
        urls += [u for u in v.get("urls") or [] if u not in urls]
        package, version, vuln_id = str(a.get("name", "")), str(a.get("version", "")), str(v.get("id", ""))
        row: Json = {
            "package": package,
            "version": version,
            "type": str(a.get("type", "unknown")),
            "id": vuln_id,
            "severity": _severity(v.get("severity")),
            "fix_state": _fix_state(fix.get("state")),
            "fixed_in": [str(x) for x in fix.get("versions") or []],
            "urls": urls,
            "cvss": _cvss(v.get("cvss")),
        }
        matches.setdefault((vuln_id, package, version), row)
    out: Json = {
        "db_date": db_date or db_date_of(report) or "",
        "grype_version": str(report.get("descriptor", {}).get("version", "")),
        "matches": sort_matches(list(matches.values())),
    }
    if scanned_at:
        out["scanned_at"] = scanned_at
    return out


def sort_matches(matches: list[Json]) -> list[Json]:
    return sorted(matches, key=lambda m: (SEVERITIES.index(m["severity"]), m["package"].casefold(), m["id"]))


CAVEATS = [
    "This is the scan made when the image was built (grype against the build's syft SBOM, with the vulnerability "
    "database of that day), not an assessment, and it is not updated: the same image scanned today may show "
    "findings learned about since.",
    "Distribution packages (apk, deb) are matched against the distribution's own security feed, which knows about "
    'backported fixes: a fix state of "fixed" means the distribution ships a fixed package version, "not-fixed" '
    "means no fix exists upstream for that distribution release.",
    "Images are rebuilt weekly, so a distribution fix reaches a later build within a week of the distribution "
    'shipping it; a "fixed" finding means the fix was not in the image yet when it was built.',
    "Unfixable findings are not a reason to avoid an image: every image on the same base shares them, and only the "
    "distribution can resolve them.",
    "A finding against a package does not mean the vulnerable code path is reachable in this image.",
]
"""How to read a scan; every vulns.json carries them, and readers show them."""


def summarise(matches: list[Json]) -> Json:
    """Counts per severity and fix state. `unfixed` is everything that is neither fixed nor won't-fix."""
    summary = dict.fromkeys(SEVERITIES, 0) | {"fixed": 0, "unfixed": 0, "wont_fix": 0}
    for m in matches:
        summary[_severity(m.get("severity"))] += 1
        state = m.get("fix_state")
        summary["fixed" if state == "fixed" else "wont_fix" if state == "wont-fix" else "unfixed"] += 1
    return summary


def merge(scans: dict[str, Json]) -> list[Json]:
    """
    One row per vulnerability and package across architectures (the first version seen stands), each carrying the
    `arches` it was found on. Summaries count these rows, per architecture too, so the numbers add up.
    """
    merged: dict[tuple[str, str], Json] = {}
    for arch in sorted(scans):
        for m in scans[arch].get("matches", []):
            key = (m["id"], m["package"])
            if key in merged:
                if arch not in merged[key]["arches"]:
                    merged[key]["arches"].append(arch)
            else:
                merged[key] = {**m, "arches": [arch]}
    return sort_matches(list(merged.values()))


def summary_of(scans: dict[str, Json]) -> Json:
    """
    The counts of a build's scans (record.json `vulns`, per architecture) and when they were made: `summary` over the
    merged rows, `by_arch`, `db_date` and `grype_version` (of the newest database among the arches) and `scanned_at`
    (the earliest scan, when the scans say).
    """
    newest = max(scans.values(), key=lambda s: str(s.get("db_date", "")))
    scanned = sorted(str(s["scanned_at"]) for s in scans.values() if s.get("scanned_at"))
    out: Json = {
        "summary": summarise(merge(scans)),
        "by_arch": {arch: summarise(merge({arch: scans[arch]})) for arch in sorted(scans)},
        "db_date": str(newest.get("db_date", "")),
        "grype_version": str(newest.get("grype_version", "")),
    }
    if scanned:
        out["scanned_at"] = scanned[0]
    return out


def document(image: str, tag: str, scans: dict[str, Json]) -> Json:
    """
    A build's `vulns.json`: its scans of every architecture merged into one list, worst first, with the summaries,
    the database date, grype's version, when it was scanned and the caveats. Each match is the stored row plus the
    `arches` it was found on.
    """
    head = summary_of(scans)
    return {
        "image": image,
        "tag": tag,
        "summary": head["summary"],
        "by_arch": head["by_arch"],
        "matches": merge(scans),
        "db_date": head["db_date"],
        "grype_version": head["grype_version"],
        **({"scanned_at": head["scanned_at"]} if "scanned_at" in head else {}),
        "caveats": list(CAVEATS),
    }
