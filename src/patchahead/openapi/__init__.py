"""Breaking changes read from two versions of an OpenAPI spec.

``spec.load`` reads a spec (OpenAPI 3.x or Swagger 2.0, JSON or YAML) into its
schemas and operations; ``compare.compare`` turns two of those into
:class:`BreakingChange` objects that the rest of PatchAhead migrates like any
other.
"""

from patchahead.openapi.compare import SpecDiff, compare, snake_case
from patchahead.openapi.spec import Operation, Property, Schema, Spec, SpecError, load, parse, read

__all__ = [
    "Operation",
    "Property",
    "Schema",
    "Spec",
    "SpecDiff",
    "SpecError",
    "compare",
    "load",
    "parse",
    "read",
    "snake_case",
]
