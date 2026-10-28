"""builder.derive.documents: each build's documents on its repository's Pages, from a record or from the assets."""

from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
from typing import Any

from builder.derive import documents as docs
from builder.derive.diff import diff_builds
from builder.derive.packages import normalise

Json = dict[str, Any]
RELEASE = Path(__file__).parent / "fixtures" / "release"
BUILD: Json = json.loads((RELEASE / "build.json").read_text())
LAYERS: list[Json] = json.loads((RELEASE / "layers.amd64.json").read_text())
REPORT: Json = json.loads((RELEASE / "test.amd64.json").read_text())
PAGES = "https://linuxserver-playground.github.io/docker-sonarr/builds/4.0.20.3014-ls1/"
SITE = "https://linuxserver-playground.github.io/site/"
BASE = {
    "image": BUILD["base"]["image"],
    "digest": BUILD["base"]["digest"],
    "platforms": {"amd64": {"digest": "sha256:x", "layers": [layer["digest"] for layer in LAYERS[:7]]}},
}


def syft(*packages: tuple[str, str]) -> bytes:
    artifacts = [{"name": n, "version": v, "type": "apk", "purl": f"pkg:apk/alpine/{n}@{v}"} for n, v in packages]
    return gzip.compress(json.dumps({"artifacts": artifacts}).encode())


def test_links_follow_the_assets_and_point_at_pages() -> None:
    links = docs.links(BUILD, PAGES, SITE, tests=True, vulns=False)
    assert links["build"] == f"{PAGES}build.json" and links["report"] == f"{PAGES}report.json"
    assert links["layers"] == {"amd64": f"{PAGES}layers.amd64.json", "arm64": f"{PAGES}layers.arm64.json"}
    assert links["packages"]["arm64"] == f"{PAGES}packages.arm64.json"
    assert links["sbom"]["amd64"] == f"{PAGES}packages.amd64.json.gz"
    assert links["files"]["amd64"] == f"{PAGES}files.amd64.json.gz"
    assert links["tests"] == f"{PAGES}tests.json" and "vulns" not in links
    assert links["release"] == "https://github.com/linuxserver-playground/docker-sonarr/releases/tag/4.0.20.3014-ls1"
    assert links["run_json"].endswith("/releases/download/4.0.20.3014-ls1/run.json")
    # The website's page, not the bundle's own report_url (older bundles point at control or the hub).
    assert links["test_report"] == f"{SITE}images/sonarr/builds/4.0.20.3014-ls1/"
    one_arch = copy.deepcopy(BUILD)
    one_arch["assets"] = [a for a in one_arch["assets"] if "arm64" not in a["name"]]
    assert docs.links(one_arch, PAGES, SITE)["layers"] == {"amd64": f"{PAGES}layers.amd64.json"}


def test_tests_document_summarises_each_report() -> None:
    doc = docs.tests(BUILD, {"amd64": REPORT}, PAGES, SITE)
    assert doc is not None and doc["enabled"] is True and doc["success"] is True
    assert doc["report_url"] == f"{SITE}images/sonarr/builds/4.0.20.3014-ls1/"
    assert doc["reports"]["amd64"] == REPORT
    summary = doc["summary"]["amd64"]
    assert (summary["passed"], summary["failed"], summary["total"]) == (10, 0, 10) and summary["failed_tests"] == []
    # The harness's retired html report is not a report file; the others are on Pages under their fixed names.
    assert summary["files"] == {
        "browser_log": f"{PAGES}amd64/browser.log",
        "container_log": f"{PAGES}amd64/container.log",
        "screenshot": f"{PAGES}amd64/screenshot.png",
        "sysinfo": f"{PAGES}amd64/sysinfo.txt",
    }
    assert summary["tarball"].endswith("/releases/download/4.0.20.3014-ls1/test.amd64.tar.gz")
    assert docs.tests(BUILD, {}, PAGES, SITE) is None
    disabled = copy.deepcopy(BUILD)
    disabled["test"] = {"enabled": False, "success": True}
    assert "success" not in (docs.tests(disabled, {"amd64": REPORT}, PAGES, SITE) or {})


def test_layers_and_packages_documents() -> None:
    doc = docs.layers(BUILD, "amd64", LAYERS, BASE)
    assert (doc["image"], doc["tag"], doc["arch"]) == ("sonarr", "4.0.20.3014-ls1", "amd64")
    assert doc["base_layers"] == 7 and doc["from_base_source"] == "registry"
    assert [layer.get("dockerfile_lines") for layer in doc["layers"]][-2:] == [[21, 45], [48, 48]]
    bare = docs.layers(BUILD, "amd64", LAYERS, None)
    assert "base_layers" not in bare and "from_base_source" not in bare  # nothing to go on: left out, never null
    rows: list[Json] = [{"name": "curl", "version": "8.9.1-r0", "type": "apk", "purl": None, "licenses": []}]
    table = docs.packages(BUILD, "arm64", rows)
    assert table == {
        "image": "sonarr",
        "tag": "4.0.20.3014-ls1",
        "arch": "arm64",
        "count": 1,
        "packages": [{"name": "curl", "version": "8.9.1-r0", "type": "apk", "licenses": []}],
    }


def test_build_result() -> None:
    partial = {"platforms": [{"result": "success"}, {"result": "failure"}], "test": {"success": True}}
    assert docs.build_result(partial) == "partial"
    assert docs.build_result({"platforms": [{"result": "failure"}], "test": {"enabled": False}}) == "failure"
    assert docs.build_result({"platforms": [{"result": "success"}], "test": {"enabled": True, "success": False}}) == (
        "failure"
    )
    assert docs.build_result(BUILD) == "success"


def test_published_tags_prefer_the_published_field() -> None:
    assert docs.published_tags(BUILD) == BUILD["version"]["tags"]
    published = {**BUILD, "published": {"image": "ghcr.io/linuxserver-playground/sonarr", "tags": ["latest", "x"]}}
    assert docs.published_tags(published) == ["latest", "x"]
    assert docs.published_tags({"version": {}}) == []


def test_report_is_the_build_page_aggregate() -> None:
    previous = copy.deepcopy(BUILD)
    previous["version"].update(upstream="4.0.19.2000", tag="4.0.19.2000-ls1")
    diff = diff_builds(BUILD, previous, None, None)
    links = docs.links(BUILD, PAGES, SITE, tests=True)
    report = docs.report(BUILD, diff=diff, reports={"amd64": REPORT}, build_links=links, site=SITE)
    assert report["result"] == "success" and report["classification"] == "upstream"
    assert report["diff"]["previous_tag"] == "4.0.19.2000-ls1" and report["diff"]["config_changes"] == 0
    assert "packages" not in report["diff"] and report["tags"] == BUILD["version"]["tags"]
    assert report["tests"]["amd64"]["total"] == 10 and "is_newest" not in report  # the Pages copy says nothing of it
    hub = docs.report(BUILD, diff=diff, reports={}, build_links=links, site=SITE, is_newest=False)
    assert hub["is_newest"] is False
    assert report["links"]["registry"] == "ghcr.io/linuxserver-playground/sonarr:4.0.20.3014-ls1"
    assert report["links"]["history"] == f"{SITE}images/sonarr/#builds"
    assert report["links"]["run"] == BUILD["workflow"]["run_url"] and report["links"]["build"] == f"{PAGES}build.json"


def test_parts_from_a_record_need_no_other_asset() -> None:
    record = {
        "tag": BUILD["version"]["tag"],
        "packages": {"amd64": [{"name": "curl", "version": "1", "type": "apk", "purl": None, "licenses": []}]},
        "layers": {"amd64": LAYERS},
        "tests": {"amd64": REPORT},
        "diff": {"classification": "none"},
        "base": BASE,
    }
    read_names: list[str] = []

    def read(name: str) -> bytes | None:
        read_names.append(name)
        return None

    parts = docs.parts(BUILD, record, read, previous=lambda tag: None, fetch_base=lambda i, d: {})
    assert parts.diff == {"classification": "none"} and parts.base == BASE
    assert parts.layer_rows == {"amd64": LAYERS} and parts.reports == {"amd64": REPORT}
    assert list(parts.package_rows) == ["amd64"]
    # Only what the record lacks is read: arm64's layers, packages and report.
    assert sorted(read_names) == ["layers.arm64.json", "packages.arm64.json.gz", "test.arm64.json"]


def test_parts_without_a_record_derive_from_the_assets() -> None:
    bundle = copy.deepcopy(BUILD)
    bundle["previous"] = {"tag": "4.0.19.2000-ls1"}
    old = copy.deepcopy(BUILD)
    old["version"].update(upstream="4.0.19.2000", tag="4.0.19.2000-ls1")
    old_rows = {"amd64": normalise(json.loads(gzip.decompress(syft(("curl", "8.9.0-r0")))))}
    assets = {
        "test.amd64.json": json.dumps(REPORT).encode(),
        "test.arm64.json": b"{not json",
        "layers.amd64.json": json.dumps(LAYERS).encode(),
        "packages.amd64.json.gz": syft(("curl", "8.9.1-r0"), ("krb5-libs", "1.21")),
        "packages.arm64.json.gz": b"not gzip",
    }
    asked: list[str] = []

    def previous(tag: str) -> tuple[Json, dict[str, list[Json]] | None] | None:
        asked.append(tag)
        return old, old_rows

    def fetch_base(image: str, digest: str | None) -> Json:
        return BASE

    parts = docs.parts(bundle, None, assets.get, previous=previous, fetch_base=fetch_base)
    assert asked == ["4.0.19.2000-ls1"] and parts.base == BASE
    assert list(parts.reports) == ["amd64"] and list(parts.layer_rows) == ["amd64"]
    assert [p["name"] for p in parts.package_rows["amd64"]] == ["curl", "krb5-libs"]
    assert "arm64" not in parts.package_rows
    assert any("packages.arm64.json.gz unreadable" in n for n in parts.notes)
    assert parts.diff is not None and parts.diff["classification"] == "upstream"
    assert parts.diff["packages"]["amd64"]["apk"]["added"][0]["name"] == "krb5-libs"

    # The previous release unreadable: diffed as the first build, and said so.
    lost = docs.parts(bundle, None, assets.get, previous=lambda tag: None)
    assert lost.diff is not None and lost.diff["classification"] == "initial" and lost.base is None
    assert any("could not be read" in n for n in lost.notes)
    # A record of another build is not this build's record.
    other = docs.parts(bundle, {"tag": "elsewhere", "diff": {"classification": "none"}}, assets.get)
    assert other.diff is not None and other.diff["classification"] != "none"


def test_documents_writes_each_file_and_links_them() -> None:
    parts = docs.parts(
        BUILD,
        None,
        {
            "test.amd64.json": json.dumps(REPORT).encode(),
            "layers.amd64.json": json.dumps(LAYERS).encode(),
            "packages.amd64.json.gz": syft(("curl", "8.9.1-r0")),
        }.get,
    )
    out = docs.documents(
        BUILD,
        pages=PAGES,
        site=SITE,
        diff=parts.diff,
        reports=parts.reports,
        layer_rows=parts.layer_rows,
        package_rows=parts.package_rows,
        base=parts.base,
        vulns=True,
    )
    assert sorted(out) == ["diff.json", "layers.amd64.json", "packages.amd64.json", "report.json", "tests.json"]
    assert out["diff.json"]["classification"] == "initial" and "previous_tag" not in out["diff.json"]
    assert out["report.json"]["links"]["vulns"] == f"{PAGES}vulns.json"
    assert out["packages.amd64.json"]["count"] == 1
    assert json.loads(json.dumps(out)) == out  # plain JSON throughout
