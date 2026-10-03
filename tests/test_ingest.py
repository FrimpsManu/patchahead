"""Change ingestion: classification, extraction, and honest uncertainty."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from patchahead.domain.change import ChangeKind, Confidence, Severity
from patchahead.ingest import IngestError, parse_file
from patchahead.ingest.base import ChangeDocument, parse_document
from patchahead.ingest.markdown import _pagination_contract, classify
from tests.conftest import EXAMPLE_CHANGES, dedent


def parse_text(text: str, suffix: str = ".md"):
    return parse_document(ChangeDocument(text=dedent(text), path="<test>", suffix=suffix))


class TestClassification:
    @pytest.mark.parametrize(
        "text, expected",
        [
            (
                "### Pagination is now cursor-based\nUse `next_cursor` and `has_more`.",
                ChangeKind.PAGINATION_PAGE_TO_CURSOR,
            ),
            ("### Field renamed\nThe field renamed `total` -> `amount`.", ChangeKind.FIELD_RENAME),
            ("### Method renamed\nThe method renamed `a` -> `b`.", ChangeKind.METHOD_RENAME),
            ("### Arg\nThe keyword argument `x` was renamed to `y`.", ChangeKind.KWARG_RENAME),
        ],
    )
    def test_recognizes_supported_families(self, text, expected):
        assert classify(dedent(text))[0] is expected

    @pytest.mark.parametrize(
        "text",
        [
            "### Moved\nThe endpoint moved to /v2/orders.",
            "### Shape\nThe response shape changed; orders is now nested.",
            "### Auth\nAuthentication has changed to OAuth.",
        ],
    )
    def test_recognized_but_unsupported_changes_say_so(self, text):
        kind, _, reason = classify(dedent(text))

        assert kind is ChangeKind.UNSUPPORTED
        assert "cannot migrate" in reason

    def test_unclassifiable_text_is_unknown_not_a_guess(self):
        """The prototype defaulted to the demo's pagination change here."""
        kind, _, reason = classify("We improved performance and fixed some typos.")

        assert kind is ChangeKind.UNKNOWN
        assert reason

    def test_keyword_argument_beats_a_bare_rename_signal(self):
        kind, _, _ = classify("The keyword argument `timeout_seconds` was renamed to `timeout`.")

        assert kind is ChangeKind.KWARG_RENAME


class TestSymbolExtraction:
    def test_extracts_both_symbols_and_the_owner(self):
        change = parse_text(
            """
            ### Order field renamed: `total` -> `amount`
            The monetary field on each `order` object was renamed.
            """
        )[0]

        assert change.target.symbol == "total"
        assert change.target.replacement == "amount"
        assert change.target.owner == "order"

    def test_an_arrow_in_the_heading_beats_a_looser_sentence_match(self):
        """Regression: this sentence shape used to yield `fetch_orders` -> `timeout`."""
        change = parse_text(
            """
            ### Keyword argument renamed: `timeout_seconds` -> `timeout`
            The `timeout_seconds` keyword argument on `fetch_orders` was renamed to `timeout`.
            """
        )[0]

        assert (change.target.symbol, change.target.replacement) == (
            "timeout_seconds",
            "timeout",
        )

    def test_a_rename_with_no_extractable_symbols_stays_low_confidence(self):
        change = parse_text("### Something renamed\nA field was renamed. Update your code.")[0]

        assert change.confidence is Confidence.LOW
        assert not change.target.is_rename
        assert "could not extract" in change.classification_reason

    def test_strips_a_receiver_prefix_from_the_symbol(self):
        change = parse_text("### Field renamed\nThe field renamed `customer.name` -> `full_name`.")[
            0
        ]

        assert change.target.symbol == "name"


class TestStatementsWithinASection:
    """Vendors list several changes under one heading; each is read on its own."""

    def readings(self, text: str, suffix: str = ".md"):
        return [
            (c.kind.value, c.target.symbol, c.target.replacement, c.target.owner)
            for c in parse_text(text, suffix)
        ]

    def test_each_table_row_is_a_change(self):
        assert self.readings(
            """
            ## Migrating to v2

            | v1 | v2 |
            |----|----|
            | `.dict()` | `.model_dump()` |
            | `.parse_obj()` | `.model_validate()` |
            """
        ) == [
            ("method_rename", "dict", "model_dump", ""),
            ("method_rename", "parse_obj", "model_validate", ""),
        ]

    def test_table_columns_are_chosen_by_their_headers(self):
        """ "Notes | Before | After" -- the old name is not in the first column."""
        assert self.readings(
            """
            ### Renamed Charge properties

            | Notes | Before | After |
            | --- | --- | --- |
            | same type | `amount_cents` | `amount` |
            """
        ) == [("field_rename", "amount_cents", "amount", "")]

    def test_a_bullet_that_cannot_be_migrated_is_reported_not_dropped(self):
        changes = parse_text(
            """
            ## Breaking changes

            - `fetch_all()` has been renamed to `list_all()`.
            - The `/v1/orders` endpoint was moved to `/v2/orders`.
            """
        )

        assert [c.kind for c in changes] == [ChangeKind.METHOD_RENAME, ChangeKind.UNSUPPORTED]
        assert "endpoint" in changes[1].classification_reason

    def test_two_renames_in_one_sentence(self):
        assert self.readings(
            "### Client\n\nRenamed `fetch_orders()` to `list_orders()` and `fetch_order()` "
            "to `get_order()`.\n"
        ) == [
            ("method_rename", "fetch_orders", "list_orders", ""),
            ("method_rename", "fetch_order", "get_order", ""),
        ]

    def test_a_clause_between_the_name_and_the_verb_does_not_become_the_name(self):
        """ "of `get()` and `post()` has been renamed to `verify`" renames `verify_ssl`."""
        assert self.readings(
            """
            ### Requests

            The `verify_ssl` keyword argument of `get()` and `post()` has been renamed to `verify`.
            """
        ) == [("kwarg_rename", "verify_ssl", "verify", "")]

    def test_a_statement_gets_its_own_line_number(self):
        changes = parse_text(
            """
            ## Breaking changes

            - `fetch_one()` -> `get_one()`
            - `fetch_all()` -> `list()`
            """
        )

        assert [c.evidence[0].line for c in changes] == [3, 4]

    def test_a_single_rename_still_reads_its_before_after_example(self):
        change = parse_text(
            """
            ### Method renamed: `fetch_orders` -> `list_orders`

            - **Before:** `client.fetch_orders(limit=10)`
            """
        )[0]

        assert (change.target.owner, change.target.owner_is_explicit) == ("client", False)
        assert change.old_behavior == "`client.fetch_orders(limit=10)`"


class TestWhatIsNotARename:
    def test_a_namespace_move_is_unsupported(self):
        """`create` -> `create` would be a migration that changes nothing."""
        change = parse_text(
            "### v1\n\n- `openai.Completion.create()` -> `client.completions.create()`\n"
        )[0]

        assert change.kind is ChangeKind.UNSUPPORTED
        assert "move" in change.classification_reason

    def test_a_type_change_is_not_a_rename(self):
        changes = parse_text("### Timeouts\n\nThe `timeout` argument is now `float`.\n")

        assert all(not c.target.is_rename for c in changes)

    def test_cursor_pagination_that_never_names_the_cursor_is_unsupported(self):
        """The handler would write `cursor=` and `next_cursor` -- names this vendor never used."""
        change = parse_text(
            """
            ### Pagination is now cursor-based

            Page-based pagination was removed. Pass `starting_after` with the last
            object's ID; `has_more` tells you whether to continue.
            """
        )[0]

        assert change.kind is ChangeKind.UNSUPPORTED
        assert "never names a `cursor`" in change.classification_reason


class TestSphinx:
    """CPython's "What's New" and most Python projects' docs are Sphinx."""

    def test_a_simple_table_of_cross_references(self):
        changes = parse_text(
            """
            * Removed :class:`~unittest.TestCase` aliases:

              ==================== ===================== =============
               Deprecated alias     Method Name           Deprecated in
              ==================== ===================== =============
               ``assertEquals``     :meth:`.assertEqual`  3.2
               ``failIf``           :meth:`.assertFalse`  3.1
              ==================== ===================== =============
            """,
            suffix=".rst",
        )

        assert [(c.kind, c.target.symbol, c.target.replacement) for c in changes] == [
            (ChangeKind.METHOD_RENAME, "assertEquals", "assertEqual"),
            (ChangeKind.METHOD_RENAME, "failIf", "assertFalse"),
        ]

    def test_a_titled_cross_reference_uses_its_target(self):
        changes = parse_text(
            "### Loader\n\n- :meth:`old <pkg.Loader.load_tests>` -> :meth:`pkg.Loader.load`\n"
        )

        target = changes[0].target
        assert (changes[0].kind, target.symbol, target.replacement, target.owner) == (
            ChangeKind.METHOD_RENAME,
            "load_tests",
            "load",
            "pkg.Loader",
        )

    def test_a_new_name_under_another_owner_is_a_move(self):
        """`imp.find_module()` -> `importlib.util.find_spec()` would become `imp.find_spec()`."""
        change = parse_text("### imp\n\n- `imp.find_module()` -> `importlib.util.find_spec()`\n")[0]

        assert change.kind is ChangeKind.UNSUPPORTED
        assert "moves to a different module" in change.classification_reason

    def test_the_same_owner_is_still_a_rename(self):
        change = parse_text("### Client\n\n- `Client.fetch_all()` -> `Client.list_all()`\n")[0]

        assert (change.kind, change.target.replacement) == (ChangeKind.METHOD_RENAME, "list_all")


class TestRestructuredText:
    def test_underlined_headings_and_double_backticks(self):
        changes = parse_text(
            """
            Version 3.0.0
            =============

            Breaking changes
            ----------------

            - ``fetch_orders()`` was renamed to ``list_orders()``.
            - The ``timeout_seconds`` keyword argument was renamed to ``timeout``.
            """,
            suffix=".rst",
        )

        assert [(c.kind, c.target.symbol) for c in changes] == [
            (ChangeKind.METHOD_RENAME, "fetch_orders"),
            (ChangeKind.KWARG_RENAME, "timeout_seconds"),
        ]
        # The underline became a blank line, so line numbers still match the file.
        assert changes[0].evidence[0].line == 7


class TestDocumentStructure:
    def test_a_multi_change_document_yields_several_changes(self):
        changes = parse_text(
            """
            # Release notes

            ## Breaking changes

            ### Field renamed: `total` -> `amount`
            The field renamed on each `order` object.

            ### Method renamed: `fetch` -> `list`
            The method renamed on `client`.
            """
        )

        assert [c.kind for c in changes] == [
            ChangeKind.FIELD_RENAME,
            ChangeKind.METHOD_RENAME,
        ]

    def test_non_breaking_sections_do_not_contribute_signal(self):
        changes = parse_text(
            """
            ### Field renamed: `total` -> `amount`
            The field renamed on each `order` object.

            ## Non-breaking changes

            ### Cursor support added
            You may now pass `cursor` and read `next_cursor` and `has_more`.
            """
        )

        assert [c.kind for c in changes] == [ChangeKind.FIELD_RENAME]

    def test_severity_is_read_from_a_risk_line(self):
        change = parse_text(
            """
            ### Field renamed: `total` -> `amount`
            On each `order` object.

            > Risk: HIGH -- revenue numbers break.
            """
        )[0]

        assert change.severity is Severity.HIGH

    def test_evidence_quotes_the_source_with_line_numbers(self):
        change = parse_text(
            """
            ### Field renamed: `total` -> `amount`
            The `total` field was removed from each `order` object.
            """
        )[0]

        assert change.evidence
        assert all(e.line and e.line >= 1 for e in change.evidence)


class TestStructuredDocuments:
    def test_parses_a_single_json_object(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps(
                {
                    "title": "Renamed",
                    "kind": "field_rename",
                    "target": {"symbol": "total", "replacement": "amount", "owner": "order"},
                }
            )
        )
        change = parse_file(path)[0]

        assert change.kind is ChangeKind.FIELD_RENAME
        assert change.confidence is Confidence.HIGH, "explicit input is not a guess"
        assert change.source == "structured"

    def test_parses_a_changes_list(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps(
                {
                    "changes": [
                        {"title": "A", "kind": "field_rename"},
                        {"title": "B", "kind": "method_rename"},
                    ]
                }
            )
        )

        assert len(parse_file(path)) == 2

    def test_pagination_field_names_are_configurable(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps(
                {
                    "title": "Pages",
                    "kind": "pagination_page_to_cursor",
                    "pagination": {"total_pages_key": "pageCount", "has_more_key": "hasMore"},
                }
            )
        )
        change = parse_file(path)[0]

        assert change.pagination.total_pages_key == "pageCount"
        assert change.pagination.has_more_key == "hasMore"
        assert change.pagination.cursor_param == "cursor", "unset fields keep their defaults"

    @pytest.mark.parametrize(
        "payload, message",
        [
            ('{"kind": "field_rename"}', "title"),
            ('{"title": "A"}', "kind"),
            ('{"title": "A", "kind": "teleport"}', "unknown kind"),
            ('{"title": "A", "kind": "field_rename", "target": 3}', "non-mapping"),
            ("[]", "no changes"),
            ("not json at all", "not valid JSON"),
        ],
    )
    def test_invalid_structured_input_is_rejected_with_a_reason(self, tmp_path, payload, message):
        path = tmp_path / "c.json"
        path.write_text(payload)

        with pytest.raises(IngestError, match=message):
            parse_file(path)


class TestDocumentLoading:
    def test_missing_file(self, tmp_path):
        with pytest.raises(IngestError, match="no such change document"):
            parse_file(tmp_path / "nope.md")

    def test_empty_file(self, tmp_path):
        path = tmp_path / "c.md"
        path.write_text("   \n")

        with pytest.raises(IngestError, match="empty"):
            parse_file(path)

    def test_unsupported_extension(self, tmp_path):
        path = tmp_path / "c.pdf"
        path.write_text("content")

        with pytest.raises(IngestError, match="no parser supports"):
            parse_file(path)


class TestBundledExamples:
    """Every shipped example must parse to what its documentation claims."""

    @pytest.mark.parametrize(
        "name, kind, symbol, replacement",
        [
            ("pagination-cursor.md", ChangeKind.PAGINATION_PAGE_TO_CURSOR, "", ""),
            ("field-rename.md", ChangeKind.FIELD_RENAME, "total", "amount"),
            ("method-rename.md", ChangeKind.METHOD_RENAME, "fetch_orders", "list_orders"),
            ("kwarg-rename.md", ChangeKind.KWARG_RENAME, "timeout_seconds", "timeout"),
        ],
    )
    def test_example_documents_parse_as_documented(self, name, kind, symbol, replacement):
        change = parse_file(EXAMPLE_CHANGES / name)[0]

        assert change.kind is kind
        assert change.target.symbol == symbol
        assert change.target.replacement == replacement

    def test_the_structured_example_matches_its_markdown_twin(self):
        from_json = parse_file(EXAMPLE_CHANGES / "pagination-cursor.json")[0]
        from_markdown = parse_file(EXAMPLE_CHANGES / "pagination-cursor.md")[0]

        assert from_json.kind is from_markdown.kind

    def test_the_combined_sdk_document_yields_both_changes(self):
        changes = parse_file(EXAMPLE_CHANGES / "sdk-v2.md")

        assert [c.kind for c in changes] == [
            ChangeKind.METHOD_RENAME,
            ChangeKind.KWARG_RENAME,
        ]


class TestDeterminism:
    NOTE = dedent(
        """
        ### Pagination is now cursor-based

        Responses no longer include `total_pages` or `num_pages`. Pass `cursor`;
        read `next_cursor` (or `next_page_cursor` on legacy endpoints) and stop
        when `has_more` is false.
        """
    )

    def test_the_first_named_candidate_wins(self):
        contract = _pagination_contract(self.NOTE)

        assert contract.total_pages_key == "total_pages"
        assert contract.next_cursor_key == "next_cursor"
        assert contract.has_more_key == "has_more"

    def test_the_reading_does_not_depend_on_the_hash_seed(self, tmp_path):
        """Several candidates per field used to be read out of a `set`."""
        note = tmp_path / "note.md"
        note.write_text(self.NOTE, encoding="utf-8")
        script = (
            "import sys; from patchahead.ingest.markdown import _pagination_contract as p; "
            "c = p(open(sys.argv[1]).read()); "
            "print(c.total_pages_key, c.next_cursor_key, c.has_more_key)"
        )
        readings = {
            subprocess.run(
                [sys.executable, "-c", script, str(note)],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONHASHSEED": str(seed)},
                check=True,
            ).stdout
            for seed in range(6)
        }

        assert len(readings) == 1, readings
