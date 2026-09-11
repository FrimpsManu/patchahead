"""Migration handlers: impact detection, planning, and refusal.

These are the tests that hold the line on the prototype's two worst defects:
regex false positives (``docs/assessment.md`` §2.2) and the pagination transform
that pasted a hardcoded function over user code (§2.1).
"""

from __future__ import annotations

from patchahead import handlers
from patchahead.analysis import apply_edits
from patchahead.config import Config
from patchahead.domain.change import (
    BreakingChange,
    ChangeKind,
    Confidence,
    PaginationContract,
    SymbolTarget,
)
from tests.conftest import dedent


def change(kind: ChangeKind, symbol="", replacement="", owner="", **kwargs) -> BreakingChange:
    return BreakingChange(
        title=f"test {kind.value}",
        kind=kind,
        target=SymbolTarget(symbol=symbol, replacement=replacement, owner=owner),
        **kwargs,
    )


def run(breaking_change, index, config=None):
    """analyze + plan, the way the engine does it."""
    config = config or Config()
    handler = handlers.find_handler(breaking_change)
    assert handler is not None, f"no handler for {breaking_change.kind}"
    report = handler.analyze(breaking_change, index, config)
    plan = handler.plan(breaking_change, report, index, config)
    return report, plan


def patched(source: str, plan, path: str) -> str:
    return apply_edits(dedent(source), plan.edits_for(path))


class TestRegistry:
    def test_registry_invariants_hold(self):
        assert handlers.selftest_registry() == []

    def test_every_actionable_kind_has_a_handler(self):
        for kind in ChangeKind:
            if kind.is_actionable:
                assert kind in handlers.supported_kinds(), f"{kind.value} has no handler"

    def test_unsupported_kinds_have_no_handler(self):
        assert handlers.find_handler(change(ChangeKind.UNSUPPORTED)) is None
        assert handlers.find_handler(change(ChangeKind.UNKNOWN)) is None

    def test_a_rename_without_both_symbols_is_not_supported(self):
        assert handlers.find_handler(change(ChangeKind.FIELD_RENAME, "total")) is None

    def test_every_handler_documents_its_limitations(self):
        for handler in handlers.registered():
            assert handler.limitations, f"{handler.name} documents no limitations"


class TestFieldRename:
    SOURCE = """
        import logging

        LOG_LABEL = "total"

        def report(orders, df):
            subtotal = df.total
            message = "total"
            return sum(o["total"] for o in orders) + subtotal
    """

    def test_only_real_field_accesses_are_found(self, make_index):
        index = make_index({"a.py": self.SOURCE})

        report, _ = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        found = {(f.reference.line, f.access.value) for f in report.findings}
        assert found == {(6, "attribute"), (8, "subscript")}, (
            "string constants on lines 3 and 7 are not field accesses and must never be reported"
        )

    def test_attribute_on_an_unrelated_receiver_is_reported_but_not_patched(self, make_index):
        index = make_index({"a.py": self.SOURCE})

        report, plan = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        attribute = next(f for f in report.findings if f.access.value == "attribute")
        assert attribute.confidence is Confidence.LOW
        assert attribute.patchable is False
        assert len(plan.transformations) == 1
        assert any("df.total" in s for s in plan.skipped)

    def test_the_patch_touches_only_the_field_access(self, make_index):
        index = make_index({"a.py": self.SOURCE})
        _, plan = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        result = patched(self.SOURCE, plan, "a.py")

        assert 'LOG_LABEL = "total"' in result, "unrelated constant must survive"
        assert 'message = "total"' in result, "unrelated string must survive"
        assert "df.total" in result, "unrelated attribute must survive"
        assert 'o["amount"]' in result, "the real access must be renamed"

    def test_matching_receiver_raises_confidence_to_high(self, make_index):
        index = make_index({"a.py": 'def f(order):\n    return order["total"]\n'})

        report, _ = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        assert report.findings[0].confidence is Confidence.HIGH

    def test_quote_style_is_preserved(self, make_index):
        index = make_index({"a.py": "def f(order):\n    return order['total']\n"})
        _, plan = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        result = patched("def f(order):\n    return order['total']\n", plan, "a.py")

        assert "order['amount']" in result

    def test_dict_get_is_renamed(self, make_index):
        source = 'def f(order):\n    return order.get("total", 0)\n'
        index = make_index({"a.py": source})
        _, plan = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        assert 'order.get("amount", 0)' in patched(source, plan, "a.py")

    def test_test_files_are_not_analyzed(self, make_index):
        index = make_index({"tests/test_a.py": 'def test_x(order):\n    assert order["total"]\n'})

        report, _ = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        assert report.findings == []

    def test_no_impact_produces_a_blocked_plan_with_a_reason(self, make_index):
        index = make_index({"a.py": "x = 1\n"})

        report, plan = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        assert report.findings == []
        assert "no field accesses" in plan.blocked_reason

    def test_min_confidence_high_skips_medium_findings(self, make_index):
        index = make_index({"a.py": 'def f(o):\n    return o["total"]\n'})

        report, plan = run(
            change(ChangeKind.FIELD_RENAME, "total", "amount", "order"),
            index,
            Config(min_confidence=Confidence.HIGH),
        )

        assert report.findings[0].confidence is Confidence.MEDIUM
        assert plan.transformations == []
        assert "below the `high` threshold" in plan.skipped[0]


class TestMethodRename:
    def test_renames_only_the_callee_token(self, make_index):
        source = "def f(client):\n    return client.fetch_orders(limit=10, timeout=5)\n"
        index = make_index({"a.py": source})
        _, plan = run(
            change(ChangeKind.METHOD_RENAME, "fetch_orders", "list_orders", "client"), index
        )

        assert patched(source, plan, "a.py") == (
            "def f(client):\n    return client.list_orders(limit=10, timeout=5)\n"
        )

    def test_a_bare_reference_is_reported_but_not_rewritten(self, make_index):
        index = make_index({"a.py": "def f(client):\n    return client.fetch_orders\n"})

        report, plan = run(
            change(ChangeKind.METHOD_RENAME, "fetch_orders", "list_orders", "client"), index
        )

        assert report.findings[0].patchable is False
        assert plan.transformations == []
        assert "bare method reference" in plan.skipped[0]

    def test_a_locally_defined_function_is_not_renamed(self, make_index):
        """Renaming calls to the repository's own function would break it."""
        index = make_index(
            {"a.py": "def fetch_orders():\n    return []\n\ndef g():\n    return fetch_orders()\n"}
        )

        report, plan = run(change(ChangeKind.METHOD_RENAME, "fetch_orders", "list_orders"), index)

        assert all(not f.patchable for f in report.findings)
        assert plan.transformations == []


class TestKwargRename:
    def test_renames_only_the_argument_name(self, make_index):
        source = "def f(c):\n    return c.fetch_orders(limit=1, timeout_seconds=5.0)\n"
        index = make_index({"a.py": source})
        _, plan = run(
            change(ChangeKind.KWARG_RENAME, "timeout_seconds", "timeout", "fetch_orders"), index
        )

        assert patched(source, plan, "a.py") == (
            "def f(c):\n    return c.fetch_orders(limit=1, timeout=5.0)\n"
        )

    def test_the_same_keyword_on_another_function_is_left_alone(self, make_index):
        source = dedent(
            """
            def f(c, s):
                a = c.fetch_orders(timeout_seconds=5)
                b = s.connect(timeout_seconds=9)
                return a, b
            """
        )
        index = make_index({"a.py": source})

        report, plan = run(
            change(ChangeKind.KWARG_RENAME, "timeout_seconds", "timeout", "fetch_orders"), index
        )

        assert len(report.findings) == 2
        assert len(plan.transformations) == 1
        result = patched(source, plan, "a.py")
        assert "c.fetch_orders(timeout=5)" in result
        assert "s.connect(timeout_seconds=9)" in result

    def test_with_no_named_function_every_call_is_a_medium_candidate(self, make_index):
        index = make_index({"a.py": "def f(s):\n    return s.connect(timeout_seconds=9)\n"})

        report, plan = run(change(ChangeKind.KWARG_RENAME, "timeout_seconds", "timeout"), index)

        assert report.findings[0].confidence is Confidence.MEDIUM
        assert len(plan.transformations) == 1


class TestPagination:
    LOOP = '''
        """Invoice sync."""
        import logging

        log = logging.getLogger(__name__)


        def fetch_invoices(api, account_id):
            """Fetch every invoice."""
            page = 1
            out = []

            while True:
                # one request per page
                r = api.get_orders(account_id, page=page)
                out.extend(r["items"])
                log.info("fetched %d", len(r["items"]))

                if page >= r["total_pages"]:
                    break

                page += 1

            return out
    '''

    def pagination_change(self, **contract):
        return change(
            ChangeKind.PAGINATION_PAGE_TO_CURSOR,
            pagination=PaginationContract(**contract),
        )

    def test_migrates_a_loop_in_an_arbitrary_repository(self, make_index):
        """The prototype's headline failure: it pasted its own demo function here."""
        index = make_index({"client.py": self.LOOP})
        _, plan = run(self.pagination_change(), index)

        result = patched(self.LOOP, plan, "client.py")

        assert "def fetch_invoices(api, account_id):" in result, "signature preserved"
        assert '"""Fetch every invoice."""' in result, "docstring preserved"
        assert "# one request per page" in result, "comment preserved"
        assert 'r["items"]' in result, "the response key is the caller's, not ours"
        assert "log.info" in result, "unrelated statements preserved"
        assert "cursor = None" in result
        assert "api.get_orders(account_id, cursor=cursor)" in result
        assert 'if not r.get("has_more"):' in result
        assert 'cursor = r.get("next_cursor")' in result
        assert "page" not in result.replace("# one request per page", "")

    def test_the_diff_is_exactly_four_lines(self, make_index):
        from patchahead.analysis import unified_diff

        index = make_index({"client.py": self.LOOP})
        _, plan = run(self.pagination_change(), index)
        source = dedent(self.LOOP)
        diff = unified_diff(source, apply_edits(source, plan.edits_for("client.py")), "client.py")

        added = [line for line in diff.splitlines() if line.startswith("+") and line[1:2] != "+"]
        removed = [line for line in diff.splitlines() if line.startswith("-") and line[1:2] != "-"]
        assert len(added) == 4 and len(removed) == 4

    def test_a_while_condition_loop_is_refused(self, make_index):
        index = make_index(
            {
                "a.py": """
                def sync(api):
                    page = 1
                    out = []
                    while page <= api.get_orders(page=page)["total_pages"]:
                        page += 1
                    return out
                """
            }
        )

        _, plan = run(self.pagination_change(), index)

        assert plan.transformations == []
        assert plan.blocked_reason

    def test_a_page_variable_used_elsewhere_blocks_the_migration(self, make_index):
        index = make_index(
            {
                "a.py": """
                def sync(api, log):
                    page = 1
                    out = []
                    while True:
                        r = api.get_orders(page=page)
                        out.extend(r["orders"])
                        log.info("on page %d", page)
                        if page >= r["total_pages"]:
                            break
                        page += 1
                    return out
                """
            }
        )

        report, plan = run(self.pagination_change(), index)

        assert plan.transformations == []
        assert "used outside the pagination loop" in plan.blocked_reason + " ".join(plan.skipped)
        assert any(not f.patchable for f in report.findings)

    def test_a_total_pages_read_outside_a_loop_is_reported_not_patched(self, make_index):
        index = make_index({"a.py": 'def f(r):\n    return r["total_pages"]\n'})

        report, plan = run(self.pagination_change(), index)

        assert len(report.findings) == 1
        assert report.findings[0].patchable is False
        assert plan.transformations == []

    def test_a_missing_advance_is_refused_with_a_specific_reason(self, make_index):
        index = make_index(
            {
                "a.py": """
                def sync(api):
                    page = 1
                    out = []
                    while True:
                        r = api.get_orders(page=page)
                        out.extend(r["orders"])
                        if page >= r["total_pages"]:
                            break
                    return out
                """
            }
        )

        report, _ = run(self.pagination_change(), index)

        assert "no `page += 1` advance" in report.findings[0].reason

    def test_an_existing_cursor_variable_does_not_get_clobbered(self, make_index):
        source = """
            def sync(api, cursor):
                page = 1
                out = []
                while True:
                    r = api.get_orders(page=page, token=cursor)
                    out.extend(r["orders"])
                    if page >= r["total_pages"]:
                        break
                    page += 1
                return out
        """
        index = make_index({"a.py": source})
        _, plan = run(self.pagination_change(), index)

        result = patched(source, plan, "a.py")

        assert "cursor_token = None" in result
        assert "token=cursor)" in result, "the caller's own `cursor` argument is untouched"

    def test_alternative_field_names_are_honoured(self, make_index):
        source = """
            def sync(api):
                p = 1
                out = []
                while True:
                    r = api.list(p=p)
                    out.extend(r["data"])
                    if p >= r["pageCount"]:
                        break
                    p += 1
                return out
        """
        index = make_index({"a.py": source})
        _, plan = run(
            self.pagination_change(
                page_param="p",
                total_pages_key="pageCount",
                cursor_param="after",
                next_cursor_key="nextAfter",
                has_more_key="hasMore",
            ),
            index,
        )

        result = patched(source, plan, "a.py")

        assert "after = None" in result
        assert "api.list(after=after)" in result
        assert 'if not r.get("hasMore"):' in result
        assert 'after = r.get("nextAfter")' in result

    def test_an_assignment_style_advance_is_recognized(self, make_index):
        source = """
            def sync(api):
                page = 1
                out = []
                while True:
                    r = api.get(page=page)
                    out.extend(r["orders"])
                    if page >= r["total_pages"]:
                        break
                    page = page + 1
                return out
        """
        index = make_index({"a.py": source})
        _, plan = run(self.pagination_change(), index)

        assert 'cursor = r.get("next_cursor")' in patched(source, plan, "a.py")


class TestMultiFileRepositories:
    def test_findings_span_several_files_and_are_ordered(self, make_index):
        index = make_index(
            {
                "app/b.py": 'def g(order):\n    return order["total"]\n',
                "app/a.py": 'def f(order):\n    return order["total"]\n',
            }
        )

        report, plan = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        assert [f.path for f in report.findings] == ["app/a.py", "app/b.py"]
        assert plan.target_files == ["app/a.py", "app/b.py"]
        assert len(plan.transformations) == 2
