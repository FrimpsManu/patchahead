"""AST analysis: what it records, and -- more importantly -- what it does not."""

from __future__ import annotations

import pytest

from patchahead.analysis import analyze_source, base_name, receiver_name
from patchahead.analysis.edits import EditError, apply_edits, is_parseable, unified_diff
from patchahead.analysis.index import is_excluded, is_test_path
from patchahead.analysis.python_ast import MODULE_SCOPE, ParseError
from patchahead.domain.plan import TextEdit
from tests.conftest import dedent


class TestConstructRecognition:
    def test_records_subscripts_with_constant_string_keys(self):
        module = analyze_source('x = order["total"]\n', "a.py")

        assert [(s.key, s.receiver) for s in module.subscripts] == [("total", "order")]

    def test_ignores_subscripts_with_a_variable_key(self):
        # We cannot know what `key` holds, so this is not a rename candidate.
        module = analyze_source("x = order[key]\n", "a.py")

        assert module.subscripts == []

    def test_a_bare_string_literal_is_not_an_access(self):
        """The single most important negative case.

        The prototype matched `"total"` as text and rewrote it, corrupting
        unrelated constants. A string that is not in a subscript or `.get()`
        position is never a candidate here -- not filtered later, never seen.
        """
        module = analyze_source('LABEL = "total"\nmsg = f"{LABEL}"\n', "a.py")

        assert module.subscripts == []
        assert module.get_calls == []

    def test_records_dict_get_separately_from_generic_calls(self):
        module = analyze_source('x = order.get("total", 0)\n', "a.py")

        assert [(g.key, g.receiver) for g in module.get_calls] == [("total", "order")]

    def test_records_attribute_access(self):
        module = analyze_source("x = order.total\n", "a.py")

        assert [(a.attr, a.receiver) for a in module.attributes] == [("total", "order")]

    def test_a_callee_is_a_call_not_an_attribute_read(self):
        module = analyze_source("client.fetch_orders()\n", "a.py")

        assert [c.name for c in module.calls] == ["fetch_orders"]
        assert module.attributes == [], "the callee must not be double-counted"

    def test_records_keyword_arguments_with_exact_ranges(self):
        module = analyze_source("f(a, timeout_seconds=5, b=2)\n", "a.py")
        call = module.calls[0]

        assert set(call.keywords) == {"timeout_seconds", "b"}
        keyword_range = call.keywords["timeout_seconds"]
        assert keyword_range.col == 5
        assert keyword_range.end_col == 5 + len("timeout_seconds")

    def test_star_kwargs_is_not_a_named_keyword(self):
        module = analyze_source("f(**options)\n", "a.py")

        assert module.calls[0].keywords == {}


class TestScopeTracking:
    def test_module_level_code_reports_module_scope(self):
        module = analyze_source('x = order["total"]\n', "a.py")

        assert module.subscripts[0].symbol == MODULE_SCOPE

    def test_methods_get_a_dotted_symbol(self):
        module = analyze_source(
            dedent(
                """
                class Reporter:
                    def total(self, order):
                        return order["total"]
                """
            ),
            "a.py",
        )

        assert module.subscripts[0].symbol == "Reporter.total"

    def test_nested_functions_nest_the_symbol(self):
        module = analyze_source(
            dedent(
                """
                def outer():
                    def inner(order):
                        return order["total"]
                    return inner
                """
            ),
            "a.py",
        )

        assert module.subscripts[0].symbol == "outer.inner"


class TestReceiverNames:
    @pytest.mark.parametrize(
        "source, expected",
        [
            ('order["k"]', "order"),
            ('self.cache["k"]', "self.cache"),
            ('get_orders()["k"]', "get_orders()"),
            ('{"a": 1}["k"]', ""),
            ('items[0]["k"]', ""),
        ],
    )
    def test_receiver_name(self, source, expected):
        module = analyze_source(f"x = {source}\n", "a.py")

        assert receiver_name(module.tree.body[0].value.value) == expected

    @pytest.mark.parametrize(
        "dotted, expected", [("order", "order"), ("self.cache", "self"), ("f()", "f"), ("", "")]
    )
    def test_base_name(self, dotted, expected):
        assert base_name(dotted) == expected


class TestParseFailures:
    def test_syntax_error_names_the_line(self):
        with pytest.raises(ParseError, match="a.py:1"):
            analyze_source("def broken(\n", "a.py")

    def test_null_bytes_are_a_parse_error_not_a_crash(self):
        with pytest.raises(ParseError):
            analyze_source("x = 1\x00\n", "a.py")


class TestEdits:
    def test_applies_a_single_range_edit(self):
        source = "f(timeout_seconds=5)\n"

        assert apply_edits(source, [TextEdit(1, 2, 1, 17, "timeout")]) == "f(timeout=5)\n"

    def test_applies_several_edits_right_to_left(self):
        source = "a = 1\nb = 2\nc = 3\n"
        edits = [TextEdit(1, 0, 1, 1, "x"), TextEdit(3, 0, 3, 1, "z")]

        assert apply_edits(source, edits) == "x = 1\nb = 2\nz = 3\n"

    def test_preserves_everything_outside_the_edited_range(self):
        source = dedent(
            """
            # a comment

            def f(order):   # trailing comment
                return order["total"]
            """
        )
        patched = apply_edits(source, [TextEdit(4, 17, 4, 24, '"amount"')])

        assert "# a comment" in patched
        assert "# trailing comment" in patched
        assert patched.count("\n\n") == source.count("\n\n")

    def test_overlapping_edits_are_rejected_rather_than_merged(self):
        edits = [TextEdit(1, 0, 1, 5, "x"), TextEdit(1, 3, 1, 8, "y")]

        with pytest.raises(EditError, match="overlapping"):
            apply_edits("abcdefghij\n", edits)

    def test_an_out_of_range_position_is_an_error(self):
        with pytest.raises(EditError, match="out of range"):
            apply_edits("a\n", [TextEdit(9, 0, 9, 1, "x")])

    def test_no_edits_returns_the_source_unchanged(self):
        assert apply_edits("a = 1\n", []) == "a = 1\n"

    def test_is_parseable_reports_the_failure_location(self):
        ok, error = is_parseable("def f(\n", "a.py")

        assert ok is False
        assert "a.py:" in error

    def test_unified_diff_is_empty_for_identical_sources(self):
        assert unified_diff("a\n", "a\n", "x.py") == ""

    def test_unified_diff_uses_git_style_prefixes(self):
        diff = unified_diff("a\n", "b\n", "x.py")

        assert "--- a/x.py" in diff
        assert "+++ b/x.py" in diff


class TestPathClassification:
    @pytest.mark.parametrize(
        "path, expected",
        [
            ("tests/test_a.py", True),
            ("test_a.py", True),
            ("a_test.py", True),
            ("tests/helpers.py", True),
            ("app/client.py", False),
            ("app/latest.py", False),
        ],
    )
    def test_is_test_path(self, path, expected):
        assert is_test_path(path) is expected

    @pytest.mark.parametrize(
        "path, patterns, expected",
        [
            ("a/node_modules/x.py", ["node_modules"], True),
            ("node_modules/x.py", ["node_modules"], True),
            ("app/generated/x.py", ["**/generated/**"], True),
            ("app/client.py", ["node_modules"], False),
            ("vendor/x.py", ["vendor"], True),
        ],
    )
    def test_is_excluded(self, path, patterns, expected):
        assert is_excluded(path, patterns) is expected
