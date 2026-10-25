"""Migration of the live readme-vars/jenkins-vars fixtures produces manifests that validate, with the right sources."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from builder.cli import main
from builder.manifest import load_manifest_text
from builder.migrate import migrate, migrate_repo, parse_notes_command, parse_version_command, split_pipeline
from builder.render import render_all
from contracts import load_yaml, validate

from .conftest import LIVE, SETTINGS


def _load(name: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    fixture = next(p for p in LIVE if p.name == name)
    readme_vars: dict[str, Any] = load_yaml(fixture / "readme-vars.yml")
    jenkins_vars: dict[str, Any] = load_yaml(fixture / "jenkins-vars.yml")
    dockerfile = (fixture / "Dockerfile.from").read_text(encoding="utf-8")
    return readme_vars, jenkins_vars, dockerfile


def _migrate(name: str) -> tuple[dict[str, Any], str, list[str]]:
    return migrate(*_load(name))


@pytest.mark.parametrize("fixture", LIVE, ids=lambda p: p.name)
def test_live_fixture_migrates_to_a_valid_manifest(fixture: Path) -> None:
    manifest, text, todos = _migrate(fixture.name)
    validate("image", manifest)
    reloaded = load_yaml_text(text)
    validate("image", reloaded)
    assert reloaded["name"] == manifest["name"]
    assert not any(t.startswith("does not validate") for t in todos), todos
    render_all(reloaded, SETTINGS)


def load_yaml_text(text: str) -> Any:
    """Parse YAML text the way contracts.load_yaml parses files (dates stay strings)."""
    return load_manifest_text(text)


def test_sonarr() -> None:
    manifest, text, _ = _migrate("sonarr")
    version = manifest["upstream"]["version"]
    assert version["type"] in ("json", "command")
    assert version["type"] == "json"
    assert version["url"] == "http://services.sonarr.tv/v1/releases"
    assert "v4-stable" in version["jq"]
    assert manifest["upstream"]["notes"] == {"type": "github_release", "repo": "sonarr/sonarr", "tag": "v${VERSION}"}
    assert manifest["upstream"]["sponsor"] == [{"name": "Sonarr", "url": "https://sonarr.tv/donate"}]
    assert manifest["release"] == {
        "branch": "master",
        "tag": "latest",
        "channel": "stable",
        "tags": [
            {"tag": "latest", "description": "Stable releases from Sonarr"},
            {"tag": "develop", "description": "Development releases from Sonarr"},
        ],
    }
    assert manifest["build"] == {"base": "alpine", "version_arg": "SONARR_VERSION", "platforms": ["amd64", "arm64"]}
    assert manifest["test"] == {"startup_timeout": 240, "web": {"port": 8989, "path": "/system/status"}}
    volumes = manifest["runtime"]["volumes"]
    assert [v["path"] for v in volumes] == ["/config", "/tv", "/downloads"]
    assert volumes[0].get("optional") is None and volumes[1]["optional"] is True
    assert manifest["runtime"]["ports"] == [
        {"internal": "8989", "description": "The port for the Sonarr web interface"}
    ]
    assert manifest["runtime"]["readonly"] == {"supported": True}
    assert manifest["docs"]["categories"] == ["Media Management"]
    assert manifest["docs"]["notices"] == {"media": True}
    assert (
        manifest["docs"]["setup"]
        == "Access the webui at `<your-ip>:8989`, for more information check out [Sonarr](https://sonarr.tv/).\n"
    )
    assert manifest["proxy"] == {"port": 8989, "subdomain": True, "subfolder": {"supported": False}}
    assert "# TODO: confirm whether the app supports a subfolder" in text
    assert manifest["changelog"][0] == {"date": "2026-10-19", "text": "Add krb5-libs for better postgresql support."}
    assert manifest["summary"].startswith("[Sonarr](https://sonarr.tv/) (formerly NZBdrone)")


def test_plex() -> None:
    manifest, _, _ = _migrate("plex")
    assert manifest["upstream"]["version"] == {
        "type": "json",
        "url": "https://plex.tv/api/downloads/5.json",
        "jq": ".computer.Linux.version",
    }
    assert "notes" not in manifest["upstream"]
    assert manifest["runtime"]["network"] == {"mode": "host", "description": "Use Host Networking"}
    assert manifest["runtime"]["env"][0]["name"] == "VERSION"
    assert manifest["runtime"]["env"][1] == {
        "name": "PLEX_CLAIM",
        "value": "",
        "description": "Optionally you can obtain a claim token from https://plex.tv/claim and input here. "
        "Keep in mind that the claim tokens expire within 4 minutes.",
        "optional": True,
    }
    assert manifest["runtime"]["readonly"]["notes"].startswith("* Runtime update of Plex")
    assert manifest["docs"]["notices"] == {"hwaccel": True}
    assert manifest["docs"]["categories"] == ["Media Servers", "Music", "Audiobooks"]
    assert "?+note[Updates](|If there is no value set" in manifest["docs"]["setup"]
    assert manifest["docs"]["optional_parameters"].startswith("If you want to run the container in bridge network mode")
    assert manifest["test"]["web"] == {"port": 32400, "path": "/web/index.html"}


def test_swag() -> None:
    manifest, _, _ = _migrate("swag")
    assert manifest["upstream"]["version"] == {"type": "pip", "package": "certbot"}
    assert manifest["test"]["enabled"] is False
    assert "proxy" not in manifest
    assert manifest["build"]["base"] == "alpine-nginx"
    assert manifest["runtime"]["cap_add"] == [
        {"cap": "NET_ADMIN", "description": "Required for fail2Ban to be able to modify iptables rules."}
    ]
    env = {e["name"]: e for e in manifest["runtime"]["env"]}
    assert env["VALIDATION"]["options"] == ["http", "dns"]
    assert env["SUBDOMAINS"]["optional"] is True
    assert [p["internal"] for p in manifest["runtime"]["ports"]] == ["443", "80", "443/udp"]


def test_qbittorrent() -> None:
    manifest, _, _ = _migrate("qbittorrent")
    version = manifest["upstream"]["version"]
    assert version["type"] == "github_release"
    assert version["repo"] == "userdocs/qbittorrent-nox-static"
    assert "transform" in version
    import re

    regex, replace = version["transform"][0]["regex"], version["transform"][0]["replace"]
    assert re.sub(regex, replace, "release-5.0.4_v2.0.11") == "5.0.4_v2.0.11"
    assert manifest["upstream"]["notes"]["type"] == "command"
    assert "${VERSION%%_v2*}" in manifest["upstream"]["notes"]["run"]
    assert manifest["runtime"]["custom"] == [
        {
            "flag": "stop-timeout",
            "compose": "stop_grace_period",
            "value": "10s",
            "description": "If you have a large number of torrents you may need to increase the container stop "
            "timeout to ensure a clean shutdown.",
            "optional": True,
        }
    ]
    assert manifest["runtime"]["ports"][2] == {"internal": "6881/udp", "description": "udp connection port"}


def test_webtop() -> None:
    manifest, _, _ = _migrate("webtop")
    assert manifest["upstream"]["version"] == {"type": "os"}
    assert manifest["build"]["base"] == "selkies"
    assert manifest["docs"]["notices"] == {"strict_proxy": True, "selkies": {"enabled": True, "nvidia": True}}
    assert manifest["test"]["web"] == {"port": 3001, "scheme": "https", "auth": "user:password", "screenshot_delay": 30}
    assert manifest["test"]["env"] == {"TZ": "US/Pacific"}
    assert manifest["proxy"]["scheme"] == "https"
    assert manifest["runtime"]["custom"][0] == {
        "flag": "shm-size",
        "compose": "shm_size",
        "value": "1gb",
        "description": "Recommended for all desktop images.",
    }
    assert len(manifest["release"]["tags"]) == 22


def test_bookstack() -> None:
    manifest, _, _ = _migrate("bookstack")
    assert manifest["upstream"]["version"] == {"type": "codeberg_release", "repo": "bookstack/bookstack"}
    assert manifest["upstream"]["notes"] == {
        "type": "codeberg_release",
        "repo": "bookstack/bookstack",
        "tag": "${VERSION}",
    }
    assert manifest["build"]["base"] == "alpine-nginx"
    assert manifest["runtime"]["ports"] == [
        {"internal": "80", "external": "6875", "description": "http/s web interface."}
    ]
    assert manifest["runtime"]["readonly"] == {"supported": True, "notes": "* `/tmp` must be mounted to tmpfs\n"}


def test_baseimage_alpine() -> None:
    manifest, _, todos = _migrate("baseimage-alpine")
    assert manifest["name"] == "baseimage-alpine"
    assert manifest["upstream"]["version"] == {"type": "os"}
    assert manifest["build"]["first_party"] is False
    assert manifest["build"]["version_arg"] == "OS"
    assert manifest["release"] == {"branch": "3.24", "tag": "3.24", "channel": "stable"}
    assert manifest["docs"]["custom_readme"].startswith("[![linuxserver.io]")
    assert "{% raw" not in manifest["docs"]["custom_readme"]
    assert "container_name" not in manifest["runtime"]
    assert manifest["test"]["env"] == {"LSIO_FIRST_PARTY": "true"}
    assert any("categories" in t for t in todos)


def test_jellyfin_and_code_server_and_heimdall() -> None:
    jellyfin, _, _ = _migrate("jellyfin")
    assert jellyfin["upstream"]["version"] == {
        "type": "deb_packages",
        "index_url": "https://repo.jellyfin.org/ubuntu/dists/resolute/main/binary-amd64/Packages",
        "package": "jellyfin-server",
    }
    assert jellyfin["runtime"]["env"][0]["name"] == "JELLYFIN_PublishedServerUrl"
    code, _, _ = _migrate("code-server")
    assert code["upstream"]["version"] == {
        "type": "github_release",
        "repo": "coder/code-server",
        "transform": [{"regex": "^v", "replace": ""}],
    }
    assert code["upstream"]["notes"] == {"type": "github_release", "repo": "coder/code-server", "tag": "v${VERSION}"}
    heimdall, _, _ = _migrate("heimdall")
    assert heimdall["upstream"]["version"] == {"type": "github_release", "repo": "linuxserver/Heimdall"}
    assert heimdall["upstream"]["notes"] == {"type": "github_release", "repo": "linuxserver/Heimdall"}


def test_markdown_is_preserved_exactly() -> None:
    readme_vars, _, _ = _load("bookstack")
    manifest, text, _ = _migrate("bookstack")
    assert manifest["docs"]["setup"] == readme_vars["app_setup_block"]
    assert load_yaml_text(text)["docs"]["setup"] == readme_vars["app_setup_block"]
    swag_vars, _, _ = _load("swag")
    _, swag_text, _ = _migrate("swag")
    assert load_yaml_text(swag_text)["docs"]["setup"] == swag_vars["app_setup_block"]


def test_unmapped_keys_are_reported() -> None:
    readme_vars, jenkins_vars, dockerfile = _load("sonarr")
    readme_vars["mystery_key"] = "x"
    _, _, todos = migrate(readme_vars, jenkins_vars, dockerfile)
    assert "unmapped key mystery_key: no equivalent in image.yml" in todos
    assert any(t.startswith("dropped init_diagram") for t in todos)


def test_date_typos_are_flagged() -> None:
    _, _, todos = _migrate("radarr")
    assert any("11.30.20" in t and "MM.DD.YY" in t for t in todos)


def test_escaped_github_expressions_survive_rendering() -> None:
    # emby's external_artifact_check escapes `${{ secrets.CR_PAT }}`; re-rendering it must not raise.
    _, _, todos = _migrate("emby")
    assert any("${{ secrets.CR_USER }}" in t for t in todos)
    assert any(t.startswith("dropped external_artifact_check: rewrite it as upstream.version") for t in todos)


def test_groovy_escapes_are_undoubled() -> None:
    readme_vars, jenkins_vars, dockerfile = _load("sonarr")
    jenkins_vars["custom_version_command"] = (
        "wget -qO- https://x.example/v | jq -r '. | contains( \\\\\"rc\\\\\" )' | sed 's|\\\\b||g'"
    )
    manifest, _, _ = migrate(readme_vars, jenkins_vars, dockerfile)
    assert (
        manifest["upstream"]["version"]["run"]
        == "wget -qO- https://x.example/v | jq -r '. | contains( \\\"rc\\\" )' | sed 's|\\b||g'"
    )


def test_multiarch_false_builds_amd64_only() -> None:
    readme_vars, jenkins_vars, dockerfile = _load("sonarr")
    readme_vars.pop("available_architectures", None)
    jenkins_vars["repo_vars"] = [*jenkins_vars["repo_vars"], "MULTIARCH = 'false'"]
    manifest, _, _ = migrate(readme_vars, jenkins_vars, dockerfile)
    assert manifest["build"]["platforms"] == ["amd64"]


def test_raw_blocks_render_once() -> None:
    readme_vars, jenkins_vars, dockerfile = _load("sonarr")
    inspect = "docker inspect -f '{{ index .Config.Labels \"build_version\" }}' x"
    readme_vars["app_setup_block"] = "{% raw -%}\n" + inspect + "\n{%- endraw %}"
    manifest, _, _ = migrate(readme_vars, jenkins_vars, dockerfile)
    assert '{{ index .Config.Labels "build_version" }}' in manifest["docs"]["setup"]


def test_split_pipeline_respects_quotes() -> None:
    assert split_pipeline("curl -s 'http://a|b' | jq -r '.x | .y' | sed 's|^v||'") == [
        "curl -s 'http://a|b'",
        "jq -r '.x | .y'",
        "sed 's|^v||'",
    ]
    assert split_pipeline("a || b") == ["a || b"]


@pytest.mark.parametrize(
    "command,expected",
    [
        (
            "curl -sX GET https://api.github.com/repos/coder/code-server/releases/latest "
            "| jq -r '.tag_name' | sed 's|^v||'",
            {"type": "github_release", "repo": "coder/code-server", "transform": [{"regex": "^v", "replace": ""}]},
        ),
        (
            "curl -sX GET https://api.github.com/repos/x/y/releases/latest | jq -r '.tag_name'",
            {"type": "github_release", "repo": "x/y"},
        ),
        (
            "curl -sL 'https://api.github.com/repos/x/y/releases' "
            "| jq -r 'first(.[] | select(.prerelease == false) | .tag_name)'",
            {"type": "github_release", "repo": "x/y"},
        ),
        (
            "curl -sL 'https://api.github.com/repos/x/y/releases' | jq -r '.[0] | .tag_name'",
            {"type": "github_release", "repo": "x/y", "prerelease": True},
        ),
        (
            "curl -sX GET 'https://codeberg.org/api/v1/repos/bookstack/bookstack/releases/latest' | jq -r '.tag_name'",
            {"type": "codeberg_release", "repo": "bookstack/bookstack"},
        ),
        (
            "curl -s https://example.org/dists/x/main/binary-amd64/Packages | grep -A 7 -m 1 'Package: foo' | "
            "awk -F ': ' '/Version/{print $2;exit}'",
            {
                "type": "deb_packages",
                "index_url": "https://example.org/dists/x/main/binary-amd64/Packages",
                "package": "foo",
            },
        ),
        (
            "curl -sL https://example.org/v1 | jq -r '.[0].version' | cut -c1-8",
            {
                "type": "json",
                "url": "https://example.org/v1",
                "jq": ".[0].version",
                "transform": [{"regex": "^(.{8}).*$", "replace": "\\1"}],
            },
        ),
    ],
)
def test_version_command_shapes(command: str, expected: dict[str, Any]) -> None:
    source, todo = parse_version_command(command)
    assert source == expected
    assert todo is None


def test_unrecognised_version_command_is_kept_verbatim() -> None:
    command = "docker run --rm alpine sh -c 'echo 1.2.3'"
    source, todo = parse_version_command(command)
    assert source == {"type": "command", "run": command}
    assert todo is not None and "TODO" not in todo and "not recognised" in todo


def test_notes_command_shapes() -> None:
    source, todo = parse_notes_command(
        'curl -H "Authorization: token ${GITHUB_TOKEN}" -s '
        "https://api.github.com/repos/a/b/releases/tags/v${EXT_RELEASE} | jq -r '. |.body' > releasebody.json"
    )
    assert source == {"type": "github_release", "repo": "a/b", "tag": "v${VERSION}"} and todo is None
    source, todo = parse_notes_command(
        "curl -s https://api.github.com/repos/a/b/releases/tags/v${EXT_RELEASE%%+ubu*} "
        "| jq -r '. |.body' > releasebody.json"
    )
    assert (
        source["type"] == "command" and "releasebody.json" not in source["run"] and "${VERSION%%+ubu*}" in source["run"]
    )
    assert todo is not None


def test_cli_writes_image_yml(tmp_path: Path) -> None:
    fixture = next(p for p in LIVE if p.name == "sonarr")
    for name in ("readme-vars.yml", "jenkins-vars.yml"):
        (tmp_path / name).write_text((fixture / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "Dockerfile").write_text((fixture / "Dockerfile.from").read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["migrate", str(tmp_path)]) == 0
    text, _ = migrate_repo(tmp_path)
    assert (tmp_path / "image.yml").read_text(encoding="utf-8") == text
    validate("image", load_yaml(tmp_path / "image.yml"))
    assert text.startswith("# image.yml for docker-sonarr, migrated by `builder migrate`")
