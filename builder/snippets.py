"""
The docker compose and docker run snippets for an image, as plain functions so the hub can import them.

    from builder.snippets import compose, docker_run, as_dict
    compose(manifest_data)                 # full: optional parameters included with "#optional" markers
    docker_run(manifest_data, full=False)  # required-only

Ordering and markers follow the legacy README snippets exactly.
"""

from __future__ import annotations

import json
from typing import Any

from .manifest import Json, Settings, build_context

OPTIONAL = "optional"


def _ctx(manifest: Json, settings: Settings | None) -> Json:
    return manifest if "rt" in manifest and "image_ref" in manifest else build_context(manifest, settings)


def _env_pair(item: Json, *, compose: bool = False) -> str:
    """VAR=value; values with spaces are quoted (the whole pair in compose, the value in docker run)."""
    name, value = item["name"], item["value"]
    if " " not in value:
        return f"{name}={value}"
    return f'"{name}={value}"' if compose else f'{name}="{value}"'


def _keep(item: Json, full: bool) -> bool:
    return full or not item.get("optional", False)


def _flag_items(rt: Json) -> list[tuple[str, bool]]:
    """Scalar flags (hostname, mac address, net, privileged) in legacy order: (compose/run key, optional)."""
    out: list[tuple[str, bool]] = []
    for key in ("hostname", "mac_address"):
        if rt.get(key):
            out.append((key, bool(rt[key].get("optional", False))))
    return out


def compose(manifest: Json, settings: Settings | None = None, *, full: bool = True) -> str:
    """The compose snippet body (no code fence)."""
    ctx = _ctx(manifest, settings)
    rt = ctx["rt"]
    mark = " #optional"

    def opt(flag: bool) -> str:
        return mark if flag else ""

    lines = ["---", "services:", f"  {ctx['name']}:", f"    image: {ctx['image_ref']}"]
    lines.append(f"    container_name: {rt['container_name']}")
    for key, optional in _flag_items(rt):
        if _keep({"optional": optional}, full):
            lines.append(f"    {key}: {rt[key]['value']}{opt(optional)}")
    privileged = rt["privileged"]
    if privileged is True or (privileged == OPTIONAL and full):
        lines.append(f"    privileged: true{opt(privileged == OPTIONAL)}")
    caps = [c for c in rt["cap_add"] if _keep(c, full)]
    if caps:
        lines.append("    cap_add:")
        lines.extend(f"      - {c['cap']}{opt(c['optional'])}" for c in caps)
    secs = [s for s in rt["security_opt"] if _keep(s, full)]
    if secs:
        lines.append("    security_opt:")
        lines.extend(f"      - {s['compose']}{opt(s['optional'])}" for s in secs)
    net = rt["network"]
    if net and _keep(net, full):
        lines.append(f"    network_mode: {net['mode']}{opt(bool(net.get('optional')))}")
    common = rt["common_env"]
    env_lines: list[str] = []
    if common is True or (common == OPTIONAL and full):
        env_lines.extend(
            f"      - {_env_pair(e, compose=True)}{opt(common == OPTIONAL)}" for e in rt["common_env_vars"]
        )
    env_lines.extend(f"      - {_env_pair(e, compose=True)}{opt(e['optional'])}" for e in rt["env"] if _keep(e, full))
    if env_lines:
        lines.append("    environment:")
        lines.extend(env_lines)
    vols = [v for v in rt["volumes"] if _keep(v, full)]
    if vols:
        lines.append("    volumes:")
        for v in vols:
            suffix = ":ro" if v["mode"] == "ro" else ""
            lines.append(f"      - {v['host_path']}:{v['path']}{suffix}{opt(v['optional'])}")
    ports = [p for p in rt["ports"] if _keep(p, full)]
    if ports:
        lines.append("    ports:")
        lines.extend(f"      - {p['external']}:{p['internal']}{opt(p['optional'])}" for p in ports)
    devs = [d for d in rt["devices"] if _keep(d, full)]
    if devs:
        lines.append("    devices:")
        lines.extend(f"      - {d['host_path']}:{d['path']}{opt(d['optional'])}" for d in devs)
    for c in rt["custom"]:
        if not _keep(c, full):
            continue
        if c["is_array"]:
            lines.append(f"    {c['compose']}:{opt(c['optional'])}")
            lines.extend(f"      - {v}{opt(c['optional'])}" for v in c["values"])
        else:
            lines.append(f'    {c["compose"]}: "{c["values"][0]}"{opt(c["optional"])}')
    lines.append("    restart: unless-stopped")
    ext = ctx["external_application"]
    if ext:
        lines.append("")
        lines.append("# This container requires an external application to be run separately.")
        lines.append(ext["compose"].rstrip("\n"))
    return "\n".join(lines) + "\n"


def docker_run(manifest: Json, settings: Settings | None = None, *, full: bool = True) -> str:
    """The docker run snippet body (no code fence)."""
    ctx = _ctx(manifest, settings)
    rt = ctx["rt"]

    def opt(flag: bool) -> str:
        return " `#optional`" if flag else ""

    args: list[str] = [f"--name={rt['container_name']}"]
    for key, optional in _flag_items(rt):
        if _keep({"optional": optional}, full):
            args.append(f"--{key.replace('_', '-')}={rt[key]['value']}{opt(optional)}")
    net = rt["network"]
    if net and _keep(net, full):
        args.append(f"--net={net['mode']}{opt(bool(net.get('optional')))}")
    privileged = rt["privileged"]
    if privileged is True or (privileged == OPTIONAL and full):
        args.append(f"--privileged{opt(privileged == OPTIONAL)}")
    args.extend(f"--cap-add={c['cap']}{opt(c['optional'])}" for c in rt["cap_add"] if _keep(c, full))
    args.extend(f"--security-opt {s['run']}{opt(s['optional'])}" for s in rt["security_opt"] if _keep(s, full))
    common = rt["common_env"]
    if common is True or (common == OPTIONAL and full):
        args.extend(f"-e {_env_pair(e)}{opt(common == OPTIONAL)}" for e in rt["common_env_vars"])
    args.extend(f"-e {_env_pair(e)}{opt(e['optional'])}" for e in rt["env"] if _keep(e, full))
    args.extend(f"-p {p['external']}:{p['internal']}{opt(p['optional'])}" for p in rt["ports"] if _keep(p, full))
    for v in rt["volumes"]:
        if _keep(v, full):
            suffix = ":ro" if v["mode"] == "ro" else ""
            args.append(f"-v {v['host_path']}:{v['path']}{suffix}{opt(v['optional'])}")
    args.extend(f"--device {d['host_path']}:{d['path']}{opt(d['optional'])}" for d in rt["devices"] if _keep(d, full))
    for c in rt["custom"]:
        if _keep(c, full):
            args.extend(f'--{c["flag"]}="{v}"{opt(c["optional"])}' for v in c["values"])
    args.append("--restart unless-stopped")
    lines = ["docker run -d \\"] + [f"  {a} \\" for a in args] + [f"  {ctx['image_ref']}"]
    ext = ctx["external_application"]
    if ext:
        lines.append("")
        lines.append("# This container requires an external application to be run separately.")
        lines.append(ext["cli"].rstrip("\n"))
    return "\n".join(lines) + "\n"


def as_dict(manifest: Json, settings: Settings | None = None) -> dict[str, Any]:
    """Structured form for the hub: normalised runtime parameters plus both snippet variants."""
    ctx = _ctx(manifest, settings)
    rt = ctx["rt"]
    return {
        "name": ctx["name"],
        "image": ctx["image_ref"],
        "runtime": {k: v for k, v in rt.items() if k != "common_env_vars"},
        "common_env": rt["common_env_vars"] if rt["common_env"] else [],
        "compose": {"full": compose(ctx), "required": compose(ctx, full=False)},
        "run": {"full": docker_run(ctx), "required": docker_run(ctx, full=False)},
    }


def as_json(manifest: Json, settings: Settings | None = None) -> str:
    return json.dumps(as_dict(manifest, settings), indent=2, sort_keys=False) + "\n"
