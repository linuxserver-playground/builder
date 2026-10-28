"""
Layer attribution: which layers of a build came from the base image, and which Dockerfile instruction made
each of the others. The ci harness only knows `from_base` when the base image is local to the runner, which
it usually is not, so it is derived from the base's layer digests as the registry publishes them (record.json
`base`, or `registry.base_layers` for a release without one). Dockerfile lines come from matching each layer's
`created_by` (the instruction as BuildKit recorded it) against the Dockerfile text in build.json. Pure functions;
`documents.layers` shapes a build's `layers.<arch>.json` with them, for the hub's API and the copy on the
repository's Pages. Moved here from the hub so both use one implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

Json = dict[str, Any]
FromBaseSource = Literal["registry", "count", "harness"]

KEYWORDS = (
    "FROM",
    "RUN",
    "COPY",
    "ADD",
    "ENV",
    "ARG",
    "LABEL",
    "EXPOSE",
    "VOLUME",
    "WORKDIR",
    "USER",
    "ENTRYPOINT",
    "CMD",
    "HEALTHCHECK",
    "SHELL",
    "STOPSIGNAL",
    "ONBUILD",
    "MAINTAINER",
)
ATTRIBUTED = {"RUN", "COPY", "ADD", "ENV", "LABEL"}
"""Instructions a layer is attributed to; the rest never produce one, or are metadata BuildKit folds elsewhere."""

_KEYWORD = re.compile(r"^(" + "|".join(KEYWORDS) + r")\b\s*(.*)$", re.IGNORECASE | re.DOTALL)
_RUN_PREFIX = re.compile(r"^\|\d+\s+(?:\S+=\S*\s+)*")
"""`|3 A=1 B=2 ` : the build args BuildKit records before a RUN command."""
_SHELL = re.compile(r"^/bin/sh -c\s+")
_NOP = re.compile(r"^#\(nop\)\s*")
_BUILDKIT = re.compile(r"\s*#\s*buildkit\s*$")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


@dataclass(frozen=True)
class Instruction:
    keyword: str
    start: int
    """1-based first line."""
    end: int
    """1-based last line (continuations followed)."""
    text: str
    """The instruction with continuations joined, keyword included."""


def parse_dockerfile(text: str) -> list[Instruction]:
    """Every instruction of a Dockerfile with its line range; comments and blank lines are skipped."""
    lines = text.splitlines()
    out: list[Instruction] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        start = i
        parts = [line]
        while parts[-1].rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            parts.append(lines[i])
        end = i
        pieces = [p.rstrip()[:-1] if p.rstrip().endswith("\\") else p for p in parts[:-1]] + [parts[-1]]
        joined = " ".join(" ".join(pieces).split())
        heredoc = _HEREDOC.search(joined)
        if heredoc and not joined.strip().endswith(heredoc.group(1)):
            terminator = heredoc.group(1)
            while i + 1 < len(lines) and lines[i].strip() != terminator:
                i += 1
            end = i
        match = _KEYWORD.match(joined)
        if match:
            out.append(Instruction(match.group(1).upper(), start + 1, end + 1, joined))
        i += 1
    return out


def final_stage(instructions: list[Instruction]) -> list[Instruction]:
    """The instructions after the last FROM: only those produce layers of the published image."""
    starts = [n for n, ins in enumerate(instructions) if ins.keyword == "FROM"]
    return instructions[starts[-1] + 1 :] if starts else instructions


def _normalise(body: str) -> str:
    """Whitespace collapsed, quotes dropped: enough to compare an instruction with BuildKit's record of it."""
    return " ".join(body.replace('"', "").replace("'", "").split())


def created_by_parts(created_by: str) -> tuple[str, str] | None:
    """(keyword, normalised body) of a layer's `created_by`, or None when it is not a Dockerfile instruction."""
    text = _BUILDKIT.sub("", created_by.strip())
    text = _NOP.sub("", text)
    match = _KEYWORD.match(text)
    if not match:
        return None
    keyword, body = match.group(1).upper(), match.group(2)
    if keyword == "RUN":
        body = _SHELL.sub("", _RUN_PREFIX.sub("", body.strip()))
    return keyword, _normalise(body)


def instruction_body(instruction: Instruction) -> str:
    body = instruction.text[len(instruction.keyword) :]
    if instruction.keyword == "RUN":
        body = body.strip()
        if body.startswith("["):
            return _normalise(body)
    return _normalise(body)


def matches(created: str, instruction: Instruction) -> bool:
    """A confident match: equal after normalisation, or one is a long prefix of the other (heredocs, trimmed args)."""
    expected = instruction_body(instruction)
    if not created or not expected:
        return created == expected
    if created == expected:
        return True
    shorter, longer = sorted((created, expected), key=len)
    return len(shorter) >= 40 and longer.startswith(shorter)


def attribute(layers: list[Json], dockerfile: str | None) -> list[list[int] | None]:
    """
    `[start, end]` line numbers per layer, matched sequentially from the last layer and the last instruction
    backwards (the image's own layers are the last ones, so unknown base layers fall off the front), None where
    no confident match is found. Layers flagged `from_base` are skipped.
    """
    out: list[list[int] | None] = [None] * len(layers)
    if not dockerfile:
        return out
    instructions = [ins for ins in final_stage(parse_dockerfile(dockerfile)) if ins.keyword in ATTRIBUTED]
    pointer = len(instructions) - 1
    for n in range(len(layers) - 1, -1, -1):
        layer = layers[n]
        if layer.get("from_base") or not layer.get("created_by"):
            continue
        parts = created_by_parts(str(layer["created_by"]))
        if parts is None:
            continue
        keyword, body = parts
        for k in range(pointer, -1, -1):
            if instructions[k].keyword == keyword and matches(body, instructions[k]):
                out[n] = [instructions[k].start, instructions[k].end]
                pointer = k - 1
                break
    return out


def annotate(layers: list[Json], dockerfile: str | None, base: Json | None, arch: str) -> tuple[list[Json], Json]:
    """
    Copies of the layer records with `from_base` and `dockerfile_lines` set where they can be told, and a
    summary: `{"base_layers": N | None, "from_base_source": "registry" | "count" | "harness" | None}`.

    `from_base` is true when the layer's compressed digest is in the base's layer list for this architecture
    (`registry`). When the base is known for other architectures only, the first N layers are taken, N being
    the base's layer count (`count`). Otherwise the harness's own flags stand (`harness`) or the field is left
    unset.
    """
    out = [dict(layer) for layer in layers]
    source: FromBaseSource | None = None
    base_layers: int | None = None
    platforms = (base or {}).get("platforms", {})
    digests = set(platforms.get(arch, {}).get("layers", []))
    if digests:
        source = "registry"
        for layer in out:
            layer["from_base"] = layer.get("digest") in digests
        base_layers = sum(1 for layer in out if layer["from_base"])
    elif platforms:
        counts = {len(p.get("layers", [])) for p in platforms.values()}
        if len(counts) == 1:
            base_layers = counts.pop()
            source = "count"
            for layer in out:
                layer["from_base"] = bool(layer.get("index", 0) < base_layers)
    elif any("from_base" in layer for layer in out):
        source = "harness"
        base_layers = sum(1 for layer in out if layer.get("from_base"))
    for layer, lines in zip(out, attribute(out, dockerfile), strict=True):
        if lines is not None:
            layer["dockerfile_lines"] = lines
    return out, {"base_layers": base_layers, "from_base_source": source}
