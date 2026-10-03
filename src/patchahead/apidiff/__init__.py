"""Breaking changes read from two versions of a library, not from its release notes.

``surface.read`` parses a library's public API without importing it;
``compare.compare`` turns two of those into :class:`BreakingChange` objects that
the rest of PatchAhead migrates like any other; ``download.fetch`` gets a published
version from PyPI as a wheel, which is unpacked and read -- never installed.
"""

from patchahead.apidiff.compare import ApiDiff, compare
from patchahead.apidiff.download import ApiDiffError, fetch, unpack
from patchahead.apidiff.surface import Member, Param, Surface, read

__all__ = [
    "ApiDiff",
    "ApiDiffError",
    "Member",
    "Param",
    "Surface",
    "compare",
    "fetch",
    "read",
    "unpack",
]
