"""Tests for ShopifyClient.graphql()/graphql_paginate() (REST->GraphQL migration).

No live network: every test drives a real ShopifyClient against an
httpx.MockTransport, so the retry/pagination/error-handling logic is
exercised for real, not mocked away. Auth uses the static
SHOPIFY_ACCESS_TOKEN shortcut so `_refresh_token()` never runs.

Run from the repository root:

    .venv/bin/python -m pytest tests/test_shopify_client.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shopify_sync.client import (  # noqa: E402
    GraphQLError,
    ShopifyClient,
    _is_throttled,
    _throttle_wait_seconds,
)

SHOP = "test-shop.myshopify.com"
API_VERSION = "2026-07"


def _client_with_transport(handler: Callable[[httpx.Request], httpx.Response]) -> ShopifyClient:
    client = ShopifyClient(
        shop_domain=SHOP, api_version=API_VERSION, access_token="static-test-token"
    )
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
        timeout=5.0,
    )
    return client


def _json_response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


# --------------------------------------------------------------------------
# graphql() — success and error handling
# --------------------------------------------------------------------------


def test_graphql_returns_the_body_on_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"data": {"shop": {"name": "Test"}}, "extensions": {}})

    client = _client_with_transport(handler)
    body = client.graphql("{ shop { name } }")
    assert body["data"]["shop"]["name"] == "Test"


def test_graphql_sends_the_access_token_header_and_query_body():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-Shopify-Access-Token")
        import json

        seen["body"] = json.loads(request.content)
        return _json_response({"data": {"ok": True}})

    client = _client_with_transport(handler)
    client.graphql("{ ok }", variables={"first": 5})

    assert seen["token"] == "static-test-token"
    assert seen["body"] == {"query": "{ ok }", "variables": {"first": 5}}


def test_graphql_raises_graphql_error_on_a_non_throttled_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"data": None, "errors": [{"message": "Field does not exist"}]}
        )

    client = _client_with_transport(handler)
    with pytest.raises(GraphQLError) as exc_info:
        client.graphql("{ bogusField }")
    assert exc_info.value.errors == [{"message": "Field does not exist"}]


def test_graphql_does_not_treat_partial_data_with_errors_as_success():
    """A 200 with both `data` and a non-empty `errors` array must still raise
    -- partial data alongside a real error is not a success."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "data": {"orders": {"nodes": [{"id": "1"}]}},
                "errors": [{"message": "something failed downstream"}],
            }
        )

    client = _client_with_transport(handler)
    with pytest.raises(GraphQLError):
        client.graphql("{ orders { nodes { id } } }")


def test_graphql_retries_on_throttled_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _json_response(
                {
                    "data": None,
                    "errors": [
                        {
                            "message": "Throttled",
                            "extensions": {"code": "THROTTLED"},
                        }
                    ],
                    "extensions": {
                        "cost": {
                            "requestedQueryCost": 100,
                            "throttleStatus": {
                                "currentlyAvailable": 0,
                                "restoreRate": 1000.0,
                            },
                        }
                    },
                }
            )
        return _json_response({"data": {"ok": True}})

    client = _client_with_transport(handler)
    body = client.graphql("{ ok }")
    assert body["data"]["ok"] is True
    assert calls["n"] == 2


def test_graphql_exhausts_retries_and_raises_the_last_errors_on_persistent_throttling():
    calls = {"n": 0}
    throttled_error = {"message": "Throttled", "extensions": {"code": "THROTTLED"}}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _json_response(
            {
                "data": None,
                "errors": [throttled_error],
                "extensions": {
                    "cost": {
                        "requestedQueryCost": 10,
                        "throttleStatus": {"currentlyAvailable": 0, "restoreRate": 1000.0},
                    }
                },
            }
        )

    client = _client_with_transport(handler)
    with pytest.raises(GraphQLError) as exc_info:
        client.graphql("{ ok }")

    # Bounded: exactly MAX_RETRIES attempts, not an infinite or open-ended loop.
    from shopify_sync.client import MAX_RETRIES

    assert calls["n"] == MAX_RETRIES
    assert exc_info.value.errors == [throttled_error]


# --------------------------------------------------------------------------
# THROTTLED helpers
# --------------------------------------------------------------------------


def test_is_throttled_detects_the_throttled_extension_code():
    assert _is_throttled([{"extensions": {"code": "THROTTLED"}}]) is True
    assert _is_throttled([{"message": "some other error"}]) is False
    assert _is_throttled([]) is False


def test_throttle_wait_seconds_computes_the_deficit_over_restore_rate():
    extensions = {
        "cost": {
            "requestedQueryCost": 500,
            "throttleStatus": {"currentlyAvailable": 100, "restoreRate": 200.0},
        }
    }
    # deficit = 500 - 100 = 400; 400 / 200 = 2.0s
    assert _throttle_wait_seconds(extensions) == 2.0


def test_throttle_wait_seconds_falls_back_when_the_shape_is_incomplete():
    assert _throttle_wait_seconds(None) > 0
    assert _throttle_wait_seconds({}) > 0
    assert _throttle_wait_seconds({"cost": {}}) > 0


def test_throttle_wait_seconds_falls_back_when_there_is_no_real_deficit():
    extensions = {
        "cost": {
            "requestedQueryCost": 50,
            "throttleStatus": {"currentlyAvailable": 500, "restoreRate": 1000.0},
        }
    }
    assert _throttle_wait_seconds(extensions) > 0


# --------------------------------------------------------------------------
# graphql_paginate()
# --------------------------------------------------------------------------


def test_graphql_paginate_yields_nodes_across_multiple_pages():
    pages = [
        {
            "data": {
                "orders": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
                    "nodes": [{"id": "1"}, {"id": "2"}],
                }
            }
        },
        {
            "data": {
                "orders": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"id": "3"}],
                }
            }
        },
    ]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages[calls["n"]]
        calls["n"] += 1
        return _json_response(page)

    client = _client_with_transport(handler)
    nodes = list(client.graphql_paginate("query { orders { ... } }", {"first": 2}, ("orders",)))

    assert [n["id"] for n in nodes] == ["1", "2", "3"]
    assert calls["n"] == 2


def test_graphql_paginate_sends_the_cursor_from_the_previous_page():
    seen_cursors: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen_cursors.append(body.get("variables", {}).get("after"))
        if len(seen_cursors) == 1:
            return _json_response(
                {
                    "data": {
                        "orders": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "abc"},
                            "nodes": [{"id": "1"}],
                        }
                    }
                }
            )
        return _json_response(
            {
                "data": {
                    "orders": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"id": "2"}],
                    }
                }
            }
        )

    client = _client_with_transport(handler)
    list(client.graphql_paginate("query { orders { ... } }", {"first": 1}, ("orders",)))

    assert seen_cursors == [None, "abc"]


def test_graphql_paginate_stops_at_a_single_page_with_no_next():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "data": {
                    "orders": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"id": "1"}],
                    }
                }
            }
        )

    client = _client_with_transport(handler)
    nodes = list(client.graphql_paginate("query { orders { ... } }", {"first": 1}, ("orders",)))
    assert [n["id"] for n in nodes] == ["1"]
