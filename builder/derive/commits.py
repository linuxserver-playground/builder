"""
The repository commits between two builds, from GitHub's compare payload (`GET /repos/{owner}/{repo}/compare/
{base}...{head}`): each commit with its author and whether the pipeline made it, and the areas of the repository
the range touched. Pure functions; the caller fetches the payload (the record action with `gh api`, the hub with
its client) and passes it in.

Areas, by path: `manifest` (image.yml), `dockerfile` (Dockerfile, Dockerfile.aarch64), `root` (root/, the files
copied into the image), `generated` (what `builder render` writes: README, AGENTS.md, LICENSE, FUNDING.yml,
root/donate.txt, the workflows), `packages` (package_versions.txt, the table the build commits), `other`. The
first three change the image; the rest never do.
"""

from __future__ import annotations

import re
from typing import Any

Json = dict[str, Any]

GENERATED_FILES = frozenset(
    {
        "README.md",
        "AGENTS.md",
        "LICENSE",
        ".github/FUNDING.yml",
        "root/donate.txt",
        ".github/workflows/build.yml",
        ".github/workflows/pr-report.yml",
    }
)
"""`builder render`'s committed files (render.REPO_FILES, repeated here so derive stays importable on its own)."""
PACKAGE_TABLE = "package_versions.txt"
BOT_SUBJECTS = ("Update package versions", "Update generated files")
"""The two commits the shared workflow makes on a live branch (CONVENTIONS §8)."""
IMAGE_AREAS = ("manifest", "dockerfile", "root")
"""The areas whose change can change the image."""
AREA_ORDER = ("manifest", "dockerfile", "root", "generated", "packages", "other")
_DOCKERFILE = re.compile(r"^Dockerfile(\..+)?$")


def area_of(path: str) -> str:
    if path == "image.yml":
        return "manifest"
    if _DOCKERFILE.match(path):
        return "dockerfile"
    if path in GENERATED_FILES:
        return "generated"
    if path == PACKAGE_TABLE:
        return "packages"
    if path.startswith("root/"):
        return "root"
    return "other"


def is_bot(commit: Json) -> bool:
    """The pipeline's own commit: by a `[bot]` account, or one of the two subjects the shared workflow writes."""
    author = commit.get("author") or {}
    login = str(author.get("login") or "") if isinstance(author, dict) else ""
    inner = commit.get("commit") or {}
    name = str(((inner.get("author") or {}).get("name")) or "")
    subject = str(inner.get("message") or "").split("\n", 1)[0]
    return login.endswith("[bot]") or name.endswith("[bot]") or subject.startswith(BOT_SUBJECTS)


def _commit(commit: Json) -> Json:
    inner = commit.get("commit") or {}
    author = inner.get("author") or {}
    account = commit.get("author") if isinstance(commit.get("author"), dict) else None
    return {
        "sha": str(commit.get("sha") or ""),
        "subject": str(inner.get("message") or "").split("\n", 1)[0].strip(),
        "author": {
            "name": str(author.get("name") or "") or None,
            "login": str(account.get("login") or "") or None if account else None,
        },
        "date": str(author.get("date") or "") or None,
        "url": str(commit.get("html_url") or "") or None,
        "bot": is_bot(commit),
    }


def summarise(compare: Json) -> Json:
    """`diff.commit`'s commit list and areas from the compare payload: `commits` oldest first as GitHub lists them,
    `count` (GitHub's `total_commits`, which may exceed the 250 it lists), `truncated`, and `areas`, the number of
    files changed in the whole range per area (only areas with changes; GitHub lists at most 300 files)."""
    commits = [_commit(c) for c in compare.get("commits") or [] if isinstance(c, dict)]
    total = compare.get("total_commits")
    count = int(total) if isinstance(total, int) else len(commits)
    files = [f for f in compare.get("files") or [] if isinstance(f, dict)]
    counts: dict[str, int] = {}
    for f in files:
        area = area_of(str(f.get("filename") or ""))
        counts[area] = counts.get(area, 0) + 1
    areas = {area: counts[area] for area in AREA_ORDER if area in counts}
    return {
        "commits": commits,
        "count": count,
        "truncated": count > len(commits) or len(files) >= 300,
        "areas": areas,
    }


def describe(summary: Json, old: str, new: str) -> str:
    """One line for `diff.reasons`: how many commits moved the repository and what they touched."""
    count = int(summary.get("count") or 0)
    commits = summary.get("commits") or []
    areas = summary.get("areas") or {}
    span = f"{old[:7]} → {new[:7]}"
    if count == 0:
        return f"repository at a newer commit ({span})"
    noun = "commit" if count == 1 else "commits"
    if commits and all(c.get("bot") for c in commits) and not summary.get("truncated"):
        what = "the pipeline's own" + (" package table update" if set(areas) <= {"packages"} else " generated files")
        return f"{count} {noun} ({span}): {what}"
    touched = [a for a in AREA_ORDER if a in areas]
    names = {
        "manifest": "image.yml",
        "dockerfile": "the Dockerfiles",
        "root": "root/",
        "generated": "generated files",
        "packages": "the package table",
        "other": "other files",
    }
    if not touched:
        return f"{count} {noun} ({span})"
    return f"{count} {noun} ({span}): changed {', '.join(names[a] for a in touched)}"


def changes_image(summary: Json | None) -> bool | None:
    """Whether the range touched an area that can change the image; None when the areas are unknown (no payload,
    or GitHub listed only part of the files)."""
    if not summary or summary.get("truncated"):
        return None
    areas = summary.get("areas") or {}
    return any(a in areas for a in IMAGE_AREAS)
