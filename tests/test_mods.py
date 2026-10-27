"""Mod branches: rendering their generated files and converting legacy linuxserver/docker-mods branches."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from builder.cli import main
from builder.manifest import Settings
from builder.mods import (
    convert,
    line_tag_suffixes,
    mod_findings,
    mod_yaml,
    readme_parts,
    render_mod,
    rewrite_registry,
    source_types,
    upstream_lines,
)
from contracts import ContractError, validate

BUILD_IMAGE = """
env:
  GITHUB_REPO: "linuxserver/docker-mods" #don't modify
  ENDPOINT: "linuxserver/mods" #don't modify
  BASEIMAGE: "{base}" #replace
  MODNAME: "{name}" #replace
  MULTI_ARCH: "{multi}" #set to true if needed
jobs:
  set-vars:
    steps:
      - run: |
          MOD_VERSION={version}
"""

README = (
    "# Python3 - Docker mod for code-server/openvscode-server\n\n"
    "This mod adds a python3 dev environment to code-server/openvscode-server, to be installed/updated during "
    "container start.\n\n"
    "In code-server docker arguments, set an environment variable "
    "`DOCKER_MODS=linuxserver/mods:code-server-python3`\n\n"
    "If adding multiple mods, enter them in an array separated by `|`, such as "
    "`DOCKER_MODS=lscr.io/linuxserver/mods:code-server-python3|linuxserver/mods:code-server-mod2`   \n\n"
    "Compose example:\n\n```yaml\nenvironment:\n  - DOCKER_MODS=linuxserver/mods:code-server-python3\n```\n"
)


def branch_files(files: dict[str, str]) -> Callable[[str], str | None]:
    return lambda path: files.get(path)


def legacy(base: str, name: str, multi: str = "false", version: str = '""', readme: str = README) -> dict[str, str]:
    return {
        ".github/workflows/BuildImage.yml": BUILD_IMAGE.format(base=base, name=name, multi=multi, version=version),
        "README.md": readme,
        "Dockerfile": "FROM scratch\nCOPY root/ /\n",
    }


def test_convert_a_regular_branch() -> None:
    result = convert(
        "code-server-python3",
        branch_files(legacy("code-server", "python3")),
        listed_for=["code-server", "openvscode-server"],
        known_targets={"code-server", "openvscode-server", "universal"},
        maintainers=["aptalca"],
    )
    assert result.skipped is None and result.notes == []
    mod = result.mod
    assert mod is not None
    validate("mod", mod)
    assert (mod["name"], mod["target"], mod["title"]) == ("python3", "code-server", "Python3")
    assert mod["summary"].startswith("This mod adds a python3 dev environment")
    assert mod["platforms"] == ["amd64"] and mod["images"] == ["openvscode-server"]
    assert mod["deprecated"] == {"status": False}
    # The summary paragraph is not repeated; the registry and repository point at the playground.
    assert not mod["docs"].startswith("This mod adds") and "If adding multiple mods" not in mod["docs"]
    assert "ghcr.io/linuxserver-playground/mods:code-server-python3" in mod["docs"]
    assert "linuxserver/mods:" not in mod["docs"].replace("linuxserver-playground/mods:", "")
    assert not any(line != line.rstrip() for line in mod["docs"].splitlines())


def test_multi_arch_versioning_and_irregular_names_are_reported() -> None:
    result = convert(
        "nginx-proxy-confs-version",
        branch_files(legacy("nginx", "proxy-confs", multi="true", version='"$(curl -s https://example.org)"')),
        listed_for=[],
        known_targets={"nginx"},
        maintainers=[],
    )
    mod = result.mod
    assert mod is not None
    assert (mod["target"], mod["name"]) == ("nginx", "proxy-confs-version")
    assert mod["platforms"] == ["amd64", "arm64"] and mod["maintainers"] == ["linuxserver"]
    assert any("does not match BuildImage.yml" in n for n in result.notes)
    assert any("needs a lookup of its own" in n for n in result.notes)
    text = mod_yaml(mod, result.notes)
    assert text.startswith("# Schema: ") and "# Converted from linuxserver/docker-mods: " in text
    assert yaml.safe_load(text) == mod


def test_unfinished_tooling_and_unknown_branches_are_skipped() -> None:
    known = {"swag", "universal"}
    placeholders = convert(
        "swag-sablier",
        branch_files(legacy("replace_baseimage", "replace_modname")),
        listed_for=[],
        known_targets=known,
        maintainers=[],
    )
    assert placeholders.skipped and "placeholders" in placeholders.skipped
    assert convert("mod-scripts", branch_files({}), listed_for=[], known_targets=known, maintainers=[]).skipped
    unknown = convert(
        "i16n-package-install",
        branch_files(legacy("universal", "internationalization")),
        listed_for=[],
        known_targets=known,
        maintainers=[],
    )
    assert unknown.skipped and "not <target>-<name>" in unknown.skipped


def test_deprecated_banner_gives_way_to_the_real_title() -> None:
    readme = (
        "# THIS MOD HAS BEEN DEPRECATED - Please use [swag-imagemagick](https://x) instead\n\n"
        "# Imagemagick - Docker mod for nginx/letsencrypt\n\nThis mod adds imagemagick to nginx/letsencrypt.\n"
    )
    result = convert(
        "letsencrypt-imagemagick",
        branch_files(legacy("letsencrypt", "imagemagick", readme=readme)),
        listed_for=[],
        known_targets={"letsencrypt"},
        maintainers=["x"],
    )
    mod = result.mod
    assert mod is not None and mod["title"] == "Imagemagick"
    assert mod["deprecated"]["status"] is True and "swag" in mod["deprecated"]["message"]

    notice = README + "\n## DEPRECATION NOTICE\n\n### This mod is deprecated: the feature is now in the image.\n"
    result = convert(
        "code-server-python3",
        branch_files(legacy("code-server", "python3", readme=notice)),
        listed_for=[],
        known_targets={"code-server"},
        maintainers=["x"],
    )
    assert result.mod is not None
    assert result.mod["deprecated"] == {
        "status": True,
        "message": "This mod is deprecated: the feature is now in the image.",
    }


def test_readme_parts_skip_badges_and_usage_lines() -> None:
    title, summary, _ = readme_parts(
        "# About\n\n[![badge](x)](y)\n\nIn docker arguments, set `DOCKER_MODS=linuxserver/mods:x`\n\n"
        "A real paragraph about the mod.\n"
    )
    assert title == "About" and summary == "A real paragraph about the mod."
    assert (
        rewrite_registry("lscr.io/linuxserver/mods:a and linuxserver/mods:b", "o")
        == "ghcr.io/o/mods:a and ghcr.io/o/mods:b"
    )


def test_render_mod_and_its_check(tmp_path: Path) -> None:
    mod = {
        "schema": 1,
        "name": "tshoot",
        "target": "universal",
        "title": "tshoot",
        "summary": "Adds common troubleshooting tools to any container.",
        "maintainers": ["thelamer"],
        "env": [{"name": "TSHOOT_X", "description": "An example.", "default": "1"}],
        "deprecated": {"status": False},
        "docs": "Run `ping`.\n",
    }
    files = render_mod(mod, Settings())
    readme = files["README.md"]
    assert "DOCKER_MODS=ghcr.io/linuxserver-playground/mods:universal-tshoot" in readme
    assert "any LinuxServer.io container" in readme and "| `TSHOOT_X` | `1` | An example. |" in readme
    assert "https://linuxserver-playground.github.io/site/mods/universal-tshoot/" in readme
    assert "docker-mods branch universal-tshoot" in files["AGENTS.md"]
    assert "mod-build.yml@v1" in files[".github/workflows/build.yml"]

    (tmp_path / "mod.yml").write_text(yaml.safe_dump(mod), encoding="utf-8")
    assert main(["render-mod", str(tmp_path), "--check"]) == 1
    assert main(["render-mod", str(tmp_path)]) == 0
    assert main(["render-mod", str(tmp_path), "--check"]) == 0
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == readme
    assert "## Versions" not in readme and "MOD_VERSION" not in files["AGENTS.md"]


def _versioned_mod() -> dict[str, object]:
    return {
        "schema": 1,
        "name": "docker",
        "target": "universal",
        "summary": "Adds the docker and compose binaries to any container.",
        "maintainers": ["thelamer"],
        "upstream": {
            "url": "https://github.com/moby/moby",
            "version": {
                "type": "combined",
                "sources": [
                    {"type": "github_release", "repo": "moby/moby"},
                    {"type": "github_release", "repo": "docker/compose"},
                ],
            },
        },
    }


def test_an_upstream_block_is_documented_and_checked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mod = _versioned_mod()
    validate("mod", mod)
    files = render_mod(mod, Settings())
    readme = files["README.md"]
    assert "## Versions" in readme and "[upstream](https://github.com/moby/moby)" in readme
    assert "`github_release moby/moby + github_release docker/compose`" in readme
    assert "ghcr.io/linuxserver-playground/mods:universal-docker-<version>" in readme
    assert "`ARG MOD_VERSION`" in files["AGENTS.md"]
    assert (
        files[".github/workflows/build.yml"]
        == render_mod({**mod, "upstream": None}, Settings())[".github/workflows/build.yml"]
    )

    (tmp_path / "mod.yml").write_text(yaml.safe_dump(mod), encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM scratch\nCOPY root/ /\n", encoding="utf-8")
    assert main(["render-mod", str(tmp_path)]) == 1
    assert "no `ARG MOD_VERSION`" in capsys.readouterr().err
    (tmp_path / "Dockerfile").write_text("FROM alpine AS build\nARG MOD_VERSION\nFROM scratch\n", encoding="utf-8")
    assert main(["render-mod", str(tmp_path), "--check"]) == 0


def test_findings_reach_into_combined_sources() -> None:
    mod = _versioned_mod()
    assert mod_findings(mod, None) == []
    sources = mod["upstream"]["version"]["sources"]  # type: ignore[index]
    sources.append({"type": "command", "run": "echo 1"})
    assert source_types(mod["upstream"]["version"]) == ["combined", "github_release", "github_release", "command"]  # type: ignore[index]
    assert mod_findings(mod, "ARG MOD_VERSION\n") == [
        "mod.yml: upstream.version uses command, which the scheduler never runs; use a typed source"
    ]


def _golang_mod() -> dict[str, object]:
    def go(url: str, jq: str) -> dict[str, object]:
        return {"type": "json", "url": url, "jq": jq, "transform": [{"regex": "^go", "replace": ""}]}

    return {
        "schema": 1,
        "name": "golang",
        "target": "code-server",
        "summary": "This mod adds golang/go to code-server.",
        "maintainers": ["thelamer"],
        "upstream": {
            "url": "https://go.dev/",
            "version": go("https://go.dev/dl/?mode=json", ".[0].version"),
            "tags": ["major", "minor"],
            "lines": [
                {"name": "previous", "version": go("https://go.dev/dl/?mode=json", ".[1].version"), "tags": ["minor"]},
                {"name": "unstable", "version": go("https://go.dev/dl/?mode=json&include=all", ".[0].version")},
            ],
        },
    }


def test_version_lines_are_validated_documented_and_dispatchable() -> None:
    mod = _golang_mod()
    validate("mod", mod)
    assert [line["name"] for line in upstream_lines(mod)] == [None, "previous", "unstable"]
    assert upstream_lines(mod)[0]["tags"] == ["major", "minor"] and upstream_lines(mod)[2]["tags"] == []

    files = render_mod(mod, Settings())
    readme, workflow = files["README.md"], files[".github/workflows/build.yml"]
    assert "| default | `json https://go.dev/dl/?mode=json` | `code-server-golang`, `code-server-golang-X`," in readme
    assert (
        "| previous | `json https://go.dev/dl/?mode=json` | `code-server-golang-X.Y`, `code-server-golang-X.Y.Z` |"
        in readme
    )
    assert "| unstable | `json https://go.dev/dl/?mode=json&include=all` | `code-server-golang-X.Y.Z` |" in readme
    assert "(previous, unstable)" in files["AGENTS.md"]
    # Only a mod with lines gets the line input; every other branch keeps the identical file.
    assert "      line:\n" in workflow and "mod_line: ${{ github.event.inputs.line || '' }}" in workflow
    plain = render_mod(_versioned_mod(), Settings())[".github/workflows/build.yml"]
    assert "line" not in plain.replace("pipeline", "")
    tagged = _golang_mod()
    del tagged["upstream"]["lines"]  # type: ignore[attr-defined]
    validate("mod", tagged)
    assert render_mod(tagged, Settings())[".github/workflows/build.yml"] == plain
    assert "| default |" in render_mod(tagged, Settings())["README.md"]

    for bad in ({"name": "Previous", "version": {"type": "pip", "package": "x"}}, {"name": "x"}):
        broken = _golang_mod()
        broken["upstream"]["lines"] = [bad]  # type: ignore[index]
        with pytest.raises(ContractError):
            validate("mod", broken)
    broken = _golang_mod()
    broken["upstream"]["tags"] = ["patch"]  # type: ignore[index]
    with pytest.raises(ContractError):
        validate("mod", broken)


def test_line_findings_and_tag_suffixes() -> None:
    mod = _golang_mod()
    assert mod_findings(mod, "ARG MOD_VERSION\n") == []
    lines = mod["upstream"]["lines"]  # type: ignore[index]
    lines.append({"name": "previous", "version": {"type": "command", "run": "echo 1"}})
    assert mod_findings(mod, "ARG MOD_VERSION\n") == [
        "mod.yml: upstream.lines[previous].version uses command, which the scheduler never runs; use a typed source",
        "mod.yml: upstream.lines repeats previous; line names are unique",
    ]
    assert line_tag_suffixes("1.27.3", ["major", "minor"]) == ["1", "1.27"]
    assert line_tag_suffixes("1.26.10", ["minor"]) == ["1.26"]
    assert line_tag_suffixes("1.27rc3", ["minor"]) == []
    assert line_tag_suffixes("1.27", ["major", "minor"]) == ["1"]
    assert line_tag_suffixes("20260101", ["major", "minor"]) == []
    assert line_tag_suffixes("1.27.3", []) == []
