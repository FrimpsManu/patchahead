"""Reading two versions of an OpenAPI spec into changes PatchAhead migrates."""

from __future__ import annotations

import json

import pytest

from patchahead import openapi
from patchahead.cli import EXIT_OK, EXIT_USAGE, main
from patchahead.domain.change import ChangeKind, Confidence


def spec(schemas=None, paths=None, version="1"):
    return {
        "openapi": "3.0.3",
        "info": {"title": "Shop", "version": version},
        "paths": paths or {},
        "components": {"schemas": schemas or {}},
    }


def obj(**properties):
    return {"type": "object", "properties": properties}


NUMBER = {"type": "number"}
STRING = {"type": "string"}


def changes(old, new):
    return openapi.compare(openapi.read(old), openapi.read(new)).changes


class TestReading:
    def test_shapes_are_spelled_one_way(self):
        document = spec(
            {
                "Order": obj(
                    total=NUMBER,
                    placed={"type": "string", "format": "date-time"},
                    buyer={"$ref": "#/components/schemas/Customer"},
                    lines={"type": "array", "items": {"$ref": "#/components/schemas/Line"}},
                    note={"type": ["string", "null"]},
                    either={"oneOf": [STRING, {"type": "null"}]},
                )
            }
        )

        shapes = {
            p.name: p.shape for p in openapi.read(document).schemas["Order"].properties.values()
        }

        assert shapes == {
            "total": "number",
            "placed": "string/date-time",
            "buyer": "ref:Customer",
            "lines": "array<ref:Line>",
            "note": "string",
            "either": "string",
        }

    def test_all_of_members_are_merged_and_cycles_stop(self):
        document = spec(
            {
                "Base": obj(id=STRING),
                "Order": {"allOf": [{"$ref": "#/components/schemas/Base"}, obj(total=NUMBER)]},
                "Loop": {"allOf": [{"$ref": "#/components/schemas/Loop"}, obj(x=NUMBER)]},
            }
        )

        schemas = openapi.read(document).schemas

        assert set(schemas["Order"].properties) == {"id", "total"}
        assert set(schemas["Loop"].properties) == {"x"}

    def test_an_operations_own_parameter_overrides_the_paths(self):
        page = {"name": "page", "in": "query", "schema": {"type": "integer"}}
        document = spec(
            paths={
                "/orders": {
                    "parameters": [page],
                    "get": {
                        "operationId": "listOrders",
                        "parameters": [dict(page, schema=STRING, required=True)],
                    },
                }
            }
        )

        operation = openapi.read(document).operations[("get", "/orders")]

        assert operation.parameters[("query", "page")].shape == "string"
        assert ("query", "page") in operation.required

    def test_swagger_2_parameters_carry_their_type_inline(self):
        document = {
            "swagger": "2.0",
            "info": {"title": "Shop", "version": "1"},
            "paths": {
                "/orders": {
                    "get": {
                        "operationId": "listOrders",
                        "parameters": [{"name": "page", "in": "query", "type": "integer"}],
                    }
                }
            },
            "definitions": {"Order": obj(total=NUMBER)},
        }

        read = openapi.read(document)

        assert read.operations[("get", "/orders")].parameters[("query", "page")].shape == "integer"
        assert "Order" in read.schemas

    def test_a_document_that_is_not_a_spec_is_refused(self):
        with pytest.raises(openapi.SpecError, match="no `openapi` or `swagger`"):
            openapi.read({"name": "package.json"})

    def test_yaml_is_read_when_pyyaml_is_installed(self, tmp_path):
        pytest.importorskip("yaml")
        path = tmp_path / "spec.yaml"
        path.write_text(
            "openapi: 3.0.3\ninfo: {title: Shop, version: '1'}\npaths: {}\n"
            "components:\n  schemas:\n    Order:\n      properties:\n        total: {type: number}\n"
        )

        assert openapi.load(path).schemas["Order"].properties["total"].shape == "number"


class TestComparing:
    def test_a_rename_asserts_the_schema_as_its_owner(self):
        [change] = changes(spec({"Order": obj(total=NUMBER)}), spec({"Order": obj(amount=NUMBER)}))

        assert change.kind is ChangeKind.FIELD_RENAME
        assert (change.target.symbol, change.target.replacement) == ("total", "amount")
        assert change.target.owner == "Order" and change.target.owner_is_explicit
        assert change.confidence is Confidence.MEDIUM
        assert change.source == "openapi-diff"

    def test_a_deprecated_pointer_is_high_confidence(self):
        deprecated = {"type": "number", "deprecated": True, "description": "Use `amount`."}

        [change] = changes(
            spec({"Order": obj(total=NUMBER)}),
            spec({"Order": obj(total=deprecated, amount=NUMBER)}),
        )

        assert change.kind is ChangeKind.FIELD_RENAME and change.confidence is Confidence.HIGH

    def test_a_pointer_to_a_different_type_is_reported(self):
        deprecated = {"type": "number", "deprecated": True, "description": "Use `amount`."}

        [change] = changes(
            spec({"Order": obj(total=NUMBER)}),
            spec({"Order": obj(total=deprecated, amount=STRING)}),
        )

        assert change.kind is ChangeKind.UNSUPPORTED
        assert "different type" in change.title

    def test_a_pointer_must_be_a_whole_word(self):
        """`amount_cents` in a description is not a pointer to `amount`."""
        deprecated = {"type": "number", "deprecated": True, "description": "See amount_cents."}

        assert (
            changes(
                spec({"Order": obj(total=NUMBER)}),
                spec({"Order": obj(total=deprecated, amount=NUMBER)}),
            )
            == []
        )

    @pytest.mark.parametrize(
        "operation_id, method",
        [
            ("listOrders", "list_orders"),
            ("GetOrderByID", "get_order_by_id"),
            ("orders.list", "orders_list"),
            ("copilot/add-copilot-seats-for-teams", "copilot_add_copilot_seats_for_teams"),
        ],
    )
    def test_operation_ids_become_python_method_names(self, operation_id, method):
        assert openapi.snake_case(operation_id) == method


class TestCommand:
    @pytest.fixture
    def specs(self, tmp_path):
        old = tmp_path / "v1.json"
        new = tmp_path / "v2.json"
        old.write_text(json.dumps(spec({"Order": obj(total=NUMBER)}, version="1")))
        new.write_text(json.dumps(spec({"Order": obj(amount=NUMBER)}, version="2")))
        return old, new

    def test_writes_a_change_document_migrate_reads(self, specs, tmp_path, capsys):
        out = tmp_path / "changes.json"

        code = main(["openapi-diff", str(specs[0]), str(specs[1]), "--out", str(out)])

        assert code == EXIT_OK
        assert "`Order.total` renamed to `amount`" in capsys.readouterr().out
        [change] = json.loads(out.read_text())["changes"]
        assert change["target"] == {
            "symbol": "total",
            "replacement": "amount",
            "owner": "Order",
            "owner_explicit": True,
        }

    def test_the_change_document_migrates_a_repository(self, specs, tmp_path, make_repo):
        out = tmp_path / "changes.json"
        main(["openapi-diff", str(specs[0]), str(specs[1]), "--out", str(out)])
        repo = make_repo(
            {
                "app/__init__.py": "",
                "app/report.py": """
                    def revenue(orders):
                        return sum(order["total"] for order in orders)


                    def spent(customer):
                        return customer["total"]
                """,
            }
        )

        from patchahead import engine

        run = engine.migrate(
            repo, out, engine.EngineOptions(run_tests=False, write_artifacts=False)
        )

        assert 'order["amount"]' in run.diff
        assert 'customer["total"]' not in run.diff, "another schema's field is not touched"

    def test_a_missing_file_is_a_usage_error(self, tmp_path, specs):
        assert main(["openapi-diff", str(tmp_path / "nope.json"), str(specs[1])]) == EXIT_USAGE
