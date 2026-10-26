"""
The LinuxServer v2 contracts: JSON Schemas for image.yml, mod.yml, the build bundle, the test report and
control-plane events, plus a validator other repos can vendor or install.

    from contracts import validate, load_yaml
    validate("image", load_yaml("image.yml"))     # raises ContractError with path, message and line hint
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

KINDS = {
    "image": "image.schema.json",
    "mod": "mod.schema.json",
    "bundle": "bundle.schema.json",
    "test-report": "test-report.schema.json",
    "control-event": "control-event.schema.json",
    "record": "record.schema.json",
}


class ContractError(ValueError):
    """One or more schema violations, each with a JSON path."""

    def __init__(self, kind: str, errors: list[ValidationError]) -> None:
        self.kind = kind
        self.errors = errors
        super().__init__("\n".join(format_error(e) for e in errors))


def format_error(error: ValidationError) -> str:
    path = "/".join(str(p) for p in error.absolute_path) or "<root>"
    return f"{path}: {error.message}"


def _load_schema(filename: str) -> dict[str, Any]:
    with (SCHEMA_DIR / filename).open(encoding="utf-8") as fh:
        schema: dict[str, Any] = json.load(fh)
    return schema


def registry() -> Registry:
    """Every schema registered under both its $id and its bare filename, so cross-references resolve offline."""
    reg = Registry()
    for filename in KINDS.values():
        schema = _load_schema(filename)
        resource = Resource.from_contents(schema)
        reg = reg.with_resource(schema["$id"], resource).with_resource(filename, resource)
    return reg


def validator(kind: str) -> Draft202012Validator:
    if kind not in KINDS:
        raise KeyError(f"unknown contract kind {kind!r}; one of {sorted(KINDS)}")
    schema = _load_schema(KINDS[kind])
    return Draft202012Validator(schema, registry=registry(), format_checker=Draft202012Validator.FORMAT_CHECKER)


def errors(kind: str, data: Any) -> list[ValidationError]:
    return sorted(validator(kind).iter_errors(data), key=lambda e: list(e.absolute_path))


def validate(kind: str, data: Any) -> None:
    found = errors(kind, data)
    if found:
        raise ContractError(kind, found)


class _Loader(yaml.SafeLoader):
    """SafeLoader that keeps dates as strings: YAML would otherwise turn 2026-10-19 into a date object."""


def _str_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    return str(loader.construct_scalar(node))  # type: ignore[arg-type]


_Loader.add_constructor("tag:yaml.org,2002:timestamp", _str_constructor)


def load_yaml(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_Loader)


def check_files(kind: str, paths: Iterable[str | Path]) -> dict[str, list[str]]:
    """Validate several files; returns {path: [messages]} for the ones that failed."""
    failed: dict[str, list[str]] = {}
    for p in paths:
        found = errors(kind, load_yaml(p))
        if found:
            failed[str(p)] = [format_error(e) for e in found]
    return failed
