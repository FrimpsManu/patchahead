"""The optional web UI: a view over the engine, served on localhost.

Lives inside the package rather than beside it so that the HTML ships with the
wheel. ``patchahead demo`` has to work from ``pip install patchahead[web]`` in
an empty directory, and a template that only exists in a git checkout does not.
"""

from patchahead.web.server import create_app, main, static_root

__all__ = ["create_app", "main", "static_root"]
