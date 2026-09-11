"""The LLM path: the model proposes, and everything it says is checked.

The Anthropic API is the one thing mocked in this suite -- it is remote, paid,
and non-deterministic. The mock is a stub client returning canned text, so the
code under test is the real prompt assembly, parsing, and rejection logic.
"""

from __future__ import annotations

import json

import pytest

from patchahead.config import Config
from patchahead.domain.change import BreakingChange, ChangeKind, PaginationContract
from patchahead.domain.impact import (
    AccessKind,
    CodeReference,
    Confidence,
    ImpactFinding,
    ImpactReport,
)
from patchahead.domain.plan import MigrationPlan
from patchahead.llm.client import LLMError, LLMResponse
from patchahead.llm.proposer import (
    LLMProposer,
    _strip_fences,
    build_prompt,
    find_function_span,
)
from patchahead.workspace import Repository, Workspace

LOOP = '''
"""Sync."""


def sync(api, log):
    page = 1
    out = []
    while True:
        r = api.get_orders(page=page)
        out.extend(r["orders"])
        log.info("page %d", page)
        if page >= r["total_pages"]:
            break
        page += 1
    return out
'''

MIGRATED = """def sync(api, log):
    cursor = None
    out = []
    while True:
        r = api.get_orders(cursor=cursor)
        out.extend(r["orders"])
        log.info("cursor %s", cursor)
        if not r.get("has_more"):
            break
        cursor = r.get("next_cursor")
    return out"""


# A function whose contract uses every construct `FunctionContract` compares:
# a decorator, `async`, a positional-only parameter, positional parameters with
# defaults, `*args`, keyword-only parameters both with and without a default,
# `**kwargs`, annotations throughout, and a return annotation.
RICH_ORIGINAL = """
import functools


@functools.cache
async def sync_orders(
    account,
    /,
    limit: int = 10,
    label: str = "orders",
    *extra,
    timeout: float = 1.0,
    retries: int,
    **options,
) -> list[dict]:
    page = 1
    out = []
    while True:
        r = await account.get_orders(page=page)
        out.extend(r["orders"])
        if page >= r["total_pages"]:
            break
        page += 1
    return out
"""

#: The same contract with a migrated body. Every mutation below is derived from
#: this, so a rejection can only be caused by the contract change under test.
RICH_MIGRATED = """@functools.cache
async def sync_orders(
    account,
    /,
    limit: int = 10,
    label: str = "orders",
    *extra,
    timeout: float = 1.0,
    retries: int,
    **options,
) -> list[dict]:
    cursor = None
    out = []
    while True:
        r = await account.get_orders(cursor=cursor)
        out.extend(r["orders"])
        if not r.get("has_more"):
            break
        cursor = r.get("next_cursor")
    return out"""


class StubClient:
    """A stand-in for LLMClient that returns canned text or raises."""

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> LLMResponse:
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return LLMResponse(text=self.text, model="stub")


def response(can_migrate=True, functions=None, reasoning="because") -> str:
    return json.dumps(
        {
            "can_migrate": can_migrate,
            "reasoning": reasoning,
            "functions": functions if functions is not None else [],
        }
    )


@pytest.fixture
def scene(make_repo):
    """A workspace with an unmigratable loop, plus the report and blocked plan."""
    root = make_repo({"app/sync.py": LOOP, "conftest.py": ""})
    repository = Repository.open(root)
    workspace = Workspace.materialize(repository)
    index = workspace.index()

    change = BreakingChange(
        title="Pagination is now cursor-based",
        kind=ChangeKind.PAGINATION_PAGE_TO_CURSOR,
        pagination=PaginationContract(),
    )
    report = ImpactReport(
        change=change,
        findings=[
            ImpactFinding(
                reference=CodeReference(path="app/sync.py", line=8),
                symbol="sync",
                matched_contract="while True: ... page=page",
                access=AccessKind.PAGE_LOOP,
                reason="page loop",
                confidence=Confidence.HIGH,
                patchable=False,
            )
        ],
    )
    plan = MigrationPlan(
        change=change,
        handler="pagination_page_to_cursor",
        blocked_reason="`page` is also used outside the loop's bookkeeping",
    )
    try:
        yield change, report, index, workspace, plan
    finally:
        workspace.cleanup()


@pytest.fixture
def rich_scene(make_repo):
    """The same setup, over a function with a full signature and a decorator."""
    root = make_repo({"app/sync.py": RICH_ORIGINAL, "conftest.py": ""})
    repository = Repository.open(root)
    workspace = Workspace.materialize(repository)
    index = workspace.index()

    change = BreakingChange(
        title="Pagination is now cursor-based",
        kind=ChangeKind.PAGINATION_PAGE_TO_CURSOR,
        pagination=PaginationContract(),
    )
    report = ImpactReport(
        change=change,
        findings=[
            ImpactFinding(
                reference=CodeReference(path="app/sync.py", line=18),
                symbol="sync_orders",
                matched_contract="while True: ... page=page",
                access=AccessKind.PAGE_LOOP,
                reason="page loop",
                confidence=Confidence.HIGH,
                patchable=False,
            )
        ],
    )
    plan = MigrationPlan(
        change=change,
        handler="pagination_page_to_cursor",
        blocked_reason="`page` is also used outside the loop's bookkeeping",
    )
    try:
        yield change, report, index, workspace, plan
    finally:
        workspace.cleanup()


def propose(scene, stub):
    change, report, index, workspace, plan = scene
    proposer = LLMProposer(Config(), client=stub)
    return proposer.propose(change, report, index, workspace, plan)


def propose_rich(rich_scene, new_source: str):
    """Send one proposed replacement for `sync_orders` through the real path."""
    stub = StubClient(
        response(
            functions=[{"path": "app/sync.py", "function": "sync_orders", "new_source": new_source}]
        )
    )
    return propose(rich_scene, stub)


class TestPromptAssembly:
    def test_sends_only_the_affected_functions(self, scene):
        change, report, index, workspace, plan = scene
        span = find_function_span(index.modules["app/sync.py"], "sync")

        prompt = build_prompt(change, [span], None, plan.blocked_reason)

        assert "def sync(api, log):" in prompt
        assert '"""Sync."""' not in prompt, "module-level content is not sent"

    def test_includes_the_reason_the_deterministic_path_declined(self, scene):
        change, report, index, workspace, plan = scene
        span = find_function_span(index.modules["app/sync.py"], "sync")

        prompt = build_prompt(change, [span], None, plan.blocked_reason)

        assert plan.blocked_reason in prompt

    def test_refuses_to_send_more_source_than_the_limit(self, scene, monkeypatch):
        from patchahead.llm import proposer as proposer_module

        monkeypatch.setattr(proposer_module, "MAX_SOURCE_CHARS", 10)
        stub = StubClient(response(functions=[]))

        result = propose(scene, stub)

        assert stub.calls == [], "nothing was sent"
        assert "above the 10-character limit" in result.error


class TestAcceptedProposals:
    def test_a_valid_proposal_is_applied_and_diffed(self, scene):
        stub = StubClient(
            response(
                functions=[{"path": "app/sync.py", "function": "sync", "new_source": MIGRATED}]
            )
        )

        result = propose(scene, stub)

        assert result.ok, result.error
        assert result.engine == "llm"
        assert "cursor = None" in result.diff
        assert result.changed_files == ["app/sync.py"]

    def test_markdown_fences_are_tolerated(self, scene):
        payload = response(
            functions=[{"path": "app/sync.py", "function": "sync", "new_source": MIGRATED}]
        )
        stub = StubClient(f"```json\n{payload}\n```")

        assert propose(scene, stub).ok

    def test_strip_fences_leaves_plain_json_alone(self):
        assert _strip_fences('{"a": 1}') == '{"a": 1}'


class TestRejectedProposals:
    """Every one of these must fail closed with a stated reason."""

    def test_malformed_json(self, scene):
        result = propose(scene, StubClient("here is your migration!"))

        assert not result.ok
        assert "did not return valid JSON" in result.error

    def test_json_missing_the_can_migrate_field(self, scene):
        result = propose(scene, StubClient('{"functions": []}'))

        assert "no `can_migrate` field" in result.error

    def test_a_model_that_declines_is_reported_not_retried(self, scene):
        result = propose(scene, StubClient(response(can_migrate=False, reasoning="too complex")))

        assert "declined to migrate" in result.error
        assert "too complex" in result.error

    def test_a_file_outside_the_impact_report_is_refused(self, scene):
        stub = StubClient(
            response(
                functions=[{"path": "app/other.py", "function": "sync", "new_source": MIGRATED}]
            )
        )

        result = propose(scene, stub)

        assert "does not implicate" in result.error

    def test_a_function_it_was_not_given_is_refused(self, scene):
        stub = StubClient(
            response(
                functions=[{"path": "app/sync.py", "function": "other", "new_source": MIGRATED}]
            )
        )

        assert "not one of the functions" in propose(scene, stub).error

    def test_a_renamed_function_is_refused(self, scene):
        """The prototype's exact failure mode, now a hard rejection."""
        renamed = MIGRATED.replace("def sync(", "def sync_all_orders(")
        stub = StubClient(
            response(functions=[{"path": "app/sync.py", "function": "sync", "new_source": renamed}])
        )

        result = propose(scene, stub)

        assert "renamed the function" in result.error
        assert not result.ok

    def test_unparseable_python_is_refused(self, scene):
        stub = StubClient(
            response(
                functions=[
                    {"path": "app/sync.py", "function": "sync", "new_source": "def sync(:\n"}
                ]
            )
        )

        assert "not valid Python" in propose(scene, stub).error

    def test_extra_top_level_statements_are_refused(self, scene):
        sneaky = MIGRATED + "\n\n\nimport os\nos.system('echo hi')\n"
        stub = StubClient(
            response(functions=[{"path": "app/sync.py", "function": "sync", "new_source": sneaky}])
        )

        result = propose(scene, stub)

        assert "not exactly one function definition" in result.error

    def test_claiming_success_with_no_changes_is_refused(self, scene):
        assert "proposed no changes" in propose(scene, StubClient(response(functions=[]))).error

    def test_a_rejected_proposal_leaves_the_workspace_clean(self, scene):
        _, _, _, workspace, _ = scene
        propose(scene, StubClient("garbage"))

        assert workspace.changed_files() == []


class TestContractPreservation:
    """Each contract dimension, rejected independently, through the real path.

    The model may rewrite a function's *body*. Everything else in its signature
    is a promise to callers, to type checkers, and to whatever framework
    registered it by decorator. Changing one is a different and much larger
    change than the migration that was requested, so it is rejected outright
    rather than repaired.

    Every case starts from ``RICH_MIGRATED`` -- a correct body-only migration --
    and alters exactly one thing, so a rejection can only be caused by the
    dimension under test.
    """

    def test_async_downgraded_to_sync_is_refused(self, rich_scene):
        mutated = RICH_MIGRATED.replace("async def sync_orders(", "def sync_orders(")

        result = propose_rich(rich_scene, mutated)

        assert not result.ok
        assert "async to sync" in result.error

    def test_sync_upgraded_to_async_is_refused(self, scene):
        """The other direction, on the simple fixture, which is sync."""
        stub = StubClient(
            response(
                functions=[
                    {
                        "path": "app/sync.py",
                        "function": "sync",
                        "new_source": MIGRATED.replace("def sync(", "async def sync("),
                    }
                ]
            )
        )

        result = propose(scene, stub)

        assert not result.ok
        assert "sync to async" in result.error

    def test_dropping_a_decorator_is_refused(self, rich_scene):
        result = propose_rich(rich_scene, RICH_MIGRATED.replace("@functools.cache\n", ""))

        assert not result.ok
        assert "decorators" in result.error

    def test_changing_a_decorator_is_refused(self, rich_scene):
        result = propose_rich(
            rich_scene, RICH_MIGRATED.replace("@functools.cache", "@functools.lru_cache")
        )

        assert not result.ok
        assert "decorators" in result.error

    def test_adding_a_decorator_is_refused(self, rich_scene):
        result = propose_rich(rich_scene, "@retry\n" + RICH_MIGRATED)

        assert not result.ok
        assert "decorators" in result.error

    def test_changing_a_positional_default_is_refused(self, rich_scene):
        result = propose_rich(
            rich_scene, RICH_MIGRATED.replace("limit: int = 10", "limit: int = 50")
        )

        assert not result.ok
        assert "positional defaults" in result.error

    def test_changing_a_keyword_only_default_is_refused(self, rich_scene):
        result = propose_rich(
            rich_scene, RICH_MIGRATED.replace("timeout: float = 1.0", "timeout: float = 30.0")
        )

        assert not result.ok
        assert "keyword-only defaults" in result.error

    def test_adding_a_default_to_a_required_keyword_only_parameter_is_refused(self, rich_scene):
        """`retries` is required. A default changes what callers may omit."""
        result = propose_rich(
            rich_scene, RICH_MIGRATED.replace("retries: int,", "retries: int = 3,")
        )

        assert not result.ok
        assert "keyword-only defaults" in result.error

    def test_changing_a_parameter_annotation_is_refused(self, rich_scene):
        result = propose_rich(
            rich_scene, RICH_MIGRATED.replace("limit: int = 10", "limit: str = 10")
        )

        assert not result.ok
        assert "parameters" in result.error

    def test_removing_a_parameter_annotation_is_refused(self, rich_scene):
        result = propose_rich(
            rich_scene, RICH_MIGRATED.replace("timeout: float = 1.0", "timeout = 1.0")
        )

        assert not result.ok
        assert "parameters" in result.error

    def test_changing_the_return_annotation_is_refused(self, rich_scene):
        result = propose_rich(rich_scene, RICH_MIGRATED.replace("-> list[dict]:", "-> list[str]:"))

        assert not result.ok
        assert "return annotation" in result.error

    def test_removing_the_return_annotation_is_refused(self, rich_scene):
        result = propose_rich(rich_scene, RICH_MIGRATED.replace(") -> list[dict]:", "):"))

        assert not result.ok
        assert "return annotation" in result.error

    def test_positional_only_promoted_to_normal_is_refused(self, rich_scene):
        """Dropping `/` lets callers pass `account=...`, which they could not before."""
        result = propose_rich(
            rich_scene, RICH_MIGRATED.replace("    account,\n    /,\n", "    account,\n")
        )

        assert not result.ok
        assert "parameters" in result.error

    def test_keyword_only_demoted_to_positional_is_refused(self, rich_scene):
        """Moving a parameter in front of `*extra` changes how it may be passed."""
        result = propose_rich(
            rich_scene,
            RICH_MIGRATED.replace(
                "    *extra,\n    timeout: float = 1.0,\n",
                "    timeout: float = 1.0,\n    *extra,\n",
            ),
        )

        assert not result.ok
        assert "parameters" in result.error

    def test_removing_star_args_is_refused(self, rich_scene):
        result = propose_rich(rich_scene, RICH_MIGRATED.replace("    *extra,\n", "    *,\n"))

        assert not result.ok
        assert "parameters" in result.error

    def test_renaming_star_args_is_refused(self, rich_scene):
        result = propose_rich(rich_scene, RICH_MIGRATED.replace("*extra,", "*args,"))

        assert not result.ok
        assert "parameters" in result.error

    def test_removing_star_star_kwargs_is_refused(self, rich_scene):
        result = propose_rich(rich_scene, RICH_MIGRATED.replace("    **options,\n", ""))

        assert not result.ok
        assert "parameters" in result.error

    def test_renaming_star_star_kwargs_is_refused(self, rich_scene):
        result = propose_rich(rich_scene, RICH_MIGRATED.replace("**options,", "**kwargs,"))

        assert not result.ok
        assert "parameters" in result.error

    def test_dropping_a_parameter_is_refused(self, scene):
        stub = StubClient(
            response(
                functions=[
                    {
                        "path": "app/sync.py",
                        "function": "sync",
                        "new_source": MIGRATED.replace("def sync(api, log):", "def sync(api):"),
                    }
                ]
            )
        )

        result = propose(scene, stub)

        assert not result.ok
        assert "parameters" in result.error

    def test_reordering_parameters_is_refused(self, rich_scene):
        result = propose_rich(
            rich_scene,
            RICH_MIGRATED.replace(
                '    limit: int = 10,\n    label: str = "orders",\n',
                '    label: str = "orders",\n    limit: int = 10,\n',
            ),
        )

        assert not result.ok

    def test_renaming_the_function_is_refused(self, rich_scene):
        result = propose_rich(rich_scene, RICH_MIGRATED.replace("sync_orders(", "fetch_orders("))

        assert not result.ok
        assert "renamed the function" in result.error


class TestContractPositiveControls:
    """The check must not reject the change it exists to permit.

    A comparison that rejected legitimate migrations would be worse than no
    check at all: every real migration would fall back to a human while the
    tool still looked strict.
    """

    def test_a_body_only_migration_is_accepted(self, rich_scene):
        result = propose_rich(rich_scene, RICH_MIGRATED)

        assert result.ok, result.error
        assert "cursor = None" in result.diff
        assert result.changed_files == ["app/sync.py"]

    def test_a_body_only_migration_is_accepted_on_the_simple_fixture(self, scene):
        stub = StubClient(
            response(
                functions=[{"path": "app/sync.py", "function": "sync", "new_source": MIGRATED}]
            )
        )

        assert propose(scene, stub).ok

    @pytest.mark.parametrize(
        "reformat",
        [
            lambda s: s.replace("limit: int = 10", "limit: int=10"),
            lambda s: s.replace("list[dict]", "list[ dict ]"),
            lambda s: s.replace('label: str = "orders"', "label: str = 'orders'"),
            lambda s: s.replace("timeout: float = 1.0", "timeout: float =  1.0"),
            lambda s: s.replace("@functools.cache", "@functools.cache  # keep"),
        ],
        ids=[
            "spacing-around-equals",
            "spacing-inside-subscript",
            "quote-style",
            "extra-whitespace",
            "decorator-trailing-comment",
        ],
    )
    def test_formatting_only_differences_do_not_cause_rejection(self, rich_scene, reformat):
        """Annotations and defaults are compared as normalized AST, not as text.

        `int = 10` and `int=10` are the same contract. Rejecting the second
        would make the check depend on the model's formatting habits rather than
        on meaning, so comparison goes through `ast.unparse`, which normalizes
        whitespace, quote style and comments away.
        """
        result = propose_rich(rich_scene, reformat(RICH_MIGRATED))

        assert result.ok, f"wrongly rejected: {result.error}"

    def test_a_semantically_different_default_is_still_refused(self, rich_scene):
        """Normalization must not go so far as to equate different values.

        `10` and `10.0` look similar and are not the same default.
        """
        result = propose_rich(
            rich_scene, RICH_MIGRATED.replace("limit: int = 10", "limit: int = 10.0")
        )

        assert not result.ok
        assert "positional defaults" in result.error


class TestErrorPropagation:
    def test_an_api_error_is_reported_not_swallowed(self, scene):
        """The prototype turned every failure into a silent `None`."""
        stub = StubClient(error=LLMError("the Anthropic API rejected the credentials"))

        result = propose(scene, stub)

        assert not result.ok
        assert "LLM request failed" in result.error
        assert "rejected the credentials" in result.error


class TestAvailability:
    def test_reports_why_the_llm_path_is_unavailable(self, monkeypatch):
        from patchahead.llm import client as client_module

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        ok, reason = client_module.available()

        assert ok is False
        assert reason
