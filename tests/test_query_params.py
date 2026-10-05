"""Renaming a query parameter in the HTTP calls that address one endpoint."""

from __future__ import annotations

import json

import pytest

from patchahead import engine
from patchahead.domain.change import ChangeKind
from patchahead.handlers.query_params import Endpoint
from tests.test_handlers import change, patched, run


def rename(owner="GET [/v2]/orders"):
    return change(ChangeKind.QUERY_PARAM_RENAME, "page", "cursor", owner)


class TestEndpoints:
    @pytest.mark.parametrize(
        "owner, shown",
        [
            ("GET /orders", "GET /orders"),
            ("get [/v2]/orders", "GET [/v2]/orders"),
            ("DELETE /orders/{id}", "DELETE /orders/{id}"),
        ],
    )
    def test_an_owner_is_read_as_an_endpoint(self, owner, shown):
        assert str(Endpoint.parse(owner)) == shown

    def test_an_owner_that_is_not_an_endpoint_is_not_supported(self, make_index):
        from patchahead import handlers

        assert Endpoint.parse("fetch_orders") is None
        assert handlers.find_handler(rename(owner="fetch_orders")) is None

    @pytest.mark.parametrize(
        "url, matches",
        [
            ("\0/orders", True),  # f"{BASE_URL}/orders", base path inside BASE_URL
            ("\0/v2/orders", True),  # f"{HOST}/v2/orders"
            ("https://api.shop.com/v2/orders", True),
            ("https://api.shop.com/orders", True),  # a server without the base path
            ("/orders", True),  # a client with base_url set
            ("/orders?page=2", True),
            ("\0/customers/\0/orders", False),
            ("\0/orders/\0", False),
            ("\0/invoices", False),
            ("\0", False),
            ("orders", False),
        ],
    )
    def test_urls_match_only_the_endpoints_path(self, url, matches):
        assert Endpoint.parse("GET [/v2]/orders").matches(url) is matches

    def test_a_placeholder_matches_any_segment_and_an_interpolation_only_a_placeholder(self):
        endpoint = Endpoint.parse("GET /customers/{id}/orders")

        assert endpoint.matches("\0/customers/\0/orders")
        assert endpoint.matches("/customers/42/orders")
        assert not Endpoint.parse("GET /customers/me/orders").matches("\0/customers/\0/orders")


class TestRenaming:
    def test_the_key_is_renamed_in_its_quote_style(self, make_index):
        source = (
            "import requests\n"
            'BASE = "https://api.shop.com/v2"\n'
            "def f(n):\n"
            "    return requests.get(f'{BASE}/orders', params={'page': n, 'limit': 5})\n"
        )
        index = make_index({"a.py": source})

        _, plan = run(rename(), index)

        assert "params={'cursor': n, 'limit': 5}" in patched(source, plan, "a.py")

    def test_a_dict_call_keyword_is_renamed(self, make_index):
        source = (
            'def f(client, n):\n    return client.get("/orders", params=dict(page=n, rows=2))\n'
        )
        index = make_index({"a.py": source})

        _, plan = run(rename(), index)

        assert "params=dict(cursor=n, rows=2)" in patched(source, plan, "a.py")

    def test_a_local_dict_built_once_is_renamed(self, make_index):
        source = (
            "def f(s, base, n):\n"
            '    query = {"page": n}\n'
            '    return s.request("GET", f"{base}/orders", params=query)\n'
        )
        index = make_index({"a.py": source})

        _, plan = run(rename(), index)

        assert 'query = {"cursor": n}' in patched(source, plan, "a.py")

    def test_a_url_from_an_enclosing_function_is_read_there(self, make_index):
        source = (
            "URL = '/invoices'\n"
            "def outer(s, base):\n"
            '    URL = f"{base}/orders"\n'
            "    def inner(n):\n"
            '        return s.get(URL, params={"page": n})\n'
            "    return inner\n"
        )
        index = make_index({"a.py": source})

        report, _ = run(rename(), index)

        assert [f.reference.line for f in report.findings] == [5]

    @pytest.mark.parametrize(
        "body, why",
        [
            ('    q = {"page": n}\n    q["limit"] = 5\n', "also used elsewhere"),
            ('    q = {"page": n}\n    q = {"page": n + 1}\n', "not a dictionary built once"),
        ],
    )
    def test_a_dict_it_cannot_follow_is_reported(self, make_index, body, why):
        source = f'def f(s, base, n):\n{body}    return s.get(f"{{base}}/orders", params=q)\n'
        index = make_index({"a.py": source})

        report, plan = run(rename(), index)

        assert plan.transformations == []
        assert why in report.findings[0].unpatchable_reason

    def test_a_call_that_already_passes_the_new_name_is_reported(self, make_index):
        source = (
            'def f(s, b, n):\n    return s.get(f"{b}/orders", params={"page": n, "cursor": n})\n'
        )
        index = make_index({"a.py": source})

        report, plan = run(rename(), index)

        assert plan.transformations == []
        assert "already passed" in report.findings[0].unpatchable_reason


@pytest.mark.slow
def test_a_migration_is_verified_by_a_test_that_fails_on_the_old_name(make_repo, tmp_path):
    """The whole pipeline: a fake session that, like the new API, rejects `page`."""
    repo = make_repo(
        {
            "app/__init__.py": "",
            "app/orders.py": """
                BASE_URL = "https://api.shop.com/v2"


                def recent_orders(session, page):
                    response = session.get(f"{BASE_URL}/orders", params={"page": page})
                    return response["items"]
            """,
            "tests/__init__.py": "",
            "tests/test_orders.py": """
                from app.orders import recent_orders


                class ShopApiV2:
                    def get(self, url, params):
                        assert url.endswith("/v2/orders")
                        if "page" in params:
                            raise ValueError("unknown query parameter: page")
                        return {"items": [params["cursor"]]}


                def test_recent_orders():
                    assert recent_orders(ShopApiV2(), 3) == [3]
            """,
        }
    )
    document = tmp_path / "changes.json"
    document.write_text(
        json.dumps(
            {
                "changes": [
                    {
                        "title": "page renamed to cursor",
                        "kind": "query_param_rename",
                        "target": {
                            "symbol": "page",
                            "replacement": "cursor",
                            "owner": "GET [/v2]/orders",
                            "owner_explicit": True,
                        },
                    }
                ]
            }
        )
    )

    result = engine.migrate(repo, document, engine.EngineOptions(write_artifacts=False))

    assert result.outcome.value == "migrated", result.results[0].message
    assert 'params={"cursor": page}' in result.diff
