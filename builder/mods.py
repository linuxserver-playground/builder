"""
Docker Mods: the files a mod branch generates from its mod.yml, and the conversion of a branch of the legacy
linuxserver/docker-mods repository into one.

A mod branch of linuxserver-playground/docker-mods holds `Dockerfile`, `root/` and `mod.yml`; `README.md`,
`AGENTS.md` and `.github/workflows/build.yml` are rendered from `mod.yml` (`render_mod`). A legacy branch carries
its identity in `.github/workflows/BuildImage.yml` (`BASEIMAGE`, `MODNAME`, `MULTI_ARCH`), its text in `README.md`
and the images it is listed for in the master branch's `mod-list.yml`; `convert` turns those into a `mod.yml` and
reports what it could not carry over (versioning logic, irregular branch names).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .manifest import Settings
from .render import environment

Json = dict[str, Any]

GENERATED = ("README.md", "AGENTS.md", ".github/workflows/build.yml")
SCHEMA_URL = "https://github.com/linuxserver-playground/builder/blob/main/schemas/mod.schema.json"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DEPRECATED_BRANCH = re.compile(r"^letsencrypt-|deprecat")
DEPRECATED_TEXT = re.compile(
    r"^.*(?:this mod (?:is|has been) (?:now )?deprecated|deprecation notice|mod (?:is|has been) deprecated).*$",
    re.I | re.M,
)
NOT_MODS = {"mod-scripts": "build tooling of the legacy repository, not a mod"}
GENERIC_TITLES = {"about", "overview", "description", "introduction", "readme"}
# The legacy READMEs' usage boilerplate, which the generated README states itself.
USAGE_LINE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:In\b[^`]*\bdocker (?:container )?arguments, set an environment variable `DOCKER_MODS=[^`]*`"
    r"(?: to enable)?\.?|If adding multiple mods, enter them in an array separated by .*)\s*$",
    re.I,
)


# Version sources the control plane's scheduler never runs (shell from a repo, docker, a package table): a mod's
# upstream block exists for the scheduler, so it may not use them, not even inside a `combined` source.
UNSCHEDULED_SOURCES = frozenset({"os", "command", "deb_repo"})
ARG_MOD_VERSION = re.compile(r"^\s*ARG\s+MOD_VERSION\b", re.I | re.M)


def branch_of(mod: Json) -> str:
    return f"{mod['target']}-{mod['name']}"


def source_types(source: Json) -> list[str]:
    """Every `type` in a version source, the parts of a `combined` one included."""
    types = [str(source.get("type"))]
    for part in source.get("sources") or []:
        if isinstance(part, dict):
            types.extend(source_types(part))
    return types


def upstream_source_label(source: Json) -> str:
    """A short, readable name for a version source: `github_release moby/moby + github_release docker/compose`."""
    if source.get("type") == "combined":
        return " + ".join(upstream_source_label(p) for p in source.get("sources") or [] if isinstance(p, dict))
    where = source.get("repo") or source.get("package") or source.get("url") or source.get("project_id")
    return f"{source.get('type')} {where}" if where else str(source.get("type"))


def upstream_lines(mod: Json) -> list[Json]:
    """The version lines of a mod's upstream block, the default line first: `name` (None for the default line,
    the one `:<branch>` follows), `version` (the source) and `tags` (the floating tags it also moves)."""
    upstream = mod.get("upstream") or {}
    if not upstream.get("version"):
        return []
    lines = [{"name": None, "version": upstream["version"], "tags": list(upstream.get("tags") or [])}]
    for line in upstream.get("lines") or []:
        lines.append({"name": line["name"], "version": line["version"], "tags": list(line.get("tags") or [])})
    return lines


def line_tag_suffixes(version: str, kinds: list[str]) -> list[str]:
    """The floating tag suffixes a line's build moves for `version` (already stripped of the characters a tag
    cannot hold): `major` is the version up to its first dot, `minor` up to its second; a version without that
    dot gets no such tag. Ordered major first, as the build tags them."""
    parts = version.split(".")
    suffixes = []
    if "major" in kinds and len(parts) >= 2:
        suffixes.append(parts[0])
    if "minor" in kinds and len(parts) >= 3:
        suffixes.append(".".join(parts[:2]))
    return [s for s in suffixes if s and s != version]


def mod_findings(mod: Json, dockerfile: str | None) -> list[str]:
    """What a valid mod.yml with an upstream block still gets wrong: a source the scheduler will not run, two
    lines of one name, or a Dockerfile that never declares the MOD_VERSION build arg the build passes."""
    lines = upstream_lines(mod)
    if not lines:
        return []
    findings = []
    for line in lines:
        where = "upstream.version" if line["name"] is None else f"upstream.lines[{line['name']}].version"
        unscheduled = sorted(set(source_types(line["version"])) & UNSCHEDULED_SOURCES)
        if unscheduled:
            findings.append(
                f"mod.yml: {where} uses {', '.join(unscheduled)}, which the scheduler never runs; use a typed source"
            )
    names = [line["name"] for line in lines[1:]]
    if duplicates := sorted({n for n in names if names.count(n) > 1}):
        findings.append(f"mod.yml: upstream.lines repeats {', '.join(duplicates)}; line names are unique")
    if dockerfile is not None and not ARG_MOD_VERSION.search(dockerfile):
        findings.append(
            "Dockerfile: no `ARG MOD_VERSION`, but the build passes the resolved upstream version as that arg"
        )
    return findings


LINE_INPUT = """      line:
        description: Version line to build, a name from mod.yml upstream.lines; empty builds the default line
        type: string
        default: ""
"""


def build_workflow(mod: Json, template: str) -> str:
    """The branch's build.yml: the shared caller, plus the `line` dispatch input when mod.yml has version lines (the
    control plane names the line it dispatches). Every other branch keeps the identical file."""
    if not (mod.get("upstream") or {}).get("lines"):
        return template
    anchor_input = "        description: Optional MOD_VERSION build arg, also published as an extra tag\n"
    anchor_input += '        type: string\n        default: ""\n'
    anchor_with = "      mod_version: ${{ github.event.inputs.mod_version || '' }}\n"
    header = "Generated for every docker-mods branch; identical on each."
    for anchor in (anchor_input, anchor_with, header):
        if template.count(anchor) != 1:
            raise ValueError(f"mod-build.yml template changed; cannot place the line input at {anchor!r}")
    out = template.replace(anchor_input, anchor_input + LINE_INPUT)
    out = out.replace(anchor_with, anchor_with + "      mod_line: ${{ github.event.inputs.line || '' }}\n")
    return out.replace(
        header,
        "Generated for every docker-mods branch; identical on each but for the `line` input,\n"
        "# which only mods with version lines (mod.yml upstream.lines) have.",
    )


def render_mod(mod: Json, settings: Settings | None = None) -> dict[str, str]:
    """README.md, AGENTS.md and the build workflow of a mod branch, from its mod.yml."""
    settings = settings or Settings()
    env = environment()
    deprecated = mod.get("deprecated") or {}
    ctx = {
        "org": settings.org,
        "site_url": settings.site_url,
        "schema_url": SCHEMA_URL,
        "branch": branch_of(mod),
        "name": mod["name"],
        "target": mod["target"],
        "title": mod.get("title") or mod["name"],
        "summary": mod["summary"].strip(),
        "env": [
            {
                "name": e["name"],
                "description": e["description"],
                "default": e.get("default"),
                "required": e.get("required", False),
            }
            for e in mod.get("env", [])
        ],
        "docs": (mod.get("docs") or "").strip(),
        "upstream": (
            {
                "url": upstream.get("url"),
                "source": upstream_source_label(upstream["version"]),
                "lines": [{**line, "source": upstream_source_label(line["version"])} for line in upstream_lines(mod)],
            }
            if (upstream := mod.get("upstream") or {}).get("version")
            else None
        ),
        "deprecated": bool(deprecated.get("status")),
        "deprecation_message": deprecated.get("message"),
    }
    template_dir = Path(__file__).resolve().parent / "templates"
    return {
        "README.md": env.get_template("mod-readme.md.j2").render(ctx).rstrip("\n") + "\n",
        "AGENTS.md": env.get_template("agents/mod.md.j2").render(ctx).rstrip("\n") + "\n",
        ".github/workflows/build.yml": build_workflow(
            mod, (template_dir / "mod-build.yml").read_text(encoding="utf-8")
        ),
    }


# -------------------------------------------------------------------------------------------------- conversion


@dataclass
class Conversion:
    branch: str
    mod: Json | None = None
    notes: list[str] = field(default_factory=list)
    skipped: str | None = None


def _env_value(workflow: str, key: str) -> str | None:
    m = re.search(rf'^\s*{key}:\s*"?([^"\n#]*)"?', workflow, re.MULTILINE)
    value = m.group(1).strip() if m else None
    return value or None


def _plain(markdown: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[*_`]+", "", text)
    return " ".join(text.split())


def rewrite_registry(text: str, org: str) -> str:
    """Point the legacy image and repository references at the playground's."""
    text = re.sub(r"(?:(?:lscr|ghcr)\.io/)?linuxserver/mods:", f"ghcr.io/{org}/mods:", text)
    text = text.replace("github.com/linuxserver/docker-mods", f"github.com/{org}/docker-mods")
    return text


def clean_title(title: str) -> str:
    """ "Calibre - Docker mod for 64-bit Ubuntu-based containers" -> "Calibre"."""
    title = _plain(title)
    title = re.sub(r"\s*[-\u2013:]\s*(?:a\s+)?(?:universal\s+)?docker[- ]mods?\b.*$", "", title, flags=re.I)
    title = re.sub(r"\s+docker[- ]mods?\s+for\s+.*$", "", title, flags=re.I)
    return title.strip() or title


def readme_parts(readme: str) -> tuple[str | None, str, str]:
    """(title, first paragraph as plain text, the rest as Markdown) of a legacy mod README. A deprecation banner
    as the first heading gives way to the next heading, and the paragraph taken as the summary is not repeated."""
    lines = readme.replace("\r\n", "\n").split("\n")
    title = None
    for _ in range(2):
        for i, line in enumerate(lines):
            if line.startswith("# "):
                title = line[2:].strip()
                lines = lines[i + 1 :]
                break
        if not (title and "deprecated" in title.lower() and any(line.startswith("# ") for line in lines)):
            break
    kept, fenced = [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced or not USAGE_LINE.match(line):
            kept.append(line)
    blocks = re.split(r"\n\s*\n", "\n".join(kept).strip("\n"))
    summary = ""
    for index, block in enumerate(blocks):
        first = block.strip()
        if not first or first.startswith(("#", "```", "|", "<", "!", "-", "*", ">")) or re.match(r"\d+\.", first):
            continue
        if "DOCKER_MODS" in first or "docker arguments" in first.lower():
            continue  # the usage line, which the generated README states itself
        summary = _plain(first)
        if len(summary) >= 10:
            blocks = blocks[:index] + blocks[index + 1 :]
            break
    return (clean_title(title) if title else None), summary, "\n\n".join(blocks).strip("\n")


def _sentence_cut(text: str, limit: int = 300) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > 80 else cut.rsplit(" ", 1)[0] + "…"


def convert(
    branch: str,
    read: Callable[[str], str | None],
    *,
    listed_for: list[str],
    known_targets: set[str],
    maintainers: list[str],
    org: str = "linuxserver-playground",
) -> Conversion:
    """
    One legacy branch as a mod.yml. `read(path)` returns a file of the branch (None when absent), `listed_for`
    the images whose mod-list.yml entry names the branch, `known_targets` every image a mod targets (to split an
    irregular branch name), `maintainers` the branch's committers' GitHub logins.
    """
    result = Conversion(branch)
    if branch in NOT_MODS:
        result.skipped = NOT_MODS[branch]
        return result
    workflow = read(".github/workflows/BuildImage.yml") or ""
    base, modname = _env_value(workflow, "BASEIMAGE"), _env_value(workflow, "MODNAME")
    if (base or "").startswith("replace") or (modname or "").startswith("replace"):
        result.skipped = "BuildImage.yml still has the template's placeholders (an unfinished mod)"
        return result
    if base and modname and f"{base}-{modname}" == branch:
        target, name = base, modname
    else:
        candidates = sorted(
            {t for t in known_targets | ({base} if base else set()) if branch.startswith(f"{t}-")}, key=len
        )
        if not candidates:
            result.skipped = "the branch is not <target>-<name> for any known target"
            return result
        target = candidates[-1]
        name = branch[len(target) + 1 :]
        result.notes.append(f"branch name does not match BuildImage.yml ({base}-{modname}); took {target} / {name}")
    if not (SLUG.match(target) and SLUG.match(name)):
        result.skipped = f"target {target!r} or name {name!r} is not a valid slug"
        return result
    if not read("Dockerfile"):
        result.skipped = "no Dockerfile"
        return result

    title, summary, body = readme_parts(read("README.md") or "")
    if not title or title.lower() in GENERIC_TITLES:
        title = name
    if len(summary) < 10:
        summary = f"{title}."
        result.notes.append("no usable first paragraph in README.md; the summary is the title")
    multi = (_env_value(workflow, "MULTI_ARCH") or "").lower() == "true"
    version = re.search(r'^\s*MOD_VERSION=(?!""|\$\{\{ *env\.MOD_VERSION *\}\})(.+)$', workflow, re.MULTILINE)
    if version:
        dockerfile = read("Dockerfile") or ""
        fallback = re.search(r'-z "?\$\{?MOD_VERSION|\$\{MOD_VERSION:-', dockerfile)
        result.notes.append(
            "upstream computed MOD_VERSION in BuildImage.yml; add an upstream block so the build passes it and the "
            "scheduler rebuilds on a new version, or "
            + ("the Dockerfile's own lookup applies" if fallback else "the Dockerfile needs a lookup of its own")
        )

    mod: Json = {
        "schema": 1,
        "name": name,
        "target": target,
        "title": title,
        "summary": _sentence_cut(rewrite_registry(summary, org)),
        "maintainers": maintainers or ["linuxserver"],
        "platforms": ["amd64", "arm64"] if multi else ["amd64"],
    }
    images = sorted({i for i in listed_for if i != target and SLUG.match(i)})
    if images:
        mod["images"] = images
    notices = [_plain(m.group(0)).strip("# >") for m in DEPRECATED_TEXT.finditer(read("README.md") or "")]
    notice = next((n for n in notices if len(n) > len("this mod is deprecated.")), None)
    deprecated = bool(DEPRECATED_BRANCH.search(branch) or notices) or bool(
        re.search(r"\bdeprecated\b", f"{title} {summary}", re.I)
    )
    mod["deprecated"] = {"status": deprecated}
    if deprecated:
        if branch.startswith("letsencrypt-"):
            message = "The letsencrypt image was renamed swag; use the swag mods."
        elif notice:
            message = _sentence_cut(rewrite_registry(notice, org), 200)
        else:
            message = "Marked deprecated upstream."
        mod["deprecated"]["message"] = message
    # Trailing spaces would stop the YAML dumper from writing the text as a readable `|` block.
    docs = "\n".join(line.rstrip() for line in rewrite_registry(body, org).split("\n")).strip()
    if docs:
        mod["docs"] = docs + "\n"
    result.mod = mod
    return result


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str)


def mod_yaml(mod: Json, notes: list[str] | None = None) -> str:
    """mod.yml text: a header naming the schema, the converter's notes as comments, then the manifest."""
    header = [f"# Schema: {SCHEMA_URL}"]
    for note in notes or []:
        header.append(f"# Converted from linuxserver/docker-mods: {note}")
    body = yaml.dump(mod, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)
    return "\n".join(header) + "\n" + body
