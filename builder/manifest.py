"""
Load ``image.yml`` with line numbers, validate it against the contract and turn it into the flat context the
templates and snippet builders consume (defaults applied, required parameters before optional ones).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from contracts import errors as contract_errors
from contracts import load_yaml

Json = dict[str, Any]
PathTuple = tuple[str | int, ...]

COMMON_ENV = [
    {"name": "PUID", "value": "1000", "description": "for UserID - see below for explanation"},
    {"name": "PGID", "value": "1000", "description": "for GroupID - see below for explanation"},
    {
        "name": "TZ",
        "value": "Etc/UTC",
        "description": "specify a timezone to use, see this "
        "[list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones#List).",
    },
]

ARCH_TAGS = {"amd64": "amd64", "arm64": "arm64v8"}
ARCH_LABELS = {"amd64": "x86-64", "arm64": "arm64"}


@dataclass(frozen=True)
class Settings:
    """Where the generated files point. Every value can be overridden with a ``BUILDER_*`` environment variable."""

    org: str = "linuxserver-playground"
    registry: str = "ghcr.io"
    control_url: str = "https://ci.thelamer.duckdns.org:8443"
    docs_url: str = "https://linuxserver-playground.github.io/docs"
    hub_url: str = "https://linuxserver-playground.github.io/hub"
    blog_url: str = "https://blog.linuxserver.io"
    upstream_org: str = "linuxserver"
    schema_url: str = "https://github.com/linuxserver-playground/builder/blob/main/schemas/image.schema.json"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        overrides = {
            name: env[f"BUILDER_{name.upper()}"]
            for name in cls.__dataclass_fields__
            if f"BUILDER_{name.upper()}" in env
        }
        return cls(**overrides)


@dataclass
class ManifestError(Exception):
    """A manifest problem with enough detail for ``file: field: message (line N)``."""

    file: str
    field: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        where = f" (line {self.line})" if self.line else ""
        return f"{self.file}: {self.field}: {self.message}{where}"


@dataclass
class Manifest:
    data: Json
    path: Path
    lines: dict[PathTuple, int] = field(default_factory=dict)

    def line_of(self, json_path: PathTuple) -> int | None:
        """Line of the deepest known node on the path (schema errors may point at a missing child)."""
        for end in range(len(json_path), -1, -1):
            if json_path[:end] in self.lines:
                return self.lines[json_path[:end]]
        return None


class _Loader(yaml.SafeLoader):
    """Keeps dates as strings, like contracts.load_yaml."""


def _str_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    return str(loader.construct_scalar(node))  # type: ignore[arg-type]


_Loader.add_constructor("tag:yaml.org,2002:timestamp", _str_constructor)


def _walk(node: yaml.Node, path: PathTuple, lines: dict[PathTuple, int]) -> None:
    lines[path] = node.start_mark.line + 1
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, yaml.ScalarNode) else str(key_node)
            _walk(value_node, (*path, key), lines)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _walk(item, (*path, index), lines)


def load_manifest_text(text: str) -> Any:
    """Parse manifest YAML from a string, dates kept as strings."""
    return yaml.load(text, Loader=_Loader)


def load_manifest(path: str | Path) -> Manifest:
    """Parse image.yml, remembering the line of every node. Raises ManifestError on YAML syntax errors."""
    path = Path(path)
    if not path.is_file():
        raise ManifestError(str(path), "<root>", "file not found")
    text = path.read_text(encoding="utf-8")
    loader = _Loader(text)
    try:
        node = loader.get_single_node()
        data = loader.construct_document(node) if node is not None else None
    except yaml.MarkedYAMLError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark else None
        raise ManifestError(str(path), "<root>", f"YAML error: {exc.problem or exc}", line) from exc
    finally:
        loader.dispose()
    if not isinstance(data, dict):
        raise ManifestError(str(path), "<root>", "manifest must be a mapping", 1)
    lines: dict[PathTuple, int] = {}
    if node is not None:
        _walk(node, (), lines)
    return Manifest(data, path, lines)


def validate_manifest(manifest: Manifest) -> list[ManifestError]:
    """Schema violations as ManifestErrors with line numbers."""
    found = []
    for error in contract_errors("image", manifest.data):
        json_path: PathTuple = tuple(error.absolute_path)
        field_name = "/".join(str(p) for p in json_path) or "<root>"
        line = manifest.line_of(json_path)
        if str(error.validator) == "additionalProperties":
            # Point at the offending key rather than the mapping that holds it.
            unexpected = re.findall(r"'([^']+)'", error.message)
            lines = [manifest.lines[(*json_path, key)] for key in unexpected if (*json_path, key) in manifest.lines]
            line = min(lines) if lines else line
        found.append(ManifestError(str(manifest.path), field_name, error.message, line))
    return found


def load_valid(path: str | Path) -> Manifest:
    manifest = load_manifest(path)
    problems = validate_manifest(manifest)
    if problems:
        raise problems[0]
    return manifest


def read_manifest_data(path: str | Path) -> Json:
    data: Json = load_yaml(path)
    return data


# --- Normalisation -------------------------------------------------------------------------------------------


def _optional(item: Json) -> bool:
    return bool(item.get("optional", False))


def _sorted(items: list[Json]) -> list[Json]:
    """Required parameters first, then optional ones, both in manifest order."""
    return [i for i in items if not _optional(i)] + [i for i in items if _optional(i)]


def normalize_runtime(runtime: Json, name: str) -> Json:
    ports = []
    for p in runtime.get("ports", []):
        number = str(p["internal"]).split("/")[0]
        ports.append(
            {
                "internal": str(p["internal"]),
                "external": str(p.get("external", number)),
                "description": p["description"],
                "name": p.get("name", ""),
                "optional": _optional(p),
            }
        )
    volumes = []
    for v in runtime.get("volumes", []):
        last = v["path"].rstrip("/").rsplit("/", 1)[-1] or "data"
        volumes.append(
            {
                "path": v["path"],
                "host_path": v.get("host_path", f"/path/to/{name}/{last}"),
                "mode": v.get("mode", "rw"),
                "description": v["description"],
                "name": v.get("name", ""),
                "optional": _optional(v),
                "default_host_path": bool(v.get("default_host_path", False)),
            }
        )
    common_env = runtime.get("common_env", True)
    env = []
    for e in runtime.get("env", []):
        if common_env and e["name"] == "TZ":
            continue
        env.append(
            {
                "name": e["name"],
                "value": str(e.get("value", "")),
                "description": e["description"],
                "title": e.get("title", ""),
                "options": list(e.get("options", [])),
                "optional": _optional(e),
                "secret": e.get("secret", any(k in e["name"] for k in ("TOKEN", "PASS", "KEY"))),
            }
        )
    devices = [
        {
            "path": d["path"],
            "host_path": d.get("host_path", d["path"]),
            "description": d["description"],
            "name": d.get("name", ""),
            "optional": _optional(d),
        }
        for d in runtime.get("devices", [])
    ]
    cap_add = [
        {"cap": c["cap"], "description": c.get("description", ""), "optional": _optional(c)}
        for c in runtime.get("cap_add", [])
    ]
    security_opt = [
        {"run": s["run"], "compose": s["compose"], "description": s["description"], "optional": _optional(s)}
        for s in runtime.get("security_opt", [])
    ]
    custom = []
    for c in runtime.get("custom", []):
        value = c["value"]
        custom.append(
            {
                "flag": c["flag"],
                "compose": c["compose"],
                "values": list(value) if isinstance(value, list) else [value],
                "is_array": isinstance(value, list),
                "description": c["description"],
                "optional": _optional(c),
            }
        )
    readonly = {"supported": False, "notes": "", **runtime.get("readonly", {})}
    nonroot = {"supported": False, "notes": "", **runtime.get("nonroot", {})}
    return {
        "container_name": runtime.get("container_name", name),
        "common_env": common_env,
        "common_env_vars": COMMON_ENV,
        "network": {"optional": False, "description": "", **runtime["network"]} if runtime.get("network") else None,
        "hostname": {"optional": False, **runtime["hostname"]} if runtime.get("hostname") else None,
        "mac_address": {"optional": False, **runtime["mac_address"]} if runtime.get("mac_address") else None,
        "privileged": runtime.get("privileged", False),
        "ports": _sorted(ports),
        "volumes": _sorted(volumes),
        "env": _sorted(env),
        "devices": _sorted(devices),
        "cap_add": _sorted(cap_add),
        "security_opt": _sorted(security_opt),
        "custom": _sorted(custom),
        "readonly": readonly,
        "nonroot": nonroot,
        "uses_sysctl": any(c["flag"] == "sysctl" for c in custom),
    }


def build_context(data: Json, settings: Settings | None = None) -> Json:
    """Everything a template or snippet builder needs, derived once from the manifest."""
    settings = settings or Settings.from_env()
    name = data["name"]
    release = data["release"]
    tag = release["tag"]
    build = data["build"]
    docs = data.get("docs", {})
    notices = {
        "hwaccel": False,
        "keyboard": False,
        "media": False,
        "seccomp": False,
        "strict_proxy": False,
        "kasm": False,
        **docs.get("notices", {}),
    }
    selkies = notices.pop("selkies", None) or {}
    has_notices = any(notices.values())
    platforms = list(build.get("platforms", []))
    repo = f"docker-{name}"
    repo_url = f"https://github.com/{settings.org}/{repo}"
    image = f"{settings.registry}/{settings.org}/{name}"
    tags = [
        {
            "tag": t["tag"],
            "description": t["description"],
            "deprecated": bool(t.get("deprecated", False)),
            "unraid_extra": t.get("unraid_extra", ""),
        }
        for t in release.get("tags", [])
    ]
    live_branches = [release["branch"]]
    for t in tags:
        if t["tag"] not in live_branches and t["tag"] != "latest":
            live_branches.append(t["tag"])
    test = data.get("test", {})
    runtime = normalize_runtime(data.get("runtime", {}), name)
    required_ports = [p["external"] for p in runtime["ports"] if not p["optional"]]
    test_ports = required_ports or ([str(test["web"]["port"])] if test.get("web") else [])
    summary = data["summary"].strip()
    return {
        "m": data,
        "settings": settings,
        "org": settings.org,
        "name": name,
        "title": data.get("title") or name.capitalize(),
        "summary": summary,
        "summary_oneline": " ".join(summary.split("\n\n")[0].split()),
        "extras": list(data.get("extras", [])),
        "repo": repo,
        "repo_url": repo_url,
        "image": image,
        "image_ref": f"{image}:{tag}",
        "release_tag": tag,
        "release_branch": release["branch"],
        "release_channel": release.get("channel", "stable"),
        "tags": tags,
        "has_latest": not tags or any(t["tag"] == "latest" for t in tags),
        "live_branches": live_branches,
        "platforms": platforms,
        "arch_rows": [
            {"label": ARCH_LABELS[a], "available": a in platforms, "tag": f"{ARCH_TAGS[a]}-\\<version tag\\>"}
            for a in ("amd64", "arm64")
        ],
        "base": build["base"],
        "is_alpine": build["base"].startswith("alpine"),
        "version_arg": build.get("version_arg", "VERSION"),
        "first_party": build.get("first_party", True),
        "test": test,
        "test_ports": test_ports,
        "upstream": data["upstream"],
        "project_url": data["upstream"].get("url", repo_url),
        "rt": runtime,
        "docs": docs,
        "logo": docs.get("logo", ""),
        "setup": docs.get("setup", ""),
        "optional_parameters": docs.get("optional_parameters", ""),
        "notices": notices,
        "has_notices": has_notices,
        "selkies": {"enabled": True, "nvidia": bool(selkies.get("nvidia", False))}
        if selkies.get("enabled", bool(selkies))
        else {},
        "selkies_https_port": selkies.get("https_port", 3001),
        "selkies_http_port": selkies.get("http_port", 3000),
        "external_application": docs.get("external_application"),
        "custom_readme": docs.get("custom_readme"),
        "proxy": data.get("proxy"),
        "deprecation": {"status": False, "message": "", **data.get("deprecation", {})},
        "changelog": list(data.get("changelog", [])),
        "docs_page_url": f"{settings.docs_url}/images/{repo}",
        "control_url": settings.control_url,
        "ci_badge_url": f"{settings.control_url}/badge/{name}/{tag}.svg",
        "ci_page_url": f"{settings.control_url}/images/{name}/{tag}",
        "hub_url": settings.hub_url,
        "mods_url": f"{settings.hub_url}/mods/?image={name}",
        "universal_mods_url": f"{settings.hub_url}/mods/?image=universal",
        "blog_url": settings.blog_url,
        "docs_url": settings.docs_url,
        "schema_url": settings.schema_url,
        "badge_params": "color=94398d&labelColor=555555&logoColor=ffffff&style=for-the-badge",
    }
