"""Change ingestion: turning upstream change documents into BreakingChanges.

Importing this package registers the built-in parsers. Adding a format (OpenAPI
spec diffs, a vendor changelog API) means adding a module here that subclasses
:class:`~patchahead.ingest.base.ChangeParser` and calls
:func:`~patchahead.ingest.base.register`.
"""

from patchahead.ingest.base import (
    ChangeDocument,
    ChangeParser,
    IngestError,
    parse_document,
    parse_file,
    register,
    registered,
)

# Import for the registration side effect. Order matters only in that later
# registrations are tried first; the two built-ins select on disjoint suffixes.
from patchahead.ingest import markdown as markdown  # noqa: E402,F401  isort:skip
from patchahead.ingest import structured as structured  # noqa: E402,F401  isort:skip

__all__ = [
    "ChangeDocument",
    "ChangeParser",
    "IngestError",
    "parse_document",
    "parse_file",
    "register",
    "registered",
]
