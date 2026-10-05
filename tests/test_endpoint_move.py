"""Rewriting the fixed words of a moved endpoint's path in the URLs that call it."""

from __future__ import annotations

import json

import pytest

from patchahead import engine
from patchahead.domain.change import ChangeKind
from patchahead.handlers.endpoint_move import _outside_braces, moved_run
from tests.test_handlers import change, patched, run


def move(old="/v1/orders/{id}", new="/v2/orders/{id}", method="GET"):
    return change(ChangeKind.ENDPOINT_MOVE, old, new, f"{method} {old}")


class TestWhatMoved:
    @pytest.mark.parametrize(
        "old, new, words",
        [
            ("/pages/deployment", "/pages/deployments", ("/deployment", "/deployments")),
            ("/v1/orders/{id}", "/v2/orders/{id}", ("/v1", "/v2")),
            ("/a/b/c/{x}", "/a/d/e/c/{x}", ("/b", "/d/e")),
        ],
    )
    def test_one_run_of_fixed_words(self, old, new, words):
        assert moved_run(old, new) == words

    @pytest.mark.parametrize(
        "old, new",
        [
            ("/orders", "/v2/orders"),  # only added
            ("/v2/orders", "/orders"),  # only removed
            ("/repositories/{id}/x", "/repos/{owner}/{repo}/x"),  # placeholders changed
            ("/orders/{id}", "/orders/{order_id}"),  # a placeholder renamed
            ("/orders", "/orders"),
        ],
    )
    def test_anything_else_is_not_a_move_it_rewrites(self, old, new):
        assert moved_run(old, new) is None

    def test_only_an_f_strings_literal_text_counts(self):
        text = 'f"{a}/x/{b:{w}}/{{y}}"'

        literal = "".join(c for c, keep in zip(text, _outside_braces(text), strict=True) if keep)

        assert literal == 'f"/x//{{y}}"'


class TestRewriting:
    def test_the_words_change_and_nothing_else(self, make_index):
        source = 'def f(s, base, i):\n    return s.get(f"{base}/v1/orders/{i}", timeout=5)\n'
        index = make_index({"a.py": source})

        _, plan = run(move(), index)

        assert 's.get(f"{base}/v2/orders/{i}", timeout=5)' in patched(source, plan, "a.py")

    def test_a_plain_string_url_is_rewritten(self, make_index):
        source = "def f(s):\n    return s.get('https://api.shop.com/v1/orders/7')\n"
        index = make_index({"a.py": source})

        _, plan = run(move(), index)

        assert "'https://api.shop.com/v2/orders/7'" in patched(source, plan, "a.py")

    def test_words_written_twice_are_reported(self, make_index):
        source = 'def f(s, i):\n    return s.get(f"/v1/orders/{i}" + "?v=/v1")\n'
        index = make_index({"a.py": source})

        report, plan = run(move(), index)

        assert plan.transformations == []
        assert "appears 2 times" in report.findings[0].unpatchable_reason


@pytest.mark.slow
def test_a_move_is_verified_by_a_test_that_fails_on_the_old_path(make_repo, tmp_path):
    repo = make_repo(
        {
            "app/__init__.py": "",
            "app/orders.py": """
                BASE_URL = "https://api.shop.com"


                def order(session, order_id):
                    return session.get(f"{BASE_URL}/v1/orders/{order_id}")
            """,
            "tests/__init__.py": "",
            "tests/test_orders.py": """
                from app.orders import order


                class ShopApi:
                    def get(self, url):
                        if "/v1/" in url:
                            raise LookupError("404: /v1 was retired")
                        return url.rsplit("/", 1)[-1]


                def test_order():
                    assert order(ShopApi(), "42") == "42"
            """,
        }
    )
    document = tmp_path / "changes.json"
    document.write_text(
        json.dumps(
            {
                "changes": [
                    {
                        "title": "orders moved to v2",
                        "kind": "endpoint_move",
                        "target": {
                            "symbol": "/v1/orders/{id}",
                            "replacement": "/v2/orders/{id}",
                            "owner": "GET /v1/orders/{id}",
                            "owner_explicit": True,
                        },
                    }
                ]
            }
        )
    )

    result = engine.migrate(repo, document, engine.EngineOptions(write_artifacts=False))

    assert result.outcome.value == "migrated", result.results[0].message
    assert 'f"{BASE_URL}/v2/orders/{order_id}"' in result.diff
