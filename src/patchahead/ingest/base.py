"""The change-parser interface, and the format registry.

A parser turns one change document into zero or more
:class:`~patchahead.domain.change.BreakingChange` objects. Adding a format
(OpenAPI spec diffs, a vendor's changelog API, an SDK's ``CHANGELOG.md``
convention) means writing one class and registering it here -- no other module
changes.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from patchahead.domain.change import BreakingChange

log = logging.getLogger(__name__)


class IngestError(Exception):
    """Raised when a change document exists but cannot be read or parsed."""


@dataclass
class ChangeDocument:
    """A change document loaded from disk (or supplied directly)."""

    text: str
    #: Where it came from, for error messages and evidence. May be "<string>".
    path: str = "<string>"
    #: Lowercased file extension, e.g. ".md". Used for parser selection.
    suffix: str = ""

    @classmethod
    def load(cls, path: str | Path) -> ChangeDocument:
        file_path = Path(path).expanduser()
        if not file_path.exists():
            raise IngestError(f"no such change document: {file_path}")
        if file_path.is_dir():
            raise IngestError(f"change document is a directory: {file_path}")
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise IngestError(f"{file_path} is not valid UTF-8: {exc.reason}") from exc
        except OSError as exc:
            raise IngestError(f"cannot read {file_path}: {exc}") from exc
        if not text.strip():
            raise IngestError(f"change document is empty: {file_path}")
        return cls(text=text, path=str(file_path), suffix=file_path.suffix.lower())

    def lines(self) -> list[str]:
        return self.text.splitlines()


class ChangeParser(ABC):
    """Turns a :class:`ChangeDocument` into breaking changes."""

    #: Stable identifier, recorded on every change this parser produces.
    name: str = ""

    @abstractmethod
    def supports(self, document: ChangeDocument) -> bool:
        """Whether this parser can handle the document."""

    @abstractmethod
    def parse(self, document: ChangeDocument) -> list[BreakingChange]:
        """Extract breaking changes. May return an empty list."""


_PARSERS: list[ChangeParser] = []


def register(parser: ChangeParser) -> ChangeParser:
    """Add a parser to the registry. Later registrations are tried first."""
    _PARSERS.insert(0, parser)
    return parser


def registered() -> list[ChangeParser]:
    return list(_PARSERS)


def parse_document(document: ChangeDocument) -> list[BreakingChange]:
    """Parse with the first registered parser that supports the document.

    Raises :class:`IngestError` when no parser claims it, rather than guessing.
    """
    for parser in _PARSERS:
        if parser.supports(document):
            log.debug("parsing %s with the %s parser", document.path, parser.name)
            return parser.parse(document)
    raise IngestError(
        f"no parser supports {document.path}. Supported formats: "
        f"Markdown/plain text (.md, .txt, .rst), JSON (.json), YAML (.yaml, .yml)."
    )


def parse_file(path: str | Path) -> list[BreakingChange]:
    """Load and parse a change document from disk."""
    return parse_document(ChangeDocument.load(path))
