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


def change(
    kind: ChangeKind,
    symbol="",
    replacement="",
    owner="",
    owner_is_explicit=True,
    **kwargs,
) -> BreakingChange:
    """Build a change for a handler test.

    ``owner_is_explicit`` defaults to True because that is what a real change
    document produces whenever it states ownership ("the field on each `order`
    object"), and it is the mode that decides the safety property: a receiver
    that is not the asserted owner is never patched. Tests that want the looser
    inferred-owner behavior pass ``owner_is_explicit=False`` and say so.
    """
    return BreakingChange(
        title=f"test {kind.value}",
        kind=kind,
        target=SymbolTarget(
            symbol=symbol,
            replacement=replacement,
            owner=owner,
            owner_is_explicit=owner_is_explicit,
        ),
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
        assert any("df.total" in s for s in plan.skipped)

    def test_a_subscript_on_a_different_object_is_never_patched(self, make_index):
        """`order["total"]` and `customer["total"]` are different fields.

        The prototype rewrote both. So did this handler before the receiver
        check, because a matching constant key looked like enough evidence on
        its own. When the document asserts an owner, it is not.
        """
        source = 'def f(order, customer):\n    return order["total"] + customer["total"]\n'
        index = make_index({"a.py": source})

        report, plan = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        graded = {f.matched_contract: (f.confidence, f.patchable) for f in report.findings}
        assert graded['order["total"]'] == (Confidence.HIGH, True)
        assert graded['customer["total"]'] == (Confidence.LOW, False)

        result = patched(source, plan, "a.py")
        assert 'order["amount"]' in result
        assert 'customer["total"]' in result, "an unrelated object must be untouched"

    def test_a_receiver_that_cannot_be_shown_to_be_the_owner_fails_closed(self, make_index):
        """`for o in orders` is a real cost of this rule, and it is accepted.

        Proving `o` is an `order` needs type inference PatchAhead does not do.
        A false negative is recoverable by hand; a wrong edit is not.
        """
        source = 'def f(orders):\n    return [o["total"] for o in orders]\n'
        index = make_index({"a.py": source})

        report, plan = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        assert report.findings[0].patchable is False
        assert plan.transformations == []
        assert "not the declared owner" in report.findings[0].unpatchable_reason

    def test_a_dotted_receiver_matches_its_last_segment(self, make_index):
        source = 'def f(self):\n    return self.order["total"]\n'
        index = make_index({"a.py": source})

        report, _ = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        assert report.findings[0].confidence is Confidence.HIGH
        assert report.findings[0].patchable is True

    def test_an_inferred_owner_lowers_confidence_instead_of_refusing(self, make_index):
        """A receiver scraped from a vendor's example is a hint, not a constraint.

        `**Before:** `client.fetch_orders(...)`` names the vendor's variable, not
        the reader's, so a mismatch must not veto the migration.
        """
        source = 'def f(o):\n    return o["total"]\n'
        index = make_index({"a.py": source})

        report, plan = run(
            change(ChangeKind.FIELD_RENAME, "total", "amount", "order", owner_is_explicit=False),
            index,
        )

        assert report.findings[0].confidence is Confidence.MEDIUM
        assert len(plan.transformations) == 1

    def test_the_patch_touches_only_the_field_access(self, make_index):
        index = make_index({"a.py": self.SOURCE})
        _, plan = run(change(ChangeKind.FIELD_RENAME, "total", "amount", "order"), index)

        result = patched(self.SOURCE, plan, "a.py")

        assert 'LOG_LABEL = "total"' in result, "unrelated constant must survive"
        assert 'message = "total"' in result, "unrelated string must survive"
        assert "df.total" in result, "unrelated attribute must survive"
        assert 'o["total"]' in result, (
            "`o` cannot be shown to be the declared owner `order`, so it is "
            "reported rather than rewritten"
        )

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
            change(ChangeKind.FIELD_RENAME, "total", "amount", "order", owner_is_explicit=False),
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

    def test_a_call_on_a_different_receiver_is_never_patched(self, make_index):
        """`client.fetch_orders()` and `analytics.fetch_orders()` are different.

        Only the receiver the change document asserts may be migrated.
        """
        source = (
            "def f(client, analytics):\n"
            "    return client.fetch_orders(), analytics.fetch_orders()\n"
        )
        index = make_index({"a.py": source})

        report, plan = run(
            change(ChangeKind.METHOD_RENAME, "fetch_orders", "list_orders", "client"), index
        )

        graded = {f.matched_contract: (f.confidence, f.patchable) for f in report.findings}
        assert graded["client.fetch_orders()"] == (Confidence.HIGH, True)
        assert graded["analytics.fetch_orders()"] == (Confidence.LOW, False)

        result = patched(source, plan, "a.py")
        assert "client.list_orders()" in result
        assert "analytics.fetch_orders()" in result, "an unrelated receiver must survive"

    def test_an_inferred_receiver_still_migrates_a_differently_named_variable(self, make_index):
        """`api_client.fetch_orders()` is the same SDK call as `client.fetch_orders()`.

        When the receiver came from a Before/After example rather than an
        assertion, a different local name must not veto the migration.
        """
        source = "def f(api_client):\n    return api_client.fetch_orders()\n"
        index = make_index({"a.py": source})

        report, plan = run(
            change(
                ChangeKind.METHOD_RENAME,
                "fetch_orders",
                "list_orders",
                "client",
                owner_is_explicit=False,
            ),
            index,
        )

        assert report.findings[0].confidence is Confidence.MEDIUM
        assert "api_client.list_orders()" in patched(source, plan, "a.py")

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

    def test_with_no_named_function_nothing_is_rewritten(self, make_index):
        """A keyword name on its own does not identify a library.

        `timeout_seconds=` here belongs to a socket, and the change document --
        which names no function -- gives nothing to say otherwise. Rewriting it
        would break a working call on the strength of a shared English word, so
        the site is reported and left alone.
        """
        index = make_index({"a.py": "def f(s):\n    return s.connect(timeout_seconds=9)\n"})

        report, plan = run(change(ChangeKind.KWARG_RENAME, "timeout_seconds", "timeout"), index)

        assert len(report.findings) == 1, "the site is still reported"
        assert report.findings[0].patchable is False
        assert report.findings[0].confidence is Confidence.LOW
        assert "names no function" in report.findings[0].unpatchable_reason
        assert plan.transformations == []
        assert plan.skipped, "and the plan says why it declined"

    def test_with_no_named_function_an_unrelated_library_is_not_collateral(self, make_index):
        """The shape that made this worth changing: two libraries, one keyword."""
        index = make_index(
            {
                "a.py": (
                    "from sdk import fetch_orders\n"
                    "from mailer import send_email\n\n\n"
                    "def go():\n"
                    "    fetch_orders(retries=3)\n"
                    '    send_email(to="x", retries=5)\n'
                )
            }
        )

        report, plan = run(change(ChangeKind.KWARG_RENAME, "retries", "max_retries"), index)

        assert len(report.findings) == 2
        assert not any(finding.patchable for finding in report.findings)
        assert plan.transformations == []

    def test_lowering_the_confidence_threshold_does_not_unlock_the_refusal(self, make_index):
        """`--min-confidence low` opts into weaker evidence, not into no evidence.

        The LOW grading alone would block this at the default threshold, which
        makes the refusal look safe while resting on a setting the user can
        change. `patchable=False` is what actually holds it, and this is the test
        that tells the two apart.
        """
        index = make_index({"a.py": "def f(s):\n    return s.connect(timeout_seconds=9)\n"})

        _, plan = run(
            change(ChangeKind.KWARG_RENAME, "timeout_seconds", "timeout"),
            index,
            config=Config(min_confidence=Confidence.LOW),
        )

        assert plan.transformations == []


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

    def test_a_loop_in_a_nested_function_is_matched_once(self, make_index):
        """Walking the outer function used to find the inner loop too.

        That matched one loop twice -- as `outer` and as `outer.inner` -- and
        emitted two overlapping sets of edits for it.
        """
        source = """
            def outer(api, log):
                size = 50

                def inner():
                    page = 1
                    out = []
                    while True:
                        r = api.get(page=page)
                        out.extend(r["items"])
                        if page >= r["total_pages"]:
                            break
                        page += 1
                    return out

                log.info("size %d", size)
                return inner()
        """
        index = make_index({"a.py": source})

        report, plan = run(self.pagination_change(), index)

        assert len(report.findings) == 1
        assert report.findings[0].symbol == "outer.inner"
        assert len(plan.transformations) == 4, "four spans, not eight"
        assert {t.symbol for t in plan.transformations} == {"outer.inner"}

        result = patched(source, plan, "a.py")
        assert "cursor = None" in result
        assert 'log.info("size %d", size)' in result

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
