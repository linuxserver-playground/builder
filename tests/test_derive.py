"""builder.derive: the record a build attaches to its release, the registry lookup and the scan's stored form."""

from __future__ import annotations

import copy
import gzip
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from builder.cli import main
from builder.derive import record as record_module
from builder.derive.diff import diff_builds
from builder.derive.packages import normalise
from builder.derive.registry import base_layers, parse_reference
from builder.derive.vulns import CAVEATS, ScanError, compact, document, merge, summarise, summary_of
from contracts import validate

from .test_contracts import _bundle

Json = dict[str, Any]
NOW = "2026-10-26T12:00:00Z"
GRYPE_REPORT: Json = {
    "descriptor": {"version": "0.121.0", "db": {"status": {"built": "2026-10-26T05:00:00Z"}}},
    "matches": [
        {
            "vulnerability": {
                "id": "CVE-2026-1",
                "severity": "High",
                "fix": {"state": "fixed", "versions": ["8.9.2-r0"]},
                "dataSource": "https://security.alpinelinux.org/vuln/CVE-2026-1",
                "cvss": [{"metrics": {"baseScore": 7.5}}],
            },
            "artifact": {"name": "curl", "version": "8.9.1-r0", "type": "apk"},
        },
        {  # the same finding at a second location: one row
            "vulnerability": {"id": "CVE-2026-1", "severity": "High", "fix": {"state": "fixed"}},
            "artifact": {"name": "curl", "version": "8.9.1-r0", "type": "apk"},
        },
    ],
}


def syft(*packages: tuple[str, str]) -> bytes:
    artifacts = [{"name": n, "version": v, "type": "apk", "purl": f"pkg:apk/alpine/{n}@{v}"} for n, v in packages]
    return gzip.compress(json.dumps({"artifacts": artifacts}).encode())


class FakeGrype:
    def __init__(self, db_date: str | None = "2026-10-26T05:00:00Z", fail: bool = False) -> None:
        self.dates = [db_date]
        self.fail = fail
        self.updates = 0
        self.errors: list[str] = []

    def version(self) -> str | None:
        return "0.121.0"

    def db_date(self, *, update: bool) -> str | None:
        if update:
            self.updates += 1
            return "2026-10-26T05:00:00Z"
        return self.dates[0]

    def scan(self, sbom: Path) -> Json:
        if self.fail:
            raise ScanError("boom")
        assert json.loads(sbom.read_text())["artifacts"]
        return GRYPE_REPORT


def write_build(directory: Path, bundle: Json, packages: bytes | None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "build.json").write_text(json.dumps(bundle))
    if packages is not None:
        (directory / "packages.amd64.json.gz").write_bytes(packages)


@pytest.fixture
def build(tmp_path: Path) -> tuple[Path, Path]:
    previous = _bundle()
    current = copy.deepcopy(previous)
    current["version"].update(upstream="4.0.21.1", upstream_clean="4.0.21.1", tag="4.0.21.1-ls2", ls_build=2)
    out, prev = tmp_path / "out", tmp_path / "prev"
    write_build(out, current, syft(("curl", "8.9.1-r0"), ("krb5-libs", "1.21")))
    (out / "layers.amd64.json").write_text(json.dumps([{"index": 0, "digest": "sha256:" + "1" * 64, "size": 5}]))
    (out / "test.amd64.json").write_text(json.dumps({"schema": 1, "success": True, "tests": []}))
    write_build(prev, previous, syft(("curl", "8.9.0-r0")))
    return out, prev


def test_record_has_everything_the_hub_derives(build: tuple[Path, Path]) -> None:
    out, prev = build
    fetched: list[tuple[str, str | None]] = []

    def fetch_base(image: str, digest: str | None) -> Json:
        fetched.append((image, digest))
        return {"image": image, "digest": digest, "platforms": {"amd64": {"digest": None, "layers": ["sha256:1"]}}}

    rec = record_module.build_record(out, previous=prev, scanner=FakeGrype(), fetch_base=fetch_base, now=lambda: NOW)
    validate("record", rec)
    assert rec["tag"] == "4.0.21.1-ls2" and rec["previous_tag"] == "4.0.20.3014-ls1"
    assert [p["name"] for p in rec["packages"]["amd64"]] == ["curl", "krb5-libs"]
    assert rec["diff"]["classification"] == "upstream"
    assert rec["diff"]["packages"]["amd64"]["apk"]["added"][0]["name"] == "krb5-libs"
    assert rec["diff"]["packages"]["amd64"]["apk"]["upgraded"][0]["to"] == "8.9.1-r0"
    assert rec["layers"]["amd64"][0]["index"] == 0 and rec["tests"]["amd64"]["success"] is True
    scan = rec["vulns"]["amd64"]
    assert scan["db_date"] == "2026-10-26T05:00:00Z" and scan["scanned_at"] == NOW and len(scan["matches"]) == 1
    assert rec["base"]["platforms"]["amd64"]["layers"] == ["sha256:1"]
    assert fetched == [("ghcr.io/linuxserver/baseimage-alpine:3.24", "sha256:" + "b" * 64)]
    assert rec["errors"] == []


def test_the_diff_is_the_one_the_hub_computes(build: tuple[Path, Path]) -> None:
    out, prev = build
    rec = record_module.build_record(out, previous=prev, fetch_base=None, now=lambda: NOW)
    new, old = json.loads((out / "build.json").read_text()), json.loads((prev / "build.json").read_text())
    rows = {"amd64": normalise(json.loads(gzip.decompress((out / "packages.amd64.json.gz").read_bytes())))}
    old_rows = {"amd64": normalise(json.loads(gzip.decompress((prev / "packages.amd64.json.gz").read_bytes())))}
    assert rec["diff"] == diff_builds(new, old, rows, old_rows)
    assert rec["vulns"] == {} and rec["base"] is None


def test_previous_packages_come_from_its_record_first(build: tuple[Path, Path]) -> None:
    out, prev = build
    (prev / "packages.amd64.json.gz").unlink()
    rows = [{"name": "curl", "version": "8.9.1-r0", "type": "apk", "purl": "pkg:apk/alpine/curl@8.9.1-r0"}]
    (prev / "record.json").write_text(json.dumps({"packages": {"amd64": [{**rows[0], "licenses": []}]}}))
    rec = record_module.build_record(out, previous=prev, fetch_base=None, now=lambda: NOW)
    assert rec["diff"]["packages"]["amd64"]["apk"]["added"][0]["name"] == "krb5-libs"
    assert (
        "upgraded" not in rec["diff"]["packages"]["amd64"]["apk"]
        or not rec["diff"]["packages"]["amd64"]["apk"]["upgraded"]
    )


def test_first_build_and_failures_are_recorded_not_fatal(build: tuple[Path, Path]) -> None:
    out, _ = build

    def broken(image: str, digest: str | None) -> Json:
        raise urllib.error.URLError("no route")

    rec = record_module.build_record(out, scanner=FakeGrype(fail=True), fetch_base=broken, now=lambda: NOW)
    validate("record", rec)
    assert rec["previous_tag"] is None and rec["diff"]["classification"] == "initial"
    assert rec["vulns"] == {} and rec["base"] is None
    assert rec["errors"] == [
        "vulns amd64: boom",
        "base ghcr.io/linuxserver/baseimage-alpine:3.24: <urlopen error no route>",
    ]


def test_a_stale_database_is_updated_first(build: tuple[Path, Path]) -> None:
    out, _ = build
    grype = FakeGrype(db_date="2026-10-20T05:00:00Z")
    rec = record_module.build_record(out, scanner=grype, fetch_base=None, now=lambda: NOW)
    assert grype.updates == 1 and rec["vulns"]["amd64"]["db_date"] == "2026-10-26T05:00:00Z"


def test_cli_writes_a_valid_record(build: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    out, prev = build
    assert main(["record", str(out), "--previous", str(prev), "--no-scan", "--no-base"]) == 0
    rec = json.loads((out / "record.json").read_text())
    validate("record", rec)
    assert "diff upstream against 4.0.20.3014-ls1" in capsys.readouterr().out


def test_compact_merges_locations_and_keeps_the_scan_time() -> None:
    scan = compact(GRYPE_REPORT, scanned_at=NOW)
    assert scan["grype_version"] == "0.121.0" and scan["scanned_at"] == NOW
    assert scan["matches"] == [
        {
            "package": "curl",
            "version": "8.9.1-r0",
            "type": "apk",
            "id": "CVE-2026-1",
            "severity": "high",
            "fix_state": "fixed",
            "fixed_in": ["8.9.2-r0"],
            "urls": ["https://security.alpinelinux.org/vuln/CVE-2026-1"],
            "cvss": 7.5,
        }
    ]


def _row(package: str, vuln: str, severity: str, fix_state: str, version: str = "1.0") -> Json:
    return {
        "package": package,
        "version": version,
        "type": "apk",
        "id": vuln,
        "severity": severity,
        "fix_state": fix_state,
        "fixed_in": [],
        "urls": [],
        "cvss": None,
    }


SCANS: dict[str, Json] = {
    "amd64": {
        "db_date": "2026-10-26T05:00:00Z",
        "grype_version": "0.121.0",
        "scanned_at": "2026-10-26T12:00:05Z",
        "matches": [
            _row("zlib1g", "CVE-2023-45853", "critical", "wont-fix"),
            _row("curl", "CVE-2026-2", "high", "fixed"),
            _row("coreutils", "CVE-2016-2781", "medium", "unknown"),
        ],
    },
    "arm64": {
        "db_date": "2026-10-26T05:00:00Z",
        "grype_version": "0.121.0",
        "scanned_at": "2026-10-26T12:00:00Z",
        "matches": [
            _row("curl", "CVE-2026-2", "high", "fixed", version="1.1"),
            _row("coreutils", "CVE-2016-2781", "medium", "not-fixed"),
        ],
    },
}


def test_merge_keeps_one_row_per_vulnerability_and_package_with_its_arches() -> None:
    merged = merge(SCANS)
    assert [(m["package"], m["arches"]) for m in merged] == [
        ("zlib1g", ["amd64"]),
        ("curl", ["amd64", "arm64"]),
        ("coreutils", ["amd64", "arm64"]),
    ]
    assert merged[1]["version"] == "1.0"  # the first architecture's version stands
    assert summarise(merged) == {
        "critical": 1,
        "high": 1,
        "medium": 1,
        "low": 0,
        "negligible": 0,
        "unknown": 0,
        "fixed": 1,
        "unfixed": 1,
        "wont_fix": 1,
    }
    assert "arches" not in SCANS["amd64"]["matches"][0]  # the input is left alone


def test_document_is_the_served_shape_and_the_summary_its_head() -> None:
    doc = document("sonarr", "4.0.20.3014-ls4", SCANS)
    assert list(doc) == [
        "image",
        "tag",
        "summary",
        "by_arch",
        "matches",
        "db_date",
        "grype_version",
        "scanned_at",
        "caveats",
    ]
    assert doc["image"] == "sonarr" and doc["tag"] == "4.0.20.3014-ls4"
    assert doc["by_arch"]["arm64"]["critical"] == 0 and doc["by_arch"]["amd64"] == doc["summary"]
    assert doc["scanned_at"] == "2026-10-26T12:00:00Z" and doc["db_date"] == "2026-10-26T05:00:00Z"
    assert doc["caveats"] == CAVEATS and all(c.endswith(".") for c in CAVEATS)
    assert not any("rescan" in c for c in CAVEATS)
    head = summary_of(SCANS)
    assert head == {k: doc[k] for k in ("summary", "by_arch", "db_date", "grype_version", "scanned_at")}
    # A scan without its time (an older record) still shapes; the time is left out.
    bare = {"amd64": {k: v for k, v in SCANS["amd64"].items() if k != "scanned_at"}}
    assert "scanned_at" not in document("sonarr", "x", bare)


def test_document_from_a_real_record_scan(build: tuple[Path, Path]) -> None:
    out, prev = build
    rec = record_module.build_record(out, previous=prev, scanner=FakeGrype(), fetch_base=None, now=lambda: NOW)
    doc = document(rec["image"], rec["tag"], rec["vulns"])
    assert doc["summary"]["high"] == 1 and doc["matches"][0]["arches"] == sorted(rec["vulns"])
    assert json.loads(json.dumps(doc)) == doc


def test_parse_reference() -> None:
    assert parse_reference("ghcr.io/linuxserver/baseimage-alpine:3.24") == (
        "ghcr.io",
        "linuxserver/baseimage-alpine",
        "3.24",
    )
    assert parse_reference("docker.io/alpine:3.24") == ("registry-1.docker.io", "library/alpine", "3.24")
    assert parse_reference("alpine") == ("registry-1.docker.io", "library/alpine", "latest")


def test_base_layers_reads_an_index() -> None:
    calls: list[str] = []

    def get(url: str, params: dict[str, str], headers: dict[str, str]) -> Json:
        calls.append(url)
        if url.endswith("/token"):
            assert params == {"scope": "repository:linuxserver/baseimage-alpine:pull"}
            return {"token": "t"}
        assert headers["Authorization"] == "Bearer t"
        if url.endswith("/manifests/sha256:index"):
            return {
                "manifests": [
                    {"digest": "sha256:a", "platform": {"os": "linux", "architecture": "amd64"}},
                    {"digest": "sha256:att", "platform": {"os": "unknown", "architecture": "unknown"}},
                ]
            }
        return {"layers": [{"digest": "sha256:l1"}]}

    base = base_layers("ghcr.io/linuxserver/baseimage-alpine:3.24", "sha256:index", get)
    assert base["platforms"] == {"amd64": {"digest": "sha256:a", "layers": ["sha256:l1"]}}
    assert calls[0] == "https://ghcr.io/token"


# ------------------------------------------------------------------------------------------ commits between builds


def _compare_commit(sha: str, subject: str, name: str, login: str) -> Json:
    return {
        "sha": sha,
        "html_url": f"https://github.com/o/docker-x/commit/{sha}",
        "commit": {"message": f"{subject}\n\nlonger body", "author": {"name": name, "date": "2026-10-30T19:36:42Z"}},
        "author": {"login": login},
    }


HUMAN = _compare_commit("a" * 40, "Add the upstream donation links", "thelamer", "thelamer")
BOT = _compare_commit("b" * 40, "Update generated files [skip ci]", "github-actions[bot]", "github-actions[bot]")
TABLE = _compare_commit("c" * 40, "Update package versions [skip ci]", "github-actions[bot]", "github-actions[bot]")
COMPARE: Json = {
    "total_commits": 2,
    "commits": [HUMAN, BOT],
    "files": [{"filename": f} for f in ("image.yml", "README.md", ".github/FUNDING.yml", "root/donate.txt")],
}


def test_commit_summary_lists_commits_areas_and_bots() -> None:
    from builder.derive import commits

    out = commits.summarise(COMPARE)
    assert [c["subject"] for c in out["commits"]] == [
        "Add the upstream donation links",
        "Update generated files [skip ci]",
    ]
    assert [c["bot"] for c in out["commits"]] == [False, True]
    assert out["commits"][0]["author"] == {"name": "thelamer", "login": "thelamer"}
    assert out["count"] == 2 and out["truncated"] is False
    # root/donate.txt is a generated file, not root/
    assert out["areas"] == {"manifest": 1, "generated": 3}
    assert commits.changes_image(out) is True
    span = f"{'a' * 7} → {'b' * 7}"
    assert commits.describe(out, "a" * 40, "b" * 40) == f"2 commits ({span}): changed image.yml, generated files"


def test_commit_summary_knows_the_pipelines_own_table_update() -> None:
    from builder.derive import commits

    table = {"total_commits": 1, "commits": [TABLE], "files": [{"filename": "package_versions.txt"}]}
    out = commits.summarise(table)
    assert out["areas"] == {"packages": 1} and commits.changes_image(out) is False
    line = commits.describe(out, "1" * 40, "2" * 40)
    assert line == "1 commit (1111111 → 2222222): the pipeline's own package table update"
    assert commits.area_of("Dockerfile.aarch64") == "dockerfile"
    assert commits.area_of("root/etc/cont-init.d/10-x") == "root"
    assert commits.area_of("docs/x.md") == "other"
    truncated = commits.summarise({"total_commits": 300, "commits": table["commits"], "files": []})
    assert truncated["truncated"] is True and commits.changes_image(truncated) is None
    assert commits.changes_image(None) is None


def test_diff_reads_the_commits_when_the_compare_is_at_hand(build: tuple[Path, Path]) -> None:
    out, prev = build
    new, old = json.loads((out / "build.json").read_text()), json.loads((prev / "build.json").read_text())
    rows = {"amd64": normalise(json.loads(gzip.decompress((out / "packages.amd64.json.gz").read_bytes())))}
    # Same upstream, base and Dockerfile, a push whose commits only touched the README: not a LinuxServer change.
    same = copy.deepcopy(new)
    same["version"]["upstream"] = old["version"]["upstream"]
    same["reason"] = "push"
    same["commit"] = "f" * 40
    compare = {"total_commits": 1, "commits": [HUMAN], "files": [{"filename": "README.md"}]}
    diff = diff_builds(same, old, rows, rows, compare)
    assert diff["commit"]["commits"][0]["subject"] == "Add the upstream donation links"
    assert diff["commit"]["areas"] == {"generated": 1} and diff["commit"]["count"] == 1
    assert diff["classification"] == "none"
    assert diff["reasons"] == [f"1 commit ({old['commit'][:7]} → {'f' * 7}): changed generated files"]
    # Without the compare the push is read as a LinuxServer change, as before.
    blind = diff_builds(same, old, rows, rows)
    assert blind["classification"] == "linuxserver" and "commits" not in blind["commit"]
    # The record reads prev/compare.json by default and validates with it.
    (out / "build.json").write_text(json.dumps(new | {"commit": "b" * 40}))
    (prev / "compare.json").write_text(json.dumps(COMPARE))
    rec = record_module.build_record(out, previous=prev, fetch_base=None, now=lambda: NOW)
    validate("record", rec)
    assert rec["diff"]["commit"]["count"] == 2 and rec["diff"]["classification"] == "mixed"
    missing = record_module.build_record(
        out, previous=prev, compare=out / "nope.json", fetch_base=None, now=lambda: NOW
    )
    assert "commits" not in missing["diff"]["commit"]
    assert any(e.startswith("commits:") for e in missing["errors"])
