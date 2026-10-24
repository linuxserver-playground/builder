"""
Admonitions in manifest Markdown are written as ``?+severity[Title](|text|)``, the same shorthand the legacy
readme-vars used. README output converts them to GitHub alerts (``> [!NOTE]``), the docs page to mkdocs
``!!! note`` blocks. This mirrors the reference ``admonition.py`` filter and the ``noter`` macro.
"""

from __future__ import annotations

import re
from typing import Literal

Flavour = Literal["gfm", "mkdocs"]

GFM_SEVERITIES = {
    "abstract": "NOTE",
    "bug": "IMPORTANT",
    "danger": "WARNING",
    "failure": "CAUTION",
    "info": "NOTE",
    "note": "NOTE",
    "question": "TIP",
    "quote": "NOTE",
    "success": "TIP",
    "tip": "TIP",
    "warning": "WARNING",
}

MKDOCS_SEVERITIES = {
    "abstract": "abstract",
    "bug": "bug",
    "danger": "danger",
    "failure": "failure",
    "info": "info",
    "note": "note",
    "question": "question",
    "quote": "quote",
    "success": "success",
    "tip": "tip",
    "warning": "warning",
}

_SHORTHAND = re.compile(r"\?\+(?P<severity>\w+)(?:\[(?P<title>[^\]\n]*)\])?\(\|(?P<message>.*?)\|\)", re.DOTALL)


def admonition(
    text: str,
    flavour: Flavour = "gfm",
    severity: str = "note",
    title: str | None = None,
    collapse: str | bool | None = None,
) -> str:
    """Render one admonition block in the given Markdown flavour."""
    text = text.strip("\n")
    if severity == "deprecation":
        return f"# DEPRECATION NOTICE\n\n{text}"
    if flavour == "mkdocs":
        kind = MKDOCS_SEVERITIES.get(severity, "note")
        if collapse is None or collapse is False:
            marker = "!!!"
        elif collapse == "expanded":
            marker = "???+"
        else:
            marker = "???"
        head = f'{marker} {kind} "{title}"' if title else f"{marker} {kind}"
        body = text.replace("\n", "\n    ")
        return f"{head}\n\n    {body}"
    if collapse is not None and collapse is not False:
        return f"<details>\n<summary>{title or ''}</summary>\n\n{text}\n</details>"
    kind = GFM_SEVERITIES.get(severity, "NOTE")
    body = "\n".join(f"> {line}".rstrip() for line in text.split("\n"))
    head = f"> [!{kind}]"
    if title:
        head += f"\n> **{title}**\n>"
    return f"{head}\n{body}"


def convert(text: str, flavour: Flavour = "gfm") -> str:
    """Replace every ``?+severity[Title](|text|)`` shorthand in text with a rendered admonition."""

    def _sub(match: re.Match[str]) -> str:
        return admonition(match.group("message"), flavour, match.group("severity"), match.group("title"))

    return _SHORTHAND.sub(_sub, text)
