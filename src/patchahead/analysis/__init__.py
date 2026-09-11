"""Static analysis: parsing Python source and applying range-based edits."""

from patchahead.analysis.edits import (
    EditError,
    apply_edits,
    combined_diff,
    is_parseable,
    unified_diff,
)
from patchahead.analysis.index import RepoIndex, build, discover_python_files, is_test_path
from patchahead.analysis.python_ast import (
    MODULE_SCOPE,
    AttributeAccess,
    CallSite,
    GetCallAccess,
    ModuleAnalysis,
    ParseError,
    SourceRange,
    SubscriptAccess,
    analyze_source,
    base_name,
    receiver_name,
)

__all__ = [
    "MODULE_SCOPE",
    "AttributeAccess",
    "CallSite",
    "EditError",
    "GetCallAccess",
    "ModuleAnalysis",
    "ParseError",
    "RepoIndex",
    "SourceRange",
    "SubscriptAccess",
    "analyze_source",
    "apply_edits",
    "base_name",
    "build",
    "combined_diff",
    "discover_python_files",
    "is_parseable",
    "is_test_path",
    "receiver_name",
    "unified_diff",
]
