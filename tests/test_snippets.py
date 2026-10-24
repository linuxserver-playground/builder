"""The compose and docker run snippets match the structure of today's live sonarr README."""

from __future__ import annotations

import re
from typing import Any

import yaml

from builder.cli import main
from builder.manifest import read_manifest_data
from builder.snippets import as_dict, as_json, compose, docker_run

from .conftest import EXAMPLES, FIXTURES, SETTINGS

LIVE_SONARR = (FIXTURES / "live" / "sonarr" / "docker-sonarr.md").read_text(encoding="utf-8")


def _block(text: str, lang: str) -> str:
    match = re.search(rf"```{lang}\n(.*?)```", text, re.DOTALL)
    assert match, f"no {lang} block"
    return match.group(1)


def _sonarr() -> dict[str, Any]:
    data: dict[str, Any] = read_manifest_data(EXAMPLES / "sonarr" / "image.yml")
    return data


def _strip_optional(items: list[str]) -> list[str]:
    return [re.sub(r"\s*#optional$", "", i) for i in items]


def test_compose_matches_live_structure() -> None:
    ours = yaml.safe_load(compose(_sonarr(), SETTINGS))
    live = yaml.safe_load(_block(LIVE_SONARR, "yaml"))
    assert list(ours["services"]) == list(live["services"]) == ["sonarr"]
    mine, theirs = ours["services"]["sonarr"], live["services"]["sonarr"]
    assert mine["environment"] == theirs["environment"]
    assert mine["volumes"] == theirs["volumes"]
    assert mine["ports"] == theirs["ports"]
    assert mine["container_name"] == theirs["container_name"]
    assert mine["restart"] == theirs["restart"]
    assert mine["image"] == "ghcr.io/linuxserver-playground/sonarr:latest"
    raw = compose(_sonarr(), SETTINGS)
    assert "/path/to/tvseries:/tv #optional" in raw


def test_docker_run_matches_live_structure() -> None:
    ours = docker_run(_sonarr(), SETTINGS)
    live = _block(LIVE_SONARR, "bash")

    def args(text: str) -> list[str]:
        return [
            line.strip().rstrip(" \\").replace(" `#optional`", "")
            for line in text.splitlines()
            if line.startswith("  ")
            and not line.strip().startswith("lscr.io")
            and not line.strip().startswith("ghcr.io")
        ]

    assert args(ours) == args(live)
    assert ours.splitlines()[-1] == "  ghcr.io/linuxserver-playground/sonarr:latest"
    assert "-v /path/to/tvseries:/tv `#optional` \\" in ours


def test_required_only_variants_drop_optional_parameters() -> None:
    data = _sonarr()
    required = compose(data, SETTINGS, full=False)
    assert "#optional" not in required
    assert "/tv" not in required and "/config" in required
    run = docker_run(data, SETTINGS, full=False)
    assert "#optional" not in run and "/downloads" not in run


def test_swag_snippets_cover_every_parameter_kind() -> None:
    data = read_manifest_data(EXAMPLES / "swag" / "image.yml")
    text = compose(data, SETTINGS)
    assert "    cap_add:\n      - NET_ADMIN\n" in text
    assert "      - 80:80 #optional" in text
    assert "      - URL=example.com" in text
    run = docker_run(data, SETTINGS)
    assert "  --cap-add=NET_ADMIN \\" in run
    assert "  -p 80:80 `#optional` \\" in run


def test_custom_params_hostname_and_privileged() -> None:
    data = _sonarr()
    data["runtime"]["hostname"] = {"value": "sonarr-host", "description": "x", "optional": True}
    data["runtime"]["privileged"] = "optional"
    data["runtime"]["custom"] = [
        {"flag": "shm-size", "compose": "shm_size", "value": "1gb", "description": "x"},
        {
            "flag": "sysctl",
            "compose": "sysctls",
            "value": ["net.ipv4.ip_forward=1"],
            "description": "y",
            "optional": True,
        },
    ]
    data["runtime"]["network"] = {"mode": "host", "description": "host"}
    data["runtime"]["env"] = [{"name": "MY_VAR", "value": "a b", "description": "z"}]
    text = compose(data, SETTINGS)
    assert "    hostname: sonarr-host #optional" in text
    assert "    privileged: true #optional" in text
    assert '    shm_size: "1gb"' in text
    assert "    sysctls: #optional\n      - net.ipv4.ip_forward=1 #optional" in text
    assert "    network_mode: host" in text
    assert '      - "MY_VAR=a b"' in text
    run = docker_run(data, SETTINGS)
    assert "  --hostname=sonarr-host `#optional` \\" in run
    assert '  --shm-size="1gb" \\' in run
    assert "  --net=host \\" in run
    assert '  -e MY_VAR="a b" \\' in run


def test_external_application_is_appended() -> None:
    data = _sonarr()
    data["docs"]["external_application"] = {"cli": "docker run -d mariadb", "compose": "  mariadb:\n    image: mariadb"}
    assert compose(data, SETTINGS).endswith(
        "# This container requires an external application to be run separately.\n  mariadb:\n    image: mariadb\n"
    )
    assert docker_run(data, SETTINGS).endswith("docker run -d mariadb\n")


def test_json_form() -> None:
    data = as_dict(_sonarr(), SETTINGS)
    assert data["image"] == "ghcr.io/linuxserver-playground/sonarr:latest"
    assert [v["path"] for v in data["runtime"]["volumes"]] == ["/config", "/tv", "/downloads"]
    assert data["runtime"]["volumes"][0]["host_path"] == "/path/to/sonarr/data"
    assert data["compose"]["full"] == compose(_sonarr(), SETTINGS)
    assert data["run"]["required"] == docker_run(_sonarr(), SETTINGS, full=False)
    assert as_json(_sonarr(), SETTINGS).startswith("{\n")


def test_cli_snippets(sonarr_repo: Any, capsys: Any) -> None:
    assert main(["snippets", str(sonarr_repo), "--format", "run", "--required-only"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("docker run -d \\") and "#optional" not in out
    assert main(["snippets", str(sonarr_repo), "--format", "json"]) == 0
    assert '"name": "sonarr"' in capsys.readouterr().out
