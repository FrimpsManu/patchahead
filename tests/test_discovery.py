"""Which tests cover a module: by name, and by what they import."""

from __future__ import annotations

import pytest

from patchahead.testing import discovery


@pytest.mark.parametrize(
    "test_source",
    [
        "from app.shop_client import order\n",
        "import app.shop_client\n",
        "from app import shop_client\n",
        "from app.shop_client import (\n    order,\n    order_page,\n)\n",
    ],
)
def test_a_test_that_imports_the_module_covers_it(make_index, test_source):
    """Found running the Action live: `tests/test_order_lookup.py` covered
    `app/shop_client.py` and was never targeted, so a correct patch was reported
    unverified."""
    index = make_index(
        {"app/shop_client.py": "def order(): ...\n", "tests/test_order_lookup.py": test_source}
    )

    assert discovery.tests_for_path(index, "app/shop_client.py") == ["tests/test_order_lookup.py"]


def test_name_matches_come_first_then_importers_then_fuzzy_names(make_index):
    index = make_index(
        {
            "app/client.py": "",
            "tests/test_client.py": "",
            "tests/test_orders.py": "from app.client import fetch\n",
            "tests/test_client_retries.py": "",
        }
    )

    assert discovery.tests_for_path(index, "app/client.py") == [
        "tests/test_client.py",
        "tests/test_orders.py",
        "tests/test_client_retries.py",
    ]


@pytest.mark.parametrize(
    "test_source",
    [
        "from app.shop_client_v2 import order\n",  # a different module that starts the same
        "from other.shop_client import order\n",  # same name, another package
        "from . import shop_client\n",  # relative: which package is not known here
        "from app import other\n",
    ],
)
def test_other_imports_do_not_count(make_index, test_source):
    index = make_index({"app/shop_client.py": "", "tests/test_orders.py": test_source})

    assert discovery.tests_for_path(index, "app/shop_client.py") == []


def test_a_src_layout_and_a_package_init_are_named_by_their_import(make_index):
    index = make_index(
        {
            "src/shop/__init__.py": "",
            "src/shop/client.py": "",
            "tests/test_a.py": "import shop\n",
            "tests/test_b.py": "from shop.client import x\n",
        }
    )

    assert discovery.tests_for_path(index, "src/shop/__init__.py") == ["tests/test_a.py"]
    assert discovery.tests_for_path(index, "src/shop/client.py") == ["tests/test_b.py"]
