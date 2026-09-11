"""Change ingestion: classification, extraction, and honest uncertainty."""

from __future__ import annotations

import json

import pytest

from patchahead.domain.change import ChangeKind, Confidence, Severity
from patchahead.ingest import IngestError, parse_file
from patchahead.ingest.base import ChangeDocument, parse_document
from patchahead.ingest.markdown import classify
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
