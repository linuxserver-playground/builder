"""
A base image's layer digests per architecture, read anonymously from its registry (GHCR or Docker Hub), so a build's
own layers can be told from the ones it inherited. The HTTP GET is injectable: the build uses the standard library,
the hub its own client with its retries.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

Json = dict[str, Any]
Get = Callable[[str, dict[str, str], dict[str, str]], Json]
"""(url, query parameters, headers) -> the JSON body; raises on any HTTP or network error."""

ARCHES = ("amd64", "arm64")
GHCR = "ghcr.io"
DOCKER_HUB = "registry-1.docker.io"
DOCKER_HUB_ALIASES = {"docker.io", "index.docker.io", DOCKER_HUB}
"""Docker Hub's names in an image reference; its registry API is on registry-1 and its tokens come from auth."""
MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
"""What is accepted for a manifest: an index (per-platform manifests) or one image manifest."""
USER_AGENT = "lsio-builder (+https://github.com/linuxserver-playground/builder)"


def urllib_get(url: str, params: dict[str, str], headers: dict[str, str], *, retries: int = 3) -> Json:
    """GET with the standard library, retrying network errors, 429 and 5xx with a short backoff."""
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    for attempt in range(retries + 1):
        request = urllib.request.Request(full, headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body: Json = json.loads(response.read())
                return body
        except urllib.error.HTTPError as e:
            if attempt == retries or (e.code != 429 and e.code < 500):
                raise
        except urllib.error.URLError:
            if attempt == retries:
                raise
        time.sleep(1.5 * 2**attempt)
    raise AssertionError("unreachable")


def parse_reference(image: str) -> tuple[str, str, str]:
    """
    `ghcr.io/linuxserver/baseimage-alpine:3.24` to (registry, repository, tag). A bare `owner/name` defaults to
    GHCR; Docker Hub references (`docker.io/alpine`, or a bare `alpine`) go to its registry host, official images
    under `library/`.
    """
    first, _, rest = image.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    elif not rest:
        registry, path = DOCKER_HUB, image
    else:
        registry, path = GHCR, image
    if registry in DOCKER_HUB_ALIASES:
        registry = DOCKER_HUB
        if "/" not in path.split("@")[0].split(":")[0]:
            path = f"library/{path}"
    if "@" in path:
        path, _, tag = path.partition("@")
    else:
        name, _, tag = path.rpartition(":")
        if name and "/" not in tag:
            path = name
        else:
            tag = "latest"
    return registry, path, tag


def token(get: Get, registry: str, repository: str) -> str:
    """An anonymous pull token."""
    scope = f"repository:{repository}:pull"
    if registry == DOCKER_HUB:
        body = get("https://auth.docker.io/token", {"service": "registry.docker.io", "scope": scope}, {})
    else:
        body = get(f"https://{registry}/token", {"scope": scope}, {})
    return str(body["token"])


def base_layers(image: str, digest: str | None, get: Get = urllib_get) -> Json:
    """
    The compressed layer digests of a base image, per architecture, as the registry publishes them:
    `{"image", "digest", "platforms": {arch: {"digest", "layers": [...]}}}`. The build's `base.digest` is the
    digest of the index the build pulled, so it is fetched when given (a tag may have moved on since); a
    single-platform manifest is recorded for its one architecture.
    """
    registry, repository, tag = parse_reference(image)
    headers = {"Authorization": f"Bearer {token(get, registry, repository)}", "Accept": ", ".join(MANIFEST_TYPES)}

    def manifest(reference: str) -> Json:
        return get(f"https://{registry}/v2/{repository}/manifests/{reference}", {}, headers)

    root = manifest(digest or tag)
    platforms: dict[str, Json] = {}
    if "manifests" in root:
        for entry in root["manifests"]:
            platform = entry.get("platform") or {}
            arch = platform.get("architecture")
            if platform.get("os") != "linux" or arch not in ARCHES or arch in platforms:
                continue
            layers = manifest(entry["digest"])["layers"]
            platforms[arch] = {"digest": entry["digest"], "layers": [layer["digest"] for layer in layers]}
    elif "layers" in root:
        config = root.get("config", {})
        arch = str(config.get("platform", {}).get("architecture") or "amd64")
        platforms[arch] = {"digest": digest, "layers": [layer["digest"] for layer in root["layers"]]}
    return {"image": image, "digest": digest, "platforms": dict(sorted(platforms.items()))}
