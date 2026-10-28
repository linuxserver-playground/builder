"""
The per-build documents, shaped once for every reader (CONVENTIONS §6): `report.json` (`report`), `tests.json`
(`tests`), `layers.<arch>.json` (`layers`), `packages.<arch>.json` (`packages`), the diff (`builder.derive.diff`) and
`vulns.json` (`vulns.document`). The hub's API serves them, rendered with these functions inside its `{status, data}`
envelope; the pages assembler in workflows writes envelope-free copies of them into each build's folder on its
repository's Pages, with `links` pointing there. The hub is the API: nothing depends on those copies.

Every document is plain JSON, fields without a value left out (as the hub serves them). `parts` gathers what a
document is made from: `record.json` when the release has one, else the other Release assets with the same
`builder.derive` code the build runs.

`pages` below is the build's folder on its repository's Pages, `https://<owner>.github.io/<repo>/builds/<tag>/`, with
its trailing slash; `site` is the website's root (`SITE_URL`), with or without one.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from . import packages as pkg
from .diff import diff_builds
from .layers import annotate

Json = dict[str, Any]

ARCHES = ("amd64", "arm64")
REGISTRY = "ghcr.io"
GITHUB = "https://github.com"
REPORT_FILES = {
    "screenshot": "screenshot.png",
    "container_log": "container.log",
    "browser_log": "browser.log",
    "sysinfo": "sysinfo.txt",
    "shellcheck": "shellcheck.xml",
}
"""test.<arch>.json `files` keys and the fixed names their files have under `builds/<tag>/<arch>/` on Pages."""


def prune(value: Any) -> Any:
    """`value` with every None member of every object left out, recursively (list items are kept)."""
    if isinstance(value, dict):
        return {k: prune(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [prune(v) for v in value]
    return value


def _clean(doc: Json) -> Json:
    """`prune` of a document."""
    out: Json = prune(doc)
    return out


def release_download(repository: str, tag: str, name: str) -> str:
    """A Release asset's download URL: a link only, Release assets send no CORS headers."""
    return f"{GITHUB}/{repository}/releases/download/{tag}/{name}"


def site_build(site: str, image: str, tag: str) -> str:
    """The build's page on the website, its CI results page (CONVENTIONS §10)."""
    return f"{site.rstrip('/')}/images/{image}/builds/{tag}/"


def site_builds(site: str, image: str) -> str:
    return f"{site.rstrip('/')}/images/{image}/#builds"


def _asset_names(bundle: Json) -> set[str]:
    return {str(a["name"]) for a in bundle.get("assets") or [] if a.get("name")}


def _asset_urls(bundle: Json) -> dict[str, str]:
    return {str(a["name"]): str(a["url"]) for a in bundle.get("assets") or [] if a.get("name") and a.get("url")}


def _arches_with(bundle: Json, name: str) -> list[str]:
    """The architectures whose `name` (with `{arch}`) is among the bundle's assets."""
    names = _asset_names(bundle)
    return [arch for arch in ARCHES if name.format(arch=arch) in names]


def links(
    bundle: Json, pages: str, site: str, *, release: str | None = None, tests: bool = False, vulns: bool = False
) -> Json:
    """
    Where a build's documents and files are on its repository's Pages (`pages`), for the copies the pages assembler
    writes there, with its run record and logs on the Release (download links) and its page on the website. The hub
    links its own routes for the documents instead.
    The assets the bundle lists decide which per-architecture files exist; `tests` and `vulns` say whether the build
    has test reports and a scan of its own.
    """
    tag, repository = bundle["version"]["tag"], bundle["repository"]
    sbom = _arches_with(bundle, "packages.{arch}.json.gz")
    return _clean(
        {
            "build": f"{pages}build.json",
            "diff": f"{pages}diff.json",
            "layers": {arch: f"{pages}layers.{arch}.json" for arch in _arches_with(bundle, "layers.{arch}.json")},
            "packages": {arch: f"{pages}packages.{arch}.json" for arch in sbom},
            "release": release or f"{GITHUB}/{repository}/releases/tag/{tag}",
            "pages": pages,
            "files": {arch: f"{pages}files.{arch}.json.gz" for arch in _arches_with(bundle, "files.{arch}.json.gz")},
            "sbom": {arch: f"{pages}packages.{arch}.json.gz" for arch in sbom},
            "run_json": release_download(repository, tag, "run.json"),
            "logs_zip": release_download(repository, tag, "logs.zip"),
            "test_report": site_build(site, bundle["image"], tag),
            "tests": f"{pages}tests.json" if tests else None,
            "report": f"{pages}report.json",
            "vulns": f"{pages}vulns.json" if vulns else None,
        }
    )


# ---------------------------------------------------------------------------------------------------
# tests.json


def test_summary(bundle: Json, arch: str, report: Json, pages: str) -> Json:
    """One architecture's harness report in short: counts, failed tests, startup, ports and its files on Pages."""
    runtime = report.get("runtime") or {}
    cases = report.get("tests") or []
    failed = [c["name"] for c in cases if str(c.get("status", "")).upper() not in ("PASS", "SKIP")]
    ports = [f"{p}/tcp" for p in runtime.get("listening_tcp") or []] + [
        f"{p}/udp" for p in runtime.get("listening_udp") or []
    ]
    return _clean(
        {
            "success": bool(report.get("success")),
            "passed": sum(1 for c in cases if str(c.get("status", "")).upper() == "PASS"),
            "failed": len(failed),
            "total": len(cases),
            "failed_tests": failed,
            "startup_seconds": report.get("startup_seconds"),
            "listening_ports": ports,
            "warnings": len(report.get("warnings") or {}),
            "rss_bytes": runtime.get("rss_bytes"),
            "pids": runtime.get("pids"),
            "tarball": _asset_urls(bundle).get(f"test.{arch}.tar.gz"),
            "files": {
                kind: f"{pages}{arch}/{REPORT_FILES[kind]}"
                for kind in sorted(report.get("files") or {})
                if kind in REPORT_FILES
            },
        }
    )


def test_summaries(bundle: Json, reports: dict[str, Json], pages: str) -> dict[str, Json]:
    return {arch: test_summary(bundle, arch, reports[arch], pages) for arch in sorted(reports)}


def tests(bundle: Json, reports: dict[str, Json], pages: str, site: str) -> Json | None:
    """`tests.json`: the harness reports per architecture, as received, with a summary each; None without any."""
    if not reports:
        return None
    test = bundle.get("test") or {}
    enabled = bool(test.get("enabled", True))
    return _clean(
        {
            "image": bundle["image"],
            "tag": bundle["version"]["tag"],
            "enabled": enabled,
            "success": test.get("success") if enabled else None,
            "report_url": site_build(site, bundle["image"], bundle["version"]["tag"]),
            "summary": test_summaries(bundle, reports, pages),
            "reports": {arch: reports[arch] for arch in sorted(reports)},
        }
    )


# ---------------------------------------------------------------------------------------------------
# layers.<arch>.json and packages.<arch>.json


def layers(bundle: Json, arch: str, rows: list[Json], base: Json | None) -> Json:
    """
    `layers.<arch>.json`: the harness's layer list with `from_base` (from the base image's layer digests, record.json
    `base`) and `dockerfile_lines` (matched against the Dockerfile in build.json), and how `from_base` was decided.
    """
    annotated, meta = annotate(rows, (bundle.get("dockerfile") or {}).get(arch), base, arch)
    doc = {"image": bundle["image"], "tag": bundle["version"]["tag"], "arch": arch, **meta, "layers": annotated}
    return _clean(doc)


def packages(bundle: Json, arch: str, rows: list[Json]) -> Json:
    """`packages.<arch>.json`: the normalised package table (`packages.normalise`) of one architecture."""
    return {
        "image": bundle["image"],
        "tag": bundle["version"]["tag"],
        "arch": arch,
        "count": len(rows),
        "packages": [prune(dict(r)) for r in rows],
    }


# ---------------------------------------------------------------------------------------------------
# report.json


def diff_summary(diff: Json) -> Json:
    """`diff.json` without the package lists: enough for a build page's change strip."""
    config = diff.get("config", {})
    env = config.get("env", {})
    changes = len(env.get("added", [])) + len(env.get("removed", [])) + len(env.get("changed", []))
    for key in ("exposed_ports", "volumes"):
        changes += len(config.get(key, {}).get("added", [])) + len(config.get(key, {}).get("removed", []))
    base = diff.get("base", {})
    return _clean(
        {
            "previous_tag": diff.get("previous_tag"),
            "classification": diff["classification"],
            "reasons": list(diff.get("reasons", [])),
            "upstream": diff["upstream"],
            "base_changed": bool(base.get("changed")),
            "base_from": (base.get("from") or {}).get("digest") if base.get("changed") else None,
            "size": diff.get("size", {}),
            "totals": diff["totals"],
            "config_changes": changes,
            "dockerfile_changed": sorted(arch for arch, text in diff.get("dockerfile", {}).items() if text),
        }
    )


def build_result(bundle: Json) -> Literal["success", "failure", "partial"]:
    """`success`, `failure` or `partial` over every platform and the test outcome."""
    platforms = bundle.get("platforms", [])
    ok = sum(1 for p in platforms if p.get("result") == "success")
    test = bundle.get("test") or {}
    tested = test.get("success") if test.get("enabled", True) else None
    if ok == len(platforms) and tested is not False:
        return "success"
    if ok == 0 or tested is False:
        return "failure"
    return "partial"


def registry_ref(bundle: Json) -> str:
    """`ghcr.io/<owner>/<image>`, the image the build published."""
    owner = str(bundle["repository"]).split("/", 1)[0]
    return f"{REGISTRY}/{owner}/{bundle['image']}"


def report(
    bundle: Json,
    *,
    diff: Json | None,
    reports: dict[str, Json],
    build_links: Json,
    site: str,
    is_newest: bool | None = None,
) -> Json:
    """
    `report.json`: everything a build page shows in one document: the record's facts, platforms, the test summary
    per architecture, the diff summary (without the package lists) and every link (`build_links`, with
    `build_links["pages"]` the build's folder on its repository's Pages). `is_newest`, whether it is still its branch's
    newest build, changes after the release: the hub, which knows, passes it; the copy on the repository's Pages
    leaves it out.
    """
    version, test = bundle["version"], bundle.get("test") or {}
    tag, repository = version["tag"], bundle["repository"]
    enabled = bool(test.get("enabled", True))
    return _clean(
        {
            "image": bundle["image"],
            "repository": repository,
            "tag": tag,
            "branch": bundle["branch"],
            "release_tag": version.get("release_tag"),
            "upstream_version": version["upstream"],
            "upstream_clean": version["upstream_clean"],
            "semver": version.get("semver"),
            "ls_build": version["ls_build"],
            "created": bundle["created"],
            "reason": bundle["reason"],
            "commit": bundle["commit"],
            "tags": published_tags(bundle) or [tag],
            "base": bundle["base"],
            "platforms": bundle["platforms"],
            "index_digest": bundle.get("index_digest"),
            "result": build_result(bundle),
            "test_enabled": enabled,
            "test_success": test.get("success") if enabled else None,
            "tests": test_summaries(bundle, reports, build_links["pages"]),
            "classification": diff["classification"] if diff else None,
            "diff": diff_summary(diff) if diff else None,
            "packages": bundle.get("packages"),
            "notes": bundle.get("notes"),
            "config": bundle["config"],
            "is_newest": is_newest,
            "links": {
                **build_links,
                "run": bundle["workflow"]["run_url"],
                "commit": f"{GITHUB}/{repository}/commit/{bundle['commit']}",
                "repository": f"{GITHUB}/{repository}",
                "registry": f"{registry_ref(bundle)}:{tag}",
                "history": site_builds(site, bundle["image"]),
            },
        }
    )


def published_tags(bundle: Json) -> list[str]:
    """
    The tags the build pushed: `published.tags` (CONVENTIONS §6), else, for a release made before the field, the
    planned `version.tags`, which a live release published in full (its Release exists only once they were).
    """
    published = bundle.get("published") or {}
    tags = published.get("tags") if isinstance(published, dict) else None
    if isinstance(tags, list) and tags:
        return [str(t) for t in tags]
    return [str(t) for t in (bundle.get("version") or {}).get("tags") or []]


# ---------------------------------------------------------------------------------------------------
# The parts, from a release's assets

Read = Callable[[str], "bytes | None"]
"""A Release asset's bytes by name, None when the release has no such asset or it cannot be read."""
Previous = Callable[[str], "tuple[Json, dict[str, list[Json]] | None] | None"]
"""The previous release by tag: its build.json and its package rows per architecture (None when unknown)."""
FetchBase = Callable[[str, "str | None"], Json]
"""`registry.base_layers`: a base image's layer digests per architecture."""


@dataclass
class Parts:
    """What a build's documents are made from (`documents`), with the reasons any part is missing."""

    diff: Json | None = None
    reports: dict[str, Json] = field(default_factory=dict)
    layer_rows: dict[str, list[Json]] = field(default_factory=dict)
    package_rows: dict[str, list[Json]] = field(default_factory=dict)
    base: Json | None = None
    notes: list[str] = field(default_factory=list)


def _parse(data: bytes | None) -> Any:
    if not data:
        return None
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None


def parts(
    bundle: Json,
    record: Json | None,
    read: Read,
    *,
    previous: Previous | None = None,
    fetch_base: FetchBase | None = None,
) -> Parts:
    """
    A release's parts: from its record.json (`record`) where the build derived them, else from the other Release
    assets with the code the build runs: the package tables from the syft documents (`packages.normalise_gz`), the
    layer lists and test reports as attached (a report that does not validate against test-report.schema.json is left
    out), the base image's layers from the registry (`fetch_base`, once per release), and the diff against the
    previous release of the branch (`previous`, by build.json `previous.tag`; `diff.diff_builds`).
    """
    from contracts import ContractError, validate

    record = record if isinstance(record, dict) and record.get("tag") == bundle["version"]["tag"] else None
    derived = record or {}
    out = Parts()
    platforms = bundle.get("platforms") or []
    successful = [p["arch"] for p in platforms if p.get("result") == "success" and p["arch"] in ARCHES]
    tag = bundle["version"]["tag"]
    for arch in ARCHES:
        report = (derived.get("tests") or {}).get(arch)
        if report is None:
            report = _parse(read(f"test.{arch}.json"))
        if not isinstance(report, dict):
            continue
        try:
            validate("test-report", report)
        except ContractError as e:
            out.notes.append(f"{tag}: test.{arch}.json does not validate: {str(e).splitlines()[0]}")
            continue
        out.reports[arch] = report
    for arch in successful:
        rows = (derived.get("layers") or {}).get(arch)
        if rows is None:
            rows = _parse(read(f"layers.{arch}.json"))
        if isinstance(rows, list):
            out.layer_rows[arch] = rows
        table = (derived.get("packages") or {}).get(arch)
        if table is None and (data := read(f"packages.{arch}.json.gz")) is not None:
            try:
                table = pkg.normalise_gz(data)
            except (OSError, ValueError, EOFError) as e:
                out.notes.append(f"{tag}: packages.{arch}.json.gz unreadable: {e}")
        if isinstance(table, list):
            out.package_rows[arch] = table
    out.base = derived.get("base")
    digest = (bundle.get("base") or {}).get("digest")
    if out.base is None and fetch_base is not None and digest:
        try:
            out.base = fetch_base(bundle["base"]["image"], digest)
        except Exception as e:  # the layers are still served, without from_base from the registry
            out.notes.append(f"{tag}: base {bundle['base']['image']}: {str(e).splitlines()[0] if str(e) else e!r}")
    if isinstance(derived.get("diff"), dict):
        out.diff = derived["diff"]
    else:
        old_tag = (bundle.get("previous") or {}).get("tag")
        old = previous(old_tag) if previous is not None and old_tag else None
        if old_tag and old is None:
            out.notes.append(f"{tag}: the previous release {old_tag} could not be read; diffed as the first build")
        old_bundle, old_rows = old if old else (None, None)
        out.diff = diff_builds(bundle, old_bundle, out.package_rows or None, old_rows)
    return out


# ---------------------------------------------------------------------------------------------------
# Everything at once


def documents(
    bundle: Json,
    *,
    pages: str,
    site: str,
    diff: Json | None,
    reports: dict[str, Json],
    layer_rows: dict[str, list[Json]],
    package_rows: dict[str, list[Json]],
    base: Json | None,
    release: str | None = None,
    vulns: bool = False,
) -> dict[str, Json]:
    """
    {file name under builds/<tag>/: document} for one build, `build.json` excepted (the Release asset as it is) and
    `vulns.json` excepted (`vulns.document`). `vulns` says whether that one is written, for the links.
    """
    build_links = links(bundle, pages, site, release=release, tests=bool(reports), vulns=vulns)
    out: dict[str, Json] = {}
    if diff is not None:
        out["diff.json"] = _clean(diff)
    if (doc := tests(bundle, reports, pages, site)) is not None:
        out["tests.json"] = doc
    for arch in ARCHES:
        if arch in layer_rows:
            out[f"layers.{arch}.json"] = layers(bundle, arch, layer_rows[arch], base)
        if arch in package_rows:
            out[f"packages.{arch}.json"] = packages(bundle, arch, package_rows[arch])
    out["report.json"] = report(bundle, diff=diff, reports=reports, build_links=build_links, site=site)
    return out
