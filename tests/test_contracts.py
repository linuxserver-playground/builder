"""The contracts validate the example manifests and reject the mistakes they are meant to catch."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from contracts import KINDS, SCHEMA_DIR, ContractError, errors, load_yaml, validate, validator

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / "examples").glob("*/image.yml"))


@pytest.mark.parametrize("filename", sorted(KINDS.values()))
def test_schemas_are_valid_2020_12(filename: str) -> None:
    schema = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(schema)
    assert schema["$id"].endswith(filename)


@pytest.mark.parametrize("path", EXAMPLES, ids=[p.parent.name for p in EXAMPLES])
def test_examples_validate(path: Path) -> None:
    validate("image", load_yaml(path))


def _sonarr() -> dict[str, Any]:
    data: dict[str, Any] = load_yaml(ROOT / "examples" / "sonarr" / "image.yml")
    return data


def test_unknown_top_level_key_is_rejected() -> None:
    data = _sonarr()
    data["jenkins_vars"] = {}
    with pytest.raises(ContractError) as excinfo:
        validate("image", data)
    assert "jenkins_vars" in str(excinfo.value)


def test_version_source_requires_type_specific_fields() -> None:
    data = _sonarr()
    data["upstream"]["version"] = {"type": "github_release"}
    found = errors("image", data)
    assert any("repo" in e.message for e in found)


def test_changelog_date_must_be_iso() -> None:
    data = _sonarr()
    data["changelog"][0]["date"] = "19.10.26:"
    found = errors("image", data)
    assert found and found[0].absolute_path[0] == "changelog"


def test_port_pattern_allows_udp_suffix_only() -> None:
    data = _sonarr()
    data["runtime"]["ports"][0]["internal"] = "1900/udp"
    validate("image", data)
    data["runtime"]["ports"][0]["internal"] = "1900/sctp"
    assert errors("image", data)


def test_env_names_are_identifiers() -> None:
    data = _sonarr()
    data["runtime"]["env"] = [{"name": "JELLYFIN_PublishedServerUrl", "description": "mixed case is real"}]
    validate("image", data)
    data["runtime"]["env"] = [{"name": "WITH-DASH", "description": "x"}]
    assert errors("image", data)
    data["runtime"]["env"] = [{"name": "1LEADING_DIGIT", "description": "x"}]
    assert errors("image", data)


def test_release_tag_is_docker_safe() -> None:
    data = _sonarr()
    data["release"]["tag"] = "Not Valid"
    assert errors("image", data)


def test_os_source_needs_nothing_else() -> None:
    data = _sonarr()
    data["upstream"]["version"] = {"type": "os"}
    validate("image", data)


def test_mod_manifest_minimal_and_version_ref() -> None:
    mod = {
        "schema": 1,
        "name": "python3",
        "target": "code-server",
        "summary": "Installs python3 and pip into code-server.",
        "maintainers": ["thelamer"],
        "version": {"type": "pip", "package": "pip"},
    }
    validate("mod", mod)
    broken = copy.deepcopy(mod)
    broken["version"] = {"type": "github_release"}
    assert errors("mod", broken)


def _bundle() -> dict[str, Any]:
    return {
        "schema": 1,
        "image": "sonarr",
        "repository": "linuxserver-playground/docker-sonarr",
        "branch": "master",
        "mode": "live",
        "version": {
            "upstream": "4.0.20.3014",
            "upstream_clean": "4.0.20.3014",
            "ls_build": 1,
            "tag": "4.0.20.3014-ls1",
            "semver": "4.0.20",
        },
        "commit": "a" * 40,
        "created": "2026-10-24T12:00:00Z",
        "reason": "upstream",
        "base": {
            "family": "alpine",
            "image": "ghcr.io/linuxserver/baseimage-alpine:3.24",
            "digest": "sha256:" + "b" * 64,
        },
        "platforms": [{"arch": "amd64", "result": "success", "digest": "sha256:" + "c" * 64, "size": 123}],
        "labels": {"org.opencontainers.image.version": "4.0.20.3014-ls1"},
        "config": {"env": ["PATH=/usr/bin"], "exposed_ports": ["8989/tcp"]},
        "dockerfile": {"amd64": "FROM ghcr.io/linuxserver/baseimage-alpine:3.24\n"},
        "test": {"enabled": True, "success": True, "per_arch": {"amd64": True}},
        "assets": [{"name": "packages.amd64.json.gz", "size": 10, "sha256": "d" * 64}],
        "workflow": {"run_id": 1, "run_url": "https://github.com/x/y/actions/runs/1", "workflows_ref": "v1"},
    }


def test_bundle_example_validates() -> None:
    validate("bundle", _bundle())


def test_bundle_failed_platform_needs_no_digest() -> None:
    data = _bundle()
    data["platforms"] = [{"arch": "arm64", "result": "failure"}]
    validate("bundle", data)
    data["platforms"] = [{"arch": "arm64"}]
    assert errors("bundle", data)


def test_bundle_rejects_bad_digest() -> None:
    data = _bundle()
    data["platforms"][0]["digest"] = "sha256:short"
    assert errors("bundle", data)


def test_control_event_summary_embeds_bundle_or_null() -> None:
    base = {
        "schema": 1,
        "type": "build.summary",
        "sent": "2026-10-24T12:00:00Z",
        "repository": "linuxserver-playground/docker-sonarr",
        "run_id": 1,
        "result": "failure",
        "bundle": None,
    }
    validate("control-event", base)
    base["bundle"] = _bundle()
    base["result"] = "success"
    validate("control-event", base)
    base["bundle"] = {"schema": 2}
    assert errors("control-event", base)


def test_test_report_minimal() -> None:
    validate(
        "test-report",
        {
            "schema": 1,
            "image": "sonarr",
            "tag": "amd64-4.0.20.3014-ls1",
            "arch": "amd64",
            "started": "2026-10-24T12:00:00Z",
            "finished": "2026-10-24T12:03:00Z",
            "success": True,
            "tests": [{"name": "Container startup", "status": "PASS", "seconds": 1.0}],
        },
    )


def test_validator_has_format_checking() -> None:
    data = _bundle()
    data["created"] = "yesterday"
    assert errors("bundle", data)
    assert validator("bundle").format_checker is not None


def test_kind_is_optional_and_closed() -> None:
    data = _sonarr()
    assert "kind" not in data
    data["kind"] = "tool"
    validate("image", data)
    data["kind"] = "library"
    assert errors("image", data)
