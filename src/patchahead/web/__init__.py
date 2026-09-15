"""The optional web UI: a view over the engine, served on localhost.

Lives inside the package rather than beside it so that the HTML ships with the
wheel. ``patchahead demo`` has to work from an install in an empty directory,
and a template that only exists in a git checkout does not.

The extra for this module is ``[web]`` (FastAPI and uvicorn). The demo needs
``[demo]``, which adds the pytest that turns a patch into a verified migration.
"""

from patchahead.web.server import create_app, main, static_root

__all__ = ["create_app", "main", "static_root"]
