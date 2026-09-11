"""Typed, strictly-validated evaluation datasets.

The previous harness passed raw ``dict`` s from JSON straight into the scoring
code, which meant a mistyped key was *silently ignored*. A case whose
``expect_patched`` was spelled ``expected_patched`` asserted nothing and passed.
That is the same failure mode as a deleted test: the suite stays green and the
coverage is gone, and nothing anywhere goes red to tell you.

So datasets are loaded into frozen dataclasses, and loading is strict:

* an unknown key is an error, naming the keys that *are* allowed;
* a missing required key is an error;
* a value of the wrong shape is an error;
* a duplicate or empty case id is an error;
* an empty dataset is an error.

All of those raise :class:`DatasetError` at load time, before a single case
runs, and the eval entry point reports them as a load failure rather than a
score. The project's own rule -- no loosely-structured dictionaries crossing a
stage boundary -- applies to the thing that measures the project too.
"""

from __future__ import annotations

import json
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar

DATASETS = Path(__file__).resolve().parents[1] / "datasets"


class DatasetError(Exception):
    """A dataset file is malformed. Always names the file and the case."""


# --------------------------------------------------------------------------
# case specs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseSpec:
    """Fields every case in every suite carries."""

    id: str
    #: Non-empty when PatchAhead is *expected to fail* this case today, stating
    #: why. The case is scored honestly and reported, but does not fail the
    #: suite -- until it starts passing, which does. See
    #: :class:`~evals.harness.result.CaseStatus`.
    known_gap: str = ""
    #: Free text for the reader of the dataset. Never scored.
    note: str = ""


@dataclass(frozen=True)
class ClassificationCase(CaseSpec):
    """A change document and the reading a correct tool produces."""

    text: str = ""
    expected_kind: str = ""
    #: ``None`` means "this case does not assert that field", which is different
    #: from asserting the empty string.
    expected_symbol: str | None = None
    expected_replacement: str | None = None
    expected_owner: str | None = None
    #: Whether the document *asserts* the owner rather than illustrating it.
    #: This is the distinction that decides whether a receiver mismatch refuses
    #: or merely lowers confidence, so it is worth scoring on its own.
    expected_owner_explicit: bool | None = None

    REQUIRED = ("id", "text", "expected_kind")

    @property
    def expects_refusal(self) -> bool:
        """Whether a correct reading declines to act on this document."""
        return self.expected_kind in ("unsupported", "unknown")


@dataclass(frozen=True)
class SiteCase(CaseSpec):
    """A change plus a repository, and the sites a correct tool patches.

    Shared by the ``impact`` and ``adversarial`` suites: they measure the same
    thing against datasets chosen for different purposes.
    """

    change: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    #: ``path:line`` sites that must be rewritten.
    expect_patched: list[str] = field(default_factory=list)
    #: ``path:line`` sites that must be *found and reported* but not rewritten.
    expect_reported_only: list[str] = field(default_factory=list)
    #: Text that must survive in the patched source. Catches an edit that lands
    #: on the right line and still mangles it.
    expect_source_contains: list[str] = field(default_factory=list)
    expect_symbols: list[str] | None = None
    #: ``path:line`` -> ``high``/``medium``/``low``, for confidence calibration.
    expect_confidence: dict[str, str] = field(default_factory=dict)

    REQUIRED = ("id", "change", "files", "expect_patched")


@dataclass(frozen=True)
class MigrationCase(CaseSpec):
    """A repository, a change document, and what a full engine run must do."""

    change: str = ""
    files: dict[str, str] = field(default_factory=dict)
    expect_success: bool = False
    expect_outcome: str = ""
    #: Text the diff must not touch. Minimality, checked on added/removed lines.
    expect_unchanged: list[str] = field(default_factory=list)
    #: Paths the migration must leave alone entirely. The natural assertion for
    #: a partially-migrated repository, where the already-correct module must
    #: not be edited again and its content is identical to what the patch
    #: legitimately writes elsewhere -- so matching on text cannot express it.
    expect_untouched_files: list[str] = field(default_factory=list)
    #: True when the repository needs no migration at all -- already migrated
    #: code. Producing a diff here is an *unnecessary migration*, which is its
    #: own failure category, distinct from patching the wrong thing.
    expect_no_patch: bool = False
    #: Upper bound on added+removed diff lines. Guards against a handler that
    #: starts rewriting whole functions to accomplish a rename.
    max_changed_lines: int | None = None
    max_changed_files: int | None = None

    REQUIRED = ("id", "change", "files", "expect_success")


@dataclass(frozen=True)
class ValidationCase(CaseSpec):
    """A scenario and the verdict each validation gate must reach.

    ``mode`` decides how the patch under validation is produced:

    ``engine``
        Run the whole engine on ``change``. This is the realistic path, and the
        only one that can exercise the migration assertion end to end.

    ``direct``
        Write ``patch`` into the workspace and validate it against a plan naming
        ``plan_files``. Deterministic handlers cannot emit invalid Python or
        touch a file the plan does not name, so the gates that catch those
        cannot be reached through the engine -- and a gate with no test is a
        gate nobody knows works.
    """

    mode: str = "engine"
    files: dict[str, str] = field(default_factory=dict)
    change: str = ""
    #: ``direct`` mode: path -> replacement source.
    patch: dict[str, str] = field(default_factory=dict)
    #: ``direct`` mode: the files the plan claims it will change. Defaults to
    #: the keys of ``patch`` -- set it to something narrower to force a scope
    #: failure.
    plan_files: list[str] | None = None
    run_tests: bool = True
    test_command: str = ""
    #: Gate name -> required status, e.g. ``{"migration_assertion": "skipped"}``.
    expect_gates: dict[str, str] = field(default_factory=dict)
    expect_outcome: str = ""
    expect_verified: bool | None = None
    expect_passed: bool | None = None
    #: Substring that must appear in the named gate's detail, so a gate cannot
    #: reach the right status for the wrong reason.
    expect_gate_detail: dict[str, str] = field(default_factory=dict)

    REQUIRED = ("id", "files", "expect_gates")

    VALID_MODES = ("engine", "direct")


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

T = TypeVar("T", bound=CaseSpec)

_SIMPLE_TYPES: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "bool": bool,
    "int": int,
    "list": list,
    "dict": dict,
}


def _expected_type(annotation: str) -> tuple[type | tuple[type, ...], bool]:
    """Map a (string) annotation to a runtime check and whether ``None`` is ok.

    Annotations in this module are deliberately simple -- ``str``, ``bool``,
    ``list[str]``, ``str | None`` -- so this stays a lookup rather than a
    reimplementation of ``typing``.
    """
    optional = "None" in annotation
    head = annotation.split("|")[0].strip().split("[")[0].strip()
    # `bool` must be checked before `int`: in Python `True` is an `int`, and a
    # dataset that writes `true` where a count belongs should be rejected.
    return _SIMPLE_TYPES.get(head, object), optional


def _build(spec_cls: type[T], data: dict[str, Any], source: str) -> T:
    allowed = {f.name: f for f in fields(spec_cls)}
    case_id = data.get("id", "<no id>")

    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise DatasetError(
            f"{source}: case {case_id!r} has unknown key(s) {', '.join(unknown)}. "
            f"Allowed keys: {', '.join(sorted(allowed))}. "
            f"(A key the harness does not know is silently unscored, so it is rejected.)"
        )

    for name in getattr(spec_cls, "REQUIRED", ()):
        if name not in data:
            raise DatasetError(f"{source}: case {case_id!r} is missing required key {name!r}")

    kwargs: dict[str, Any] = {}
    for name, spec_field in allowed.items():
        if name not in data:
            if spec_field.default is MISSING and spec_field.default_factory is MISSING:
                raise DatasetError(f"{source}: case {case_id!r} is missing key {name!r}")
            continue
        value = data[name]
        expected, optional = _expected_type(str(spec_field.type))
        if value is None:
            if not optional:
                raise DatasetError(f"{source}: case {case_id!r} key {name!r} may not be null")
        elif expected is bool:
            if not isinstance(value, bool):
                raise DatasetError(
                    f"{source}: case {case_id!r} key {name!r} must be a boolean, "
                    f"got {type(value).__name__}"
                )
        elif expected is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise DatasetError(
                    f"{source}: case {case_id!r} key {name!r} must be an integer, "
                    f"got {type(value).__name__}"
                )
        elif expected is not object and not isinstance(value, expected):
            raise DatasetError(
                f"{source}: case {case_id!r} key {name!r} must be "
                f"{expected.__name__}, got {type(value).__name__}"
            )
        kwargs[name] = value

    return spec_cls(**kwargs)


def load(suite: str, spec_cls: type[T], *, root: Path | None = None) -> list[T]:
    """Load and validate one suite's dataset.

    Raises :class:`DatasetError` on anything malformed rather than skipping it.
    A dataset that half-loads measures half of what it claims to.
    """
    path = (root or DATASETS) / suite / "cases.json"
    source = f"evals/datasets/{suite}/cases.json"
    if not path.exists():
        raise DatasetError(f"{source}: dataset file not found")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{source}: not valid JSON: {exc}") from exc

    if not isinstance(payload, dict) or "cases" not in payload:
        raise DatasetError(f"{source}: top level must be an object with a `cases` list")
    raw_cases = payload["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise DatasetError(f"{source}: `cases` must be a non-empty list")

    cases: list[T] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise DatasetError(f"{source}: case at index {index} is not an object")
        case = _build(spec_cls, raw, source)
        if not case.id:
            raise DatasetError(f"{source}: case at index {index} has an empty id")
        if case.id in seen:
            raise DatasetError(
                f"{source}: duplicate case id {case.id!r}. Ids name results in the "
                f"report, so duplicates make a failure impossible to locate."
            )
        seen.add(case.id)
        cases.append(case)
    return cases


def describe(suite: str, *, root: Path | None = None) -> str:
    """The dataset's own one-line description, rendered above its metrics."""
    path = (root or DATASETS) / suite / "cases.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover - load() reports it
        return ""
    return str(payload.get("description", ""))
