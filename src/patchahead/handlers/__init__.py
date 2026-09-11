"""Migration handlers, one per migration family.

Importing this package registers the built-in handlers. To add a family, write a
module here that subclasses
:class:`~patchahead.handlers.base.MigrationHandler`, call
:func:`~patchahead.handlers.base.register` at the bottom of it, add a
:class:`~patchahead.domain.change.ChangeKind` member, and import the module
below. See ``docs/migrations.md`` for the full walkthrough.
"""

from patchahead.handlers.base import (
    MigrationHandler,
    find_handler,
    register,
    registered,
    selftest_registry,
    supported_kinds,
)

# Imported for their registration side effects. Registration order is match
# order; the built-ins claim disjoint kinds, so the order among them is
# immaterial and is kept alphabetical.
from patchahead.handlers import field_rename as field_rename  # noqa: E402,F401  isort:skip
from patchahead.handlers import kwarg_rename as kwarg_rename  # noqa: E402,F401  isort:skip
from patchahead.handlers import method_rename as method_rename  # noqa: E402,F401  isort:skip
from patchahead.handlers import pagination as pagination  # noqa: E402,F401  isort:skip

__all__ = [
    "MigrationHandler",
    "find_handler",
    "register",
    "registered",
    "selftest_registry",
    "supported_kinds",
]
