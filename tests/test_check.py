"""`builder check` passes on a fresh repo and fails with file, field and line on every rule it enforces."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from builder.check import Finding, check_repo
from builder.cli import main

from .conftest import SETTINGS, make_repo


def errors(findings: list[Finding]) -> list[str]:
    return [str(f) for f in findings if f.level == "error"]


def warnings(findings: list[Finding]) -> list[str]:
    return [str(f) for f in findings if f.level == "warning"]


def test_fresh_repo_passes(sonarr_repo: Path) -> None:
    assert check_repo(sonarr_repo, SETTINGS) == []
    assert main(["check", str(sonarr_repo)]) == 0


def test_hand_edited_readme_fails(sonarr_repo: Path, capsys: object) -> None:
    readme = sonarr_repo / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8")
    found = errors(check_repo(sonarr_repo, SETTINGS))
    assert found == ["README.md: <file>: differs (run builder render)"]
    assert main(["check", str(sonarr_repo)]) == 1


def test_drift_can_be_a_warning(sonarr_repo: Path) -> None:
    (sonarr_repo / "README.md").write_text("rendered by an older builder\n", encoding="utf-8")
    found = check_repo(sonarr_repo, SETTINGS, drift_level="warning")
    assert errors(found) == [] and warnings(found) == ["warning: README.md: <file>: differs (run builder render)"]
    assert main(["check", "--drift", "warn", str(sonarr_repo)]) == 0
    assert main(["check", str(sonarr_repo)]) == 1


def test_drift_warning_does_not_hide_an_invalid_manifest(sonarr_repo: Path) -> None:
    (sonarr_repo / "image.yml").write_text("name: [not, a, string]\n", encoding="utf-8")
    assert main(["check", "--drift", "warn", str(sonarr_repo)]) == 1


def test_missing_generated_file_fails(sonarr_repo: Path) -> None:
    (sonarr_repo / "AGENTS.md").unlink()
    assert errors(check_repo(sonarr_repo, SETTINGS)) == ["AGENTS.md: <file>: missing (run builder render)"]


def test_render_fixes_drift(sonarr_repo: Path) -> None:
    (sonarr_repo / "README.md").write_text("stale\n", encoding="utf-8")
    assert main(["render", str(sonarr_repo), "--only", "README.md"]) == 0
    assert check_repo(sonarr_repo, SETTINGS) == []


def test_schema_error_reports_field_and_line(sonarr_repo: Path) -> None:
    manifest = sonarr_repo / "image.yml"
    text = manifest.read_text(encoding="utf-8")
    assert "  tag: latest\n" in text
    manifest.write_text(text.replace("  tag: latest\n", "  tag: Not Valid\n", 1), encoding="utf-8")
    lines = manifest.read_text(encoding="utf-8").splitlines()
    line = lines.index("  tag: Not Valid") + 1
    found = check_repo(sonarr_repo, SETTINGS)
    assert len(found) == 1
    assert found[0].level == "error"
    assert found[0].field == "release/tag"
    assert found[0].line == line
    assert str(found[0]).endswith(f"(line {line})")


def test_unknown_key_points_at_its_line(sonarr_repo: Path) -> None:
    manifest = sonarr_repo / "image.yml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\njenkins_vars: {}\n", encoding="utf-8")
    found = check_repo(sonarr_repo, SETTINGS)
    assert found[0].field == "<root>"
    assert "jenkins_vars" in found[0].message
    assert found[0].line == len(manifest.read_text(encoding="utf-8").splitlines())


def test_yaml_syntax_error_reports_line(sonarr_repo: Path) -> None:
    manifest = sonarr_repo / "image.yml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\nbroken: [\n", encoding="utf-8")
    found = check_repo(sonarr_repo, SETTINGS)
    assert len(found) == 1
    assert found[0].message.startswith("YAML error")
    assert found[0].line is not None
    assert main(["check", str(sonarr_repo)]) == 1


def test_dockerfile_differences_warn(sonarr_repo: Path) -> None:
    arm = sonarr_repo / "Dockerfile.aarch64"
    arm.write_text(arm.read_text(encoding="utf-8") + "RUN echo extra\n", encoding="utf-8")
    found = check_repo(sonarr_repo, SETTINGS)
    assert errors(found) == []
    assert any("non-FROM lines differ" in w for w in warnings(found))
    assert main(["check", str(sonarr_repo)]) == 0


def test_missing_dockerfile_fails(sonarr_repo: Path) -> None:
    (sonarr_repo / "Dockerfile.aarch64").unlink()
    assert errors(check_repo(sonarr_repo, SETTINGS)) == [
        "Dockerfile.aarch64: <file>: missing (build.platforms names arm64)"
    ]


def test_s6_scripts_need_header_and_mode(sonarr_repo: Path) -> None:
    run = sonarr_repo / "root/etc/s6-overlay/s6-rc.d/svc-sonarr/run"
    run.write_text("#!/bin/bash\nexec true\n", encoding="utf-8")
    run.chmod(run.stat().st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    found = errors(check_repo(sonarr_repo, SETTINGS))
    assert "root/etc/s6-overlay/s6-rc.d/svc-sonarr/run: mode: not executable" in found
    assert any("expected '#!/usr/bin/with-contenv bash'" in f and "(line 1)" in f for f in found)
    assert any("expected '# shellcheck shell=bash'" in f and "(line 2)" in f for f in found)


def test_changelog_must_be_newest_first(sonarr_repo: Path) -> None:
    manifest = sonarr_repo / "image.yml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace("date: 2026-07-04", "date: 2027-01-01"), encoding="utf-8")
    found = [f for f in check_repo(sonarr_repo, SETTINGS) if f.file.endswith("image.yml")]
    assert len(found) == 1
    assert found[0].field == "changelog/3/date"
    assert found[0].line is not None
    assert "newest first" in found[0].message


def test_kind_base_needs_a_baseimage_name(sonarr_repo: Path) -> None:
    manifest = sonarr_repo / "image.yml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace("\nsummary:", "\nkind: base\nsummary:", 1), encoding="utf-8")
    found = [f for f in check_repo(sonarr_repo, SETTINGS) if f.field == "kind"]
    assert len(found) == 1
    assert "baseimage-" in found[0].message


def test_asset_patterns_need_a_release_source_and_a_valid_regex(sonarr_repo: Path) -> None:
    manifest = sonarr_repo / "image.yml"
    text = manifest.read_text(encoding="utf-8")
    assert "    type: json\n" in text
    block = "    type: json\n    require_assets:\n      - asset: 'x('\n"
    manifest.write_text(text.replace("    type: json\n", block, 1), encoding="utf-8")
    found = [f for f in check_repo(sonarr_repo, SETTINGS) if f.field.startswith("upstream.version.require_assets")]
    assert len(found) == 2
    assert "need one of github_release" in found[0].message and found[0].line is not None
    assert "not a valid regex" in found[1].message


def test_credentials_are_caught(sonarr_repo: Path) -> None:
    secret = sonarr_repo / "root" / "secret.env"
    secret.write_text("TOKEN=ghp_" + "a" * 36 + "\nKEY=AKIAABCDEFGHIJKLMNOP\n", encoding="utf-8")
    (sonarr_repo / "root" / "key.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\nx\n", encoding="utf-8")
    found = errors(check_repo(sonarr_repo, SETTINGS))
    assert "root/secret.env: <content>: looks like a GitHub token (line 1)" in found
    assert "root/secret.env: <content>: looks like a AWS access key (line 2)" in found
    assert "root/key.pem: <content>: looks like a private key (line 1)" in found


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_branch_check_inside_git(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "docker-sonarr")
    _git(repo, "init", "-q", "-b", "master")
    assert check_repo(repo, SETTINGS) == []
    _git(repo, "checkout", "-q", "-b", "feature")
    found = check_repo(repo, SETTINGS)
    assert errors(found) == []
    assert any("release/branch" in w and "dev build" in w for w in warnings(found))
    _git(repo, "checkout", "-q", "-b", "develop")
    found = check_repo(repo, SETTINGS)
    assert any("release/branch" in e and "live branch 'develop'" in e for e in errors(found))


def test_missing_manifest(tmp_path: Path) -> None:
    found = check_repo(tmp_path, SETTINGS)
    assert len(found) == 1 and found[0].message == "file not found"
    assert main(["check", str(tmp_path)]) == 1


def test_stale_donate_txt_fails_and_render_removes_it(sonarr_repo: Path) -> None:
    manifest = sonarr_repo / "image.yml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        text.replace("  sponsor:\n    - { name: Sonarr, url: https://sonarr.tv/donate }\n", ""), encoding="utf-8"
    )
    assert "root/donate.txt: <file>: stale (run builder render)" in errors(check_repo(sonarr_repo, SETTINGS))
    assert ".github/FUNDING.yml: <file>: differs (run builder render)" in errors(check_repo(sonarr_repo, SETTINGS))
    assert main(["render", str(sonarr_repo)]) == 0
    assert not (sonarr_repo / "root" / "donate.txt").exists()
    assert check_repo(sonarr_repo, SETTINGS) == []
