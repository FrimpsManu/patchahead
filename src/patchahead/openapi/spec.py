"""Reading an OpenAPI document into the parts a comparison needs.

Two versions of a spec are compared by what a client depends on: the
properties of each named schema, and each operation's ``operationId`` and
parameters. This module flattens a document into exactly that, for OpenAPI 3.0
and 3.1 (``components.schemas``) and Swagger 2.0 (``definitions``).

A property is compared by its **shape**: the type a client receives, written
the same way however the spec spells it -- ``string/date-time``,
``ref:Customer``, ``array<ref:LineItem>``. ``nullable`` and a 3.1 ``"null"``
type are dropped from it: a rename that also makes a field nullable is still a
rename. ``allOf`` members are merged into the schema that lists them, following
``$ref``\\ s within the document; a reference to another file is not followed,
and its properties are not read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


class SpecError(Exception):
    """The document could not be read as an OpenAPI or Swagger spec."""


@dataclass(frozen=True)
class Property:
    """A named value a client reads or sends: a schema property or a parameter."""

    name: str
    shape: str
    deprecated: bool = False
    description: str = ""


@dataclass
class Schema:
    name: str
    properties: dict[str, Property] = field(default_factory=dict)


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    operation_id: str = ""
    deprecated: bool = False
    #: ``(location, name)`` -> the parameter; ``location`` is ``query``, ``path``,
    #: ``header`` or ``cookie``.
    parameters: dict[tuple[str, str], Property] = field(default_factory=dict)
    required: frozenset[tuple[str, str]] = frozenset()

    @property
    def label(self) -> str:
        return f"{self.method.upper()} {self.path}"


@dataclass
class Spec:
    title: str = ""
    version: str = ""
    schemas: dict[str, Schema] = field(default_factory=dict)
    operations: dict[tuple[str, str], Operation] = field(default_factory=dict)


def load(path: str | Path) -> Spec:
    """Read a spec file: JSON, or YAML when PyYAML is installed."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"{path}: {exc.strerror or exc}") from exc
    return read(parse(text, path.name), str(path))


def parse(text: str, name: str = "spec") -> dict[str, Any]:
    """The document as a mapping. JSON first; YAML for anything else."""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SpecError(f"{name}: not valid JSON ({exc.msg}, line {exc.lineno})") from exc
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise SpecError(
                f"{name}: reading a YAML spec needs PyYAML (pip install 'patchahead[yaml]'); "
                "a JSON spec works without it"
            ) from exc
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SpecError(f"{name}: not valid YAML ({exc})") from exc
    if not isinstance(document, dict):
        raise SpecError(f"{name}: a spec is a mapping at the top level")
    return document


def read(document: dict[str, Any], name: str = "spec") -> Spec:
    """Flatten a parsed document into schemas and operations."""
    if "openapi" in document:
        schemas = (document.get("components") or {}).get("schemas") or {}
    elif "swagger" in document:
        schemas = document.get("definitions") or {}
    else:
        raise SpecError(f"{name}: no `openapi` or `swagger` version field; is this a spec?")

    info = document.get("info") or {}
    spec = Spec(title=str(info.get("title", "")), version=str(info.get("version", "")))
    resolver = _Resolver(document)
    for schema_name, schema in sorted(schemas.items()):
        if isinstance(schema, dict):
            spec.schemas[schema_name] = Schema(
                schema_name, resolver.properties(schema, seen=frozenset({schema_name}))
            )

    for path, item in sorted((document.get("paths") or {}).items()):
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in _METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            parameters: dict[tuple[str, str], Property] = {}
            required: set[tuple[str, str]] = set()
            # An operation's own parameter overrides a path-level one.
            for raw in [*shared, *(operation.get("parameters") or [])]:
                param = resolver.resolve(raw)
                if not isinstance(param, dict) or "name" not in param or "in" not in param:
                    continue
                key = (str(param["in"]), str(param["name"]))
                schema = param.get("schema") if "schema" in param else param
                parameters[key] = Property(
                    name=key[1],
                    shape=resolver.shape(schema),
                    deprecated=bool(param.get("deprecated")),
                    description=str(param.get("description", "")),
                )
                if param.get("required"):
                    required.add(key)
                else:
                    required.discard(key)
            spec.operations[(method, path)] = Operation(
                method=method,
                path=path,
                operation_id=str(operation.get("operationId", "")),
                deprecated=bool(operation.get("deprecated")),
                parameters=parameters,
                required=frozenset(required),
            )
    return spec


class _Resolver:
    """Follows ``#/...`` references within one document."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document

    def resolve(self, node: Any, depth: int = 0) -> Any:
        if not isinstance(node, dict) or "$ref" not in node or depth > 20:
            return node
        ref = str(node["$ref"])
        if not ref.startswith("#/"):
            return {}
        target: Any = self.document
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return {}
            target = target[part]
        return self.resolve(target, depth + 1)

    def properties(self, schema: dict[str, Any], seen: frozenset[str]) -> dict[str, Property]:
        found: dict[str, Property] = {}
        for member in schema.get("allOf") or []:
            ref = member.get("$ref", "") if isinstance(member, dict) else ""
            name = _ref_name(ref)
            if name and name in seen:
                continue
            resolved = self.resolve(member)
            if isinstance(resolved, dict):
                found.update(self.properties(resolved, seen | {name} if name else seen))
        for name, prop in (schema.get("properties") or {}).items():
            raw = prop if isinstance(prop, dict) else {}
            # `deprecated` and the description are read from the property as
            # written, beside any `$ref`, which is where a spec marks them.
            found[str(name)] = Property(
                name=str(name),
                shape=self.shape(raw),
                deprecated=bool(raw.get("deprecated")),
                description=str(raw.get("description", "")),
            )
        return found

    def shape(self, schema: Any, depth: int = 0) -> str:
        """The type a client receives, spelled one way."""
        if not isinstance(schema, dict) or depth > 10:
            return "any"
        if "$ref" in schema:
            name = _ref_name(str(schema["$ref"]))
            return f"ref:{name}" if name else "any"
        for combiner in ("oneOf", "anyOf"):
            if combiner in schema:
                options = sorted(
                    {self.shape(option, depth + 1) for option in schema[combiner]} - {"null"}
                )
                return options[0] if len(options) == 1 else f"{combiner}<{'|'.join(options)}>"
        if "allOf" in schema and len(schema["allOf"]) == 1:
            return self.shape(schema["allOf"][0], depth + 1)
        kind = schema.get("type", "object" if "properties" in schema else "any")
        if isinstance(kind, list):
            kinds = sorted(str(k) for k in kind if k != "null")
            kind = kinds[0] if len(kinds) == 1 else "|".join(kinds)
        if kind == "array":
            return f"array<{self.shape(schema.get('items'), depth + 1)}>"
        fmt = schema.get("format")
        return f"{kind}/{fmt}" if fmt else str(kind)


def _ref_name(ref: str) -> str:
    """``#/components/schemas/Customer`` -> ``Customer``; ``""`` for other refs."""
    for prefix in ("#/components/schemas/", "#/definitions/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ""
