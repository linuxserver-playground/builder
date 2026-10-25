"""
`builder migrate`: turn a legacy ``readme-vars.yml`` + ``jenkins-vars.yml`` pair into ``image.yml``.

Jinja placeholders in the legacy files (``{{ project_name }}``, ``{{ arch_x86_64 }}`` ...) are rendered with the
vendored variables in :mod:`builder.legacy_vars`. Markdown text is carried over byte for byte. Anything that has no
typed equivalent becomes a ``command`` source or a ``# TODO`` comment, and unmapped keys are reported.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from jinja2 import DebugUndefined, Environment, UndefinedError

from contracts import errors as contract_errors
from contracts import format_error, load_yaml

from .legacy_vars import COMMON, DEFAULTS, DROPPED

Json = dict[str, Any]

SECTION_ORDER = (
    "schema",
    "name",
    "title",
    "summary",
    "extras",
    "upstream",
    "release",
    "build",
    "test",
    "runtime",
    "docs",
    "proxy",
    "publish",
    "deprecation",
    "changelog",
)

BASE_FROM_IMAGE = {
    "alpine-nginx": "alpine-nginx",
    "alpine": "alpine",
    "ubuntu": "ubuntu",
    "debian": "debian",
    "fedora": "fedora",
    "arch": "arch",
    "el": "el",
    "selkies": "selkies",
    "kasmvnc": "kasmvnc",
    "rdesktop": "rdesktop",
}
BASE_ENUM = {
    "alpine",
    "alpine-nginx",
    "ubuntu",
    "debian",
    "fedora",
    "arch",
    "el",
    "selkies",
    "kasmvnc",
    "rdesktop",
    "scratch",
}

_URL = re.compile(r"""https?://[^\s'"]+""")
_GITHUB_LATEST = re.compile(r"api\.github\.com/repos/([^/]+)/([^/]+)/releases/latest/?$")
_GITHUB_RELEASES = re.compile(r"api\.github\.com/repos/([^/]+)/([^/]+)/releases/?$")
_GITHUB_TAG = re.compile(r"api\.github\.com/repos/([^/]+)/([^/]+)/releases/tags/(.+)$")
_CODEBERG_LATEST = re.compile(r"codeberg\.org/api/v1/repos/([^/]+)/([^/]+)/releases/latest/?$")
_CODEBERG_TAG = re.compile(r"codeberg\.org/api/v1/repos/([^/]+)/([^/]+)/releases/tags/(.+)$")
_JQ = re.compile(r"""^jq\s+(?:-\w+\s+)*'(.*)'$""", re.DOTALL)
_SED = re.compile(r"""^sed\s+(?:-e\s+)?'s(.)(.*?)\1(.*?)\1g?'$""")
_AWK = re.compile(r"""^awk\s+-F\s*'([^']+)'\s+'\{print \$(\d+)\}'$""")
_CUT = re.compile(r"^cut\s+-c\s*1-(\d+)$")
_GREP_PACKAGE = re.compile(r"""grep\b.*'Package:\s*([^']+)'""")
_REPO_VAR = re.compile(r"""^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:'([^']*)'|"([^"]*)"|(\S*))\s*$""")
_LEGACY_DATE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2}):?$")
_SHELL_EXPANSION = re.compile(r"\$\{[A-Z_]+[^}]*[%#/:^,][^}]*\}")


def split_pipeline(command: str) -> list[str]:
    """Split a shell command on unquoted single pipes."""
    stages: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < len(command):
                i += 1
                current.append(command[i])
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch == "|" and command[i + 1 : i + 2] != "|" and command[i - 1 : i] != "|":
            stages.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    stages.append("".join(current).strip())
    return [s for s in stages if s]


def _render_legacy(raw: Json) -> tuple[Json, list[str]]:
    """Resolve the Jinja placeholders in every string, repeatedly, against the vendored defaults and the file itself."""
    env = Environment(undefined=DebugUndefined, keep_trailing_newline=True, autoescape=False)
    ctx: Json = {**COMMON, **DEFAULTS, **raw}
    unresolved: list[str] = []

    def render(value: Any) -> Any:
        if isinstance(value, str):
            if "{{" in value or "{%" in value:
                try:
                    return env.from_string(value).render(ctx)
                except UndefinedError:
                    # An escaped expression rendered on an earlier pass, e.g. `${{ secrets.CR_PAT }}`: keep it literal.
                    return value
            return value
        if isinstance(value, list):
            return [render(v) for v in value]
        if isinstance(value, dict):
            return {k: render(v) for k, v in value.items()}
        return value

    for _ in range(6):
        rendered = render(ctx)
        if rendered == ctx:
            break
        ctx = rendered
    for key, value in ctx.items():
        if key in raw and isinstance(value, str) and "{{" in value and "{% raw" not in str(raw.get(key, "")):
            unresolved.append(f"{key}: unresolved placeholder in {value[:60]!r}")
    return ctx, unresolved


def parse_repo_vars(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        match = _REPO_VAR.match(str(line))
        if match:
            out[match.group(1)] = next((g for g in match.groups()[1:] if g is not None), "")
    return out


def parse_transform_stage(stage: str) -> Json | None:
    """A sed/awk/cut stage as a regex transform, or None when it is not one of the shapes we know."""
    if match := _SED.match(stage):
        pattern = match.group(2).replace(r"\(", "(").replace(r"\)", ")").replace(r"\{", "{").replace(r"\}", "}")
        return {"regex": pattern, "replace": match.group(3)}
    if match := _AWK.match(stage):
        sep, field = re.escape(match.group(1)).replace("\\-", "-"), int(match.group(2))
        return {"regex": f"^(?:[^{sep}]*{sep}){{{field - 1}}}([^{sep}]*).*$", "replace": r"\1"}
    if match := _CUT.match(stage):
        return {"regex": f"^(.{{{match.group(1)}}}).*$", "replace": r"\1"}
    return None


def _jq_expr(stage: str) -> str | None:
    match = _JQ.match(stage)
    return match.group(1) if match else None


def _with_transforms(source: Json, stages: list[str]) -> Json | None:
    transforms = []
    for stage in stages:
        transform = parse_transform_stage(stage)
        if transform is None:
            return None
        transforms.append(transform)
    if transforms:
        source["transform"] = transforms
    return source


def parse_version_command(command: str) -> tuple[Json, str | None]:
    """Recognise the common custom_version_command shapes; otherwise keep the command verbatim with a TODO."""
    fallback = ({"type": "command", "run": command.strip()}, "version command not recognised; prefer a typed source")
    stages = split_pipeline(command.strip())
    if not stages or not stages[0].startswith("curl") or len(stages) < 2:
        return fallback
    url_match = _URL.search(stages[0])
    if not url_match:
        return fallback
    url = url_match.group(0)
    jq = _jq_expr(stages[1])
    rest = stages[2:]
    if jq is not None:
        if (m := _GITHUB_LATEST.search(url)) and "tag_name" in jq:
            typed = _with_transforms({"type": "github_release", "repo": f"{m.group(1)}/{m.group(2)}"}, rest)
            return (typed, None) if typed else fallback
        if (m := _GITHUB_RELEASES.search(url)) and "tag_name" in jq:
            source: Json = {"type": "github_release", "repo": f"{m.group(1)}/{m.group(2)}"}
            if "prerelease == false" in jq.replace(" ", "") or "prerelease==false" in jq.replace(" ", ""):
                pass
            elif ".[0]" in jq:
                source["prerelease"] = True
            else:
                return fallback
            typed = _with_transforms(source, rest)
            return (typed, None) if typed else fallback
        if (m := _CODEBERG_LATEST.search(url)) and "tag_name" in jq:
            typed = _with_transforms({"type": "codeberg_release", "repo": f"{m.group(1)}/{m.group(2)}"}, rest)
            return (typed, None) if typed else fallback
        typed = _with_transforms({"type": "json", "url": url, "jq": jq}, rest)
        return (typed, None) if typed else fallback
    if url.rstrip("/").endswith("Packages") and (m := _GREP_PACKAGE.search(stages[1])):
        return {"type": "deb_packages", "index_url": url, "package": m.group(1).strip()}, None
    return fallback


def parse_notes_command(command: str) -> tuple[Json, str | None]:
    text = command.strip()
    text = re.sub(r"\s*>\s*releasebody\.json\s*$", "", text)
    stages = split_pipeline(text)
    if len(stages) == 2 and stages[0].startswith("curl"):
        url_match = _URL.search(stages[0])
        jq = _jq_expr(stages[1])
        if url_match and jq is not None and jq.replace(" ", "") in (".|.body", ".body"):
            url = url_match.group(0)
            for pattern, kind in ((_GITHUB_TAG, "github_release"), (_CODEBERG_TAG, "codeberg_release")):
                if (m := pattern.search(url)) and not _SHELL_EXPANSION.search(m.group(3)):
                    tag = m.group(3).replace("${EXT_RELEASE}", "${VERSION}")
                    return {"type": kind, "repo": f"{m.group(1)}/{m.group(2)}", "tag": tag}, None
    run = text.replace("${EXT_RELEASE}", "${VERSION}").replace("EXT_RELEASE", "VERSION")
    return {"type": "command", "run": run}, "release notes command kept verbatim; it must print the notes to stdout"


class Migrator:
    def __init__(self, readme_vars: Json, jenkins_vars: Json, dockerfile: str = "") -> None:
        self.rendered, unresolved = _render_legacy({**jenkins_vars, **readme_vars})
        self.readme_keys = set(readme_vars)
        self.jenkins_keys = set(jenkins_vars)
        self.raw_keys = self.readme_keys | self.jenkins_keys
        self.dockerfile = dockerfile
        self.repo_vars = parse_repo_vars(self.rendered.get("repo_vars", []))
        self.used: set[str] = {"repo_vars"}
        self.used_repo_vars: set[str] = set()
        self.todos: list[str] = list(unresolved)
        # (section, key or None, message): rendered as "# TODO" comments in the output.
        self.comments: list[tuple[str, str | None, str]] = []

    # -- helpers -------------------------------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        self.used.add(key)
        if key in self.rendered:
            return self.rendered[key]
        return DEFAULTS.get(key, default)

    def var(self, key: str, default: str = "") -> str:
        self.used_repo_vars.add(key)
        return self.repo_vars.get(key, default)

    def flag(self, key: str) -> bool | str:
        value = self.get(key, False)
        if value == "optional":
            return "optional"
        return bool(value)

    def todo(self, section: str, key: str | None, message: str) -> None:
        self.comments.append((section, key, message))
        self.todos.append(f"{section}{'.' + key if key else ''}: {message}")

    @property
    def name(self) -> str:
        name = str(self.get("project_name"))
        return name.removeprefix("docker-")

    # -- sections ------------------------------------------------------------------------------------------

    def upstream(self) -> Json:
        out: Json = {}
        url = self.get("project_url")
        if url:
            out["url"] = url
        version, version_todo = self.version_source()
        out["version"] = version
        if version_todo:
            self.todo("upstream", "version", version_todo)
        notes, notes_todo = self.notes_source()
        if notes is not None:
            out["notes"] = notes
        if notes_todo:
            self.todo("upstream", "notes", notes_todo)
        sponsors = self.get("sponsor_links", [])
        if sponsors:
            out["sponsor"] = [{"name": s["name"], "url": s["url"]} for s in sponsors]
        return out

    def version_source(self) -> tuple[Json, str | None]:
        self.used.add("external_type")
        command = self.get("custom_version_command")
        if command:
            return parse_version_command(str(command))
        ext = str(self.get("external_type", "na"))
        repo = f"{self.var('EXT_USER')}/{self.var('EXT_REPO')}"
        if ext == "github_stable":
            return {"type": "github_release", "repo": repo}, None
        if ext == "github_devel":
            return {"type": "github_release", "repo": repo, "prerelease": True}, None
        if ext == "github_commit":
            return {"type": "github_commit", "repo": repo, "branch": self.var("EXT_GIT_BRANCH")}, None
        if ext == "gitlab_commit":
            project_id = self.var("EXT_GITLAB_ID")
            return {
                "type": "gitlab_commit",
                "project_id": int(project_id) if project_id.isdigit() else 0,
                "branch": self.var("EXT_GIT_BRANCH"),
            }, None
        if ext == "alpine_repo":
            return {
                "type": "alpine_apk",
                "repo_url": self.var("DIST_REPO"),
                "package": self.var("DIST_REPO_PACKAGES"),
            }, None
        if ext == "custom_json":
            return {"type": "json", "url": self.var("JSON_URL"), "jq": self.var("JSON_PATH")}, None
        if ext == "deb_repo":
            return {
                "type": "deb_repo",
                "image": f"{self.var('DIST_IMAGE')}:{self.var('DIST_TAG')}",
                "sources": self.var("DIST_REPO"),
                "packages": self.var("DIST_REPO_PACKAGES"),
            }, None
        if ext == "external_blob":
            return {"type": "blob", "url": self.var("EXT_BLOB")}, None
        if ext == "npm_version":
            return {"type": "npm", "package": self.var("EXT_NPM")}, None
        if ext == "pip_version":
            return {"type": "pip", "package": self.var("EXT_PIP")}, None
        if ext == "os":
            return {"type": "os"}, None
        return {"type": "os"}, f"external_type {ext!r} without a custom_version_command; confirm the version source"

    def notes_source(self) -> tuple[Json | None, str | None]:
        command = self.get("custom_release_notes_command")
        if command:
            return parse_notes_command(str(command))
        ext = str(self.get("external_type", "na"))
        repo = f"{self.var('EXT_USER')}/{self.var('EXT_REPO')}"
        if ext in ("github_stable", "github_devel"):
            return {"type": "github_release", "repo": repo}, None
        if ext == "github_commit":
            return {"type": "github_commit", "repo": repo, "branch": self.var("EXT_GIT_BRANCH")}, None
        if ext == "gitlab_commit":
            project_id = self.var("EXT_GITLAB_ID")
            return {
                "type": "gitlab_commit",
                "project_id": int(project_id) if project_id.isdigit() else 0,
                "branch": self.var("EXT_GIT_BRANCH"),
            }, None
        return None, None

    def release(self) -> Json:
        out: Json = {
            "branch": str(self.get("ls_branch", "master")),
            "tag": str(self.get("release_tag", "latest")),
            "channel": "prerelease" if str(self.get("release_type", "stable")) == "prerelease" else "stable",
        }
        items = self.get("development_versions_items", []) if self.get("development_versions") else []
        self.used.add("development_versions_items")
        tags = []
        for item in items:
            tag: Json = {"tag": str(item["tag"]), "description": str(item.get("desc", ""))}
            if item.get("extra"):
                tag["unraid_extra"] = str(item["extra"])
            tags.append(tag)
        if tags:
            out["tags"] = tags
        return out

    def detect_base(self) -> str:
        dist = self.var("DIST_IMAGE", "alpine")
        for line in self.dockerfile.splitlines():
            match = re.match(r"^FROM\s+(?:--platform=\S+\s+)?\S*baseimage-([a-z0-9-]+?)(?::|\s|$)", line)
            if match and match.group(1) in BASE_FROM_IMAGE:
                return BASE_FROM_IMAGE[match.group(1)]
        if dist in BASE_ENUM:
            return dist
        self.todo("build", "base", f"DIST_IMAGE {dist!r} is not a known base family")
        return "other"

    def build(self) -> Json:
        platforms = []
        for arch in self.get("available_architectures", []):
            label = str(arch.get("arch", ""))
            if label in ("x86-64", "amd64"):
                platforms.append("amd64")
            elif label in ("arm64", "aarch64"):
                platforms.append("arm64")
            else:
                self.todos.append(f"available_architectures: {label!r} is not a supported platform; dropped")
        out: Json = {"base": self.detect_base()}
        version_arg = self.var("BUILD_VERSION_ARG")
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", version_arg):
            out["version_arg"] = version_arg
        elif version_arg:
            self.todo("build", "version_arg", f"BUILD_VERSION_ARG {version_arg!r} is not a valid ARG name")
        out["platforms"] = platforms or ["amd64", "arm64"]
        if self.name.startswith("baseimage-"):
            out["first_party"] = False
        if self.get("build_riscv64"):
            self.todo("build", "platforms", "build_riscv64 was true; riscv64 is not a supported platform")
        return out

    def test(self) -> Json:
        out: Json = {}
        if self.var("CI", "true").lower() == "false":
            out["enabled"] = False
        delay = self.var("CI_DELAY")
        if delay.isdigit():
            out["startup_timeout"] = max(10, int(delay))
        docker_env = self.var("CI_DOCKERENV")
        if docker_env:
            pairs = [p.split("=", 1) for p in docker_env.split("|") if "=" in p]
            out["env"] = {k: v for k, v in pairs}
        if self.var("CI_WEB").lower() == "true" and self.var("CI_PORT").isdigit():
            web: Json = {"port": int(self.var("CI_PORT"))}
            if self.var("CI_WEBPATH"):
                web["path"] = self.var("CI_WEBPATH")
            if self.var("CI_SSL").lower() == "true":
                web["scheme"] = "https"
            if self.var("CI_AUTH"):
                web["auth"] = self.var("CI_AUTH")
            if self.var("CI_WEB_SCREENSHOT_DELAY").isdigit():
                web["screenshot_delay"] = int(self.var("CI_WEB_SCREENSHOT_DELAY"))
            if self.var("CI_WEB_SCREENSHOT_TIMEOUT").isdigit():
                web["screenshot_timeout"] = int(self.var("CI_WEB_SCREENSHOT_TIMEOUT"))
            out["web"] = web
        if self.var("SBOM_TIMEOUT").isdigit():
            out["sbom_timeout"] = int(self.var("SBOM_TIMEOUT"))
        return out

    def _params(self, flag: str, key: str, opt_flag: str, opt_key: str) -> list[tuple[Json, bool]]:
        items: list[tuple[Json, bool]] = []
        if self.get(flag):
            items.extend((dict(i), False) for i in self.get(key, []))
        if self.get(opt_flag):
            items.extend((dict(i), True) for i in self.get(opt_key, []))
        self.used.update({key, opt_key})
        return items

    def runtime(self) -> Json:
        out: Json = {}
        container = str(self.get("param_container_name", self.name))
        if container != self.name:
            out["container_name"] = container
        common = (
            self.flag("common_param_env_vars_enabled") if "common_param_env_vars_enabled" in self.raw_keys else True
        )
        if common is not True:
            out["common_env"] = common
        net = self.flag("param_usage_include_net")
        if net:
            network: Json = {"mode": str(self.get("param_net")), "description": str(self.get("param_net_desc", ""))}
            if net == "optional":
                network["optional"] = True
            out["network"] = network
        for legacy, key in (("hostname", "hostname"), ("mac_address", "mac_address")):
            flag = self.flag(f"param_usage_include_{legacy}")
            if flag:
                param: Json = {
                    "value": str(self.get(f"param_{legacy}")),
                    "description": str(self.get(f"param_{legacy}_desc", "")),
                }
                if flag == "optional":
                    param["optional"] = True
                out[key] = param
        privileged = self.flag("privileged")
        if privileged:
            out["privileged"] = privileged

        ports = []
        for item, optional in self._params(
            "param_usage_include_ports", "param_ports", "opt_param_usage_include_ports", "opt_param_ports"
        ):
            internal = str(item["internal_port"])
            port: Json = {"internal": internal}
            external = str(item.get("external_port", internal.split("/")[0]))
            if external != internal.split("/")[0]:
                port["external"] = external
            port["description"] = str(item.get("port_desc", ""))
            if item.get("name"):
                port["name"] = str(item["name"])
            if optional:
                port["optional"] = True
            ports.append(port)
        if ports:
            out["ports"] = ports

        volumes = []
        for item, optional in self._params(
            "param_usage_include_vols", "param_volumes", "opt_param_usage_include_vols", "opt_param_volumes"
        ):
            volume: Json = {"path": str(item["vol_path"])}
            if item.get("vol_host_path"):
                volume["host_path"] = str(item["vol_host_path"])
            volume["description"] = str(item.get("desc", ""))
            if item.get("name"):
                volume["name"] = str(item["name"])
            if optional:
                volume["optional"] = True
            if item.get("default"):
                volume["default_host_path"] = True
            volumes.append(volume)
        if volumes:
            out["volumes"] = volumes

        env = []
        for item, optional in self._params(
            "param_usage_include_env", "param_env_vars", "opt_param_usage_include_env", "opt_param_env_vars"
        ):
            if common and str(item["env_var"]).upper() == "TZ":
                continue
            variable: Json = {"name": str(item["env_var"]), "value": str(item.get("env_value", ""))}
            variable["description"] = str(item.get("desc", ""))
            if item.get("name"):
                variable["title"] = str(item["name"])
            if item.get("env_options"):
                variable["options"] = [str(o) for o in item["env_options"]]
            if optional:
                variable["optional"] = True
            env.append(variable)
        if env:
            out["env"] = env

        devices = []
        for item, optional in self._params(
            "param_device_map", "param_devices", "opt_param_device_map", "opt_param_devices"
        ):
            device: Json = {"path": str(item["device_path"])}
            if item.get("device_host_path") and item["device_host_path"] != item["device_path"]:
                device["host_path"] = str(item["device_host_path"])
            device["description"] = str(item.get("desc", ""))
            if item.get("name"):
                device["name"] = str(item["name"])
            if optional:
                device["optional"] = True
            devices.append(device)
        if devices:
            out["devices"] = devices

        caps = []
        for item, optional in self._params(
            "cap_add_param", "cap_add_param_vars", "opt_cap_add_param", "opt_cap_add_param_vars"
        ):
            cap: Json = {"cap": str(item["cap_add_var"])}
            if item.get("desc"):
                cap["description"] = str(item["desc"])
            if optional:
                cap["optional"] = True
            caps.append(cap)
        if caps:
            out["cap_add"] = caps

        secs = []
        for item, optional in self._params(
            "security_opt_param", "security_opt_param_vars", "opt_security_opt_param", "opt_security_opt_param_vars"
        ):
            sec: Json = {
                "run": str(item["run_var"]),
                "compose": str(item["compose_var"]),
                "description": str(item.get("desc", "")),
            }
            if optional:
                sec["optional"] = True
            secs.append(sec)
        if secs:
            out["security_opt"] = secs

        custom = []
        for key, optional in (("custom_params", False), ("opt_custom_params", True)):
            for item in self.get(key, []) or []:
                value = item["value"]
                entry: Json = {
                    "flag": str(item["name"]),
                    "compose": str(item["name_compose"]),
                    "value": [str(v) for v in value] if isinstance(value, list) else str(value),
                    "description": str(item.get("desc", "")),
                }
                if optional:
                    entry["optional"] = True
                custom.append(entry)
        if custom:
            out["custom"] = custom

        for legacy, key in (("readonly", "readonly"), ("nonroot", "nonroot")):
            supported = self.get(f"{legacy}_supported")
            message = self.get(f"{legacy}_message")
            if supported is not None or message:
                capability: Json = {"supported": bool(supported)}
                if message:
                    capability["notes"] = str(message)
                out[key] = capability
        return out

    def docs(self) -> Json:
        out: Json = {}
        categories = [c.strip() for c in str(self.get("project_categories", "")).split(",") if c.strip()]
        if not categories:
            categories = ["Uncategorized"]
            self.todo("docs", "categories", "project_categories was empty; pick a category")
        out["categories"] = categories
        logo = self.get("project_logo")
        if logo:
            out["logo"] = str(logo)
        if self.get("app_setup_block_enabled") and self.get("app_setup_block"):
            out["setup"] = str(self.get("app_setup_block"))
        self.used.add("app_setup_block")
        if self.get("optional_parameters"):
            out["optional_parameters"] = str(self.get("optional_parameters"))
        notices: Json = {}
        for key in ("hwaccel", "keyboard", "media", "seccomp"):
            if self.get(f"readme_{key}"):
                notices[key] = True
        if self.var("CI_SSL").lower() == "true" and not self.get("full_custom_readme"):
            notices["strict_proxy"] = True
        if self.get("kasm_blurb"):
            notices["kasm"] = True
        if self.get("selkies_blurb"):
            selkies: Json = {"enabled": True}
            if self.get("show_nvidia"):
                selkies["nvidia"] = True
            if self.get("external_https_port"):
                selkies["https_port"] = int(self.get("external_https_port"))
            if self.get("external_http_port"):
                selkies["http_port"] = int(self.get("external_http_port"))
            notices["selkies"] = selkies
        self.used.update({"show_nvidia", "external_https_port", "external_http_port"})
        if notices:
            out["notices"] = notices
        if self.get("external_application_snippet_enabled"):
            external: Json = {
                "cli": str(self.get("external_application_cli_block", "")),
                "compose": str(self.get("external_application_compose_block", "")),
            }
            if self.get("external_application_unraid_block"):
                external["unraid"] = str(self.get("external_application_unraid_block"))
            out["external_application"] = external
        self.used.update(
            {
                "external_application_cli_block",
                "external_application_compose_block",
                "external_application_unraid_block",
            }
        )
        if self.get("full_custom_readme"):
            out["custom_readme"] = str(self.get("full_custom_readme"))
        return out

    def proxy(self) -> Json | None:
        if self.var("CI_WEB").lower() != "true" or not self.var("CI_PORT").isdigit():
            return None
        out: Json = {"port": int(self.var("CI_PORT"))}
        if self.var("CI_SSL").lower() == "true":
            out["scheme"] = "https"
        out["subdomain"] = True
        out["subfolder"] = {"supported": False}
        self.todo(
            "proxy", "subfolder", "confirm whether the app supports a subfolder reverse proxy and set base_url_note"
        )
        return out

    def publish(self) -> Json:
        out: Json = {}
        if self.get("unraid_template") is False:
            out["stores"] = {"unraid": False}
        requirement = self.get("unraid_requirement")
        if requirement and requirement is not False:
            out["unraid"] = {"requirement": str(requirement)}
        self.used.add("unraid_template_sync")
        return out

    def deprecation(self) -> Json | None:
        if not self.get("project_deprecation_status"):
            self.used.add("project_deprecation_message")
            return None
        out: Json = {"status": True}
        if self.get("project_deprecation_message"):
            out["message"] = str(self.get("project_deprecation_message"))
        return out

    def changelog(self) -> list[Json]:
        out = []
        for index, item in enumerate(self.get("changelogs", [])):
            raw = str(item.get("date", ""))
            match = _LEGACY_DATE.match(raw.strip())
            if match:
                day, month, year = match.groups()
                if int(month) > 12 >= int(day):
                    day, month = month, day
                    self.todos.append(
                        f"changelog[{index}]: date {raw!r} read as MM.DD.YY; confirm 20{year}-{month}-{day}"
                    )
                date = f"20{year}-{month}-{day}"
            else:
                date = raw.strip().rstrip(":")
                self.todos.append(f"changelog[{index}]: date {raw!r} is not DD.MM.YY; left as {date!r}")
            out.append({"date": date, "text": str(item.get("desc", ""))})
        for index in range(1, len(out)):
            if out[index]["date"] > out[index - 1]["date"]:
                self.todos.append(f"changelog[{index}]: {out[index]['date']} is newer than the entry above it; reorder")
        return out

    # -- assembly ------------------------------------------------------------------------------------------

    def manifest(self) -> Json:
        out: Json = {"schema": 1, "name": self.name}
        blurb = str(self.get("project_blurb", "")).strip("\n")
        out["summary"] = blurb if blurb else f"{self.name} container image."
        if not blurb and not self.get("full_custom_readme"):
            self.todo("summary", None, "project_blurb was empty")
        if self.get("project_blurb_optional_extras_enabled") and self.get("project_blurb_optional_extras"):
            out["extras"] = [str(e) for e in self.get("project_blurb_optional_extras")]
        self.used.add("project_blurb_optional_extras")
        out["upstream"] = self.upstream()
        out["release"] = self.release()
        out["build"] = self.build()
        test = self.test()
        if test:
            out["test"] = test
        out["runtime"] = self.runtime()
        out["docs"] = self.docs()
        proxy = self.proxy()
        if proxy:
            out["proxy"] = proxy
        publish = self.publish()
        if publish:
            out["publish"] = publish
        deprecation = self.deprecation()
        if deprecation:
            out["deprecation"] = deprecation
        changelog = self.changelog()
        if changelog:
            out["changelog"] = changelog
        self.report_unmapped()
        return out

    def report_unmapped(self) -> None:
        for key in sorted(self.raw_keys - self.used):
            if key in DROPPED:
                self.todos.append(f"dropped {key}: {DROPPED[key]}")
            else:
                self.todos.append(f"unmapped key {key}: no equivalent in image.yml")
        handled = {
            "BUILD_VERSION_ARG", "LS_USER", "LS_REPO", "CONTAINER_NAME", "DOCKERHUB_IMAGE", "DEV_DOCKERHUB_IMAGE",
            "PR_DOCKERHUB_IMAGE", "DIST_IMAGE", "MULTIARCH", "CI", "CI_WEB", "CI_PORT", "CI_SSL", "CI_DELAY",
            "CI_DOCKERENV", "CI_AUTH", "CI_WEBPATH", "CI_WEB_SCREENSHOT_DELAY", "CI_WEB_SCREENSHOT_TIMEOUT",
            "SBOM_TIMEOUT",
        }  # fmt: skip
        for key in sorted(set(self.repo_vars) - self.used_repo_vars - handled):
            self.todos.append(f"unmapped repo_vars entry {key}: no equivalent in image.yml")


# --- YAML output -----------------------------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if ("\n" in value or (len(value) > 100 and " " in value)) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def _dump_section(key: str, value: Any) -> str:
    text: str = yaml.dump(
        {key: value}, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=4096, default_flow_style=False
    )
    return text


def _insert_comment(section_text: str, key: str | None, message: str) -> str:
    lines = section_text.split("\n")
    index = 0
    indent = ""
    if key is not None:
        for i, line in enumerate(lines[1:], start=1):
            match = re.match(rf"^(\s*){re.escape(key)}:", line)
            if match:
                index, indent = i, match.group(1)
                break
    lines.insert(index, f"{indent}# TODO: {message}")
    return "\n".join(lines)


def to_yaml(manifest: Json, comments: list[tuple[str, str | None, str]], header: str) -> str:
    parts = [header]
    for key in SECTION_ORDER:
        if key not in manifest:
            continue
        text = _dump_section(key, manifest[key])
        for section, subkey, message in comments:
            if section == key:
                text = _insert_comment(text, subkey, message)
        parts.append(text.rstrip("\n"))
    scalar_head: list[str] = []
    body: list[str] = []
    for part in parts[1:]:
        (scalar_head if part.split(":")[0] in ("schema", "name", "title") else body).append(part)
    return header + "\n".join(scalar_head) + "\n\n" + "\n\n".join(body) + "\n"


def migrate(readme_vars: Json, jenkins_vars: Json, dockerfile: str = "") -> tuple[Json, str, list[str]]:
    """Returns (manifest dict, image.yml text, TODO lines)."""
    migrator = Migrator(readme_vars, jenkins_vars, dockerfile)
    manifest = migrator.manifest()
    violations = [format_error(e) for e in contract_errors("image", manifest)]
    for violation in violations:
        migrator.todos.append(f"does not validate: {violation}")
    header = (
        f"# image.yml for docker-{manifest['name']}, migrated by `builder migrate` "
        "from readme-vars.yml and jenkins-vars.yml.\n"
        "# Review every TODO, delete the legacy files, then run `builder render`.\n"
    )
    return manifest, to_yaml(manifest, migrator.comments, header), migrator.todos


def migrate_repo(repo_dir: Path) -> tuple[str, list[str]]:
    readme_vars = load_yaml(repo_dir / "readme-vars.yml") or {}
    jenkins_vars = load_yaml(repo_dir / "jenkins-vars.yml") or {}
    dockerfile_path = repo_dir / "Dockerfile"
    dockerfile = dockerfile_path.read_text(encoding="utf-8") if dockerfile_path.is_file() else ""
    _, text, todos = migrate(readme_vars, jenkins_vars, dockerfile)
    return text, todos
