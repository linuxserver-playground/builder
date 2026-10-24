"""Shared fixtures: a fake image repository built from an example manifest."""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

import pytest

from builder.manifest import Settings, read_manifest_data
from builder.render import render_all, write_files

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"
LIVE = sorted(p for p in (FIXTURES / "live").iterdir() if p.is_dir())

SETTINGS = Settings()

BASE_TAGS = {
    "alpine": "baseimage-alpine:3.24",
    "alpine-nginx": "baseimage-alpine-nginx:3.24",
    "ubuntu": "baseimage-ubuntu:noble",
}


def make_repo(target: Path, example: str = "sonarr", *, rendered: bool = True) -> Path:
    """Copy an example manifest into target and add the hand-edited files a real repo has."""
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(EXAMPLES / example / "image.yml", target / "image.yml")
    data = read_manifest_data(target / "image.yml")
    base = BASE_TAGS.get(data["build"]["base"], "baseimage-alpine:3.24")
    body = (
        "\n# set version label\nARG BUILD_DATE\nARG VERSION\n"
        f"ARG {data['build'].get('version_arg', 'VERSION')}\n"
        'LABEL build_version="Linuxserver.io version:- ${VERSION} Build-date:- ${BUILD_DATE}"\n\n'
        'RUN \\\n  echo "**** install packages ****" && \\\n  apk add --no-cache curl\n\nCOPY root/ /\n'
    )
    (target / "Dockerfile").write_text(f"FROM ghcr.io/linuxserver/{base}\n{body}", encoding="utf-8")
    name, tag = base.split(":")
    (target / "Dockerfile.aarch64").write_text(
        f"FROM ghcr.io/linuxserver/{name}:arm64v8-{tag}\n{body}", encoding="utf-8"
    )
    svc = target / "root/etc/s6-overlay/s6-rc.d" / f"svc-{data['name']}"
    svc.mkdir(parents=True, exist_ok=True)
    run = svc / "run"
    run.write_text(
        "#!/usr/bin/with-contenv bash\n# shellcheck shell=bash\n\nexec s6-setuidgid abc /app/bin\n", encoding="utf-8"
    )
    run.chmod(run.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (svc / "type").write_text("longrun\n", encoding="utf-8")
    if rendered:
        write_files(target, render_all(data, SETTINGS))
    return target


@pytest.fixture
def sonarr_repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "docker-sonarr")
