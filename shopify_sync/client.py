"""Thin Shopify Admin API client — REST and GraphQL.

Handles auth, the base URL, and two independent pagination/retry schemes:
REST's Link-header cursor pagination with a bounded retry on 429, and
GraphQL's `pageInfo`/cursor pagination with a bounded retry on THROTTLED
(a 200 response with an `errors` entry, not an HTTP 429 — GraphQL signals
rate limits differently, so it needs its own retry path, not a REST one
reused). Nothing here prints or logs credential values (client secret,
access token) or puts them in a URL/query string.

Auth: if SHOPIFY_ACCESS_TOKEN is set, it's used directly (kept as an
optional shortcut, e.g. for a token minted by some other means). Otherwise
this exchanges SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET for a short-lived
token via the OAuth client_credentials grant (Shopify's replacement for
permanent custom-app tokens as of 2026) and refreshes it as needed — see
https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant

REST and GraphQL share this one client (same token, same refresh) rather
than being split into two classes: `graphql()`/`graphql_paginate()` call
`_ensure_fresh_token()` exactly as the REST methods do, deliberately, so
there is one auth lifecycle, not two.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import httpx

logger = logging.getLogger("shopify_sync.client")

MAX_RETRIES = 5
DEFAULT_RETRY_AFTER_S = 2.0
TOKEN_REFRESH_MARGIN_S = 60.0


class GraphQLError(Exception):
    """Raised when a GraphQL response's top-level `errors` array is non-empty
    and not (or no longer) retryable THROTTLED entries.

    GraphQL returns HTTP 200 even on failure, with the failure signalled
    inside the body — a partial-data response alongside a non-empty
    `errors` array must not be treated as success, so this is raised rather
    than returned.
    """

    def __init__(self, errors: list[Any]) -> None:
        self.errors = errors
        super().__init__(str(errors))


class ShopifyClient:
    def __init__(
        self,
        shop_domain: str,
        api_version: str,
        access_token: str = "",
        client_id: str = "",
        client_secret: str = "",
    ) -> None:
        if not shop_domain:
            raise ValueError("SHOPIFY_SHOP_DOMAIN must be set in .env")
        if not access_token and not (client_id and client_secret):
            raise ValueError(
                "Set either SHOPIFY_ACCESS_TOKEN, or both SHOPIFY_CLIENT_ID and "
                "SHOPIFY_CLIENT_SECRET, in .env"
            )

        self._shop_domain = shop_domain
        self._base_url = f"https://{shop_domain}/admin/api/{api_version}"
        self._client_id = client_id
        self._client_secret = client_secret

        self._token = access_token
        self._token_expires_at: float | None = None  # None = static token, never expires
        self._auth_client = httpx.Client(timeout=30.0)
        self._client = httpx.Client(headers={"Accept": "application/json"}, timeout=30.0)

        if not self._token:
            self._refresh_token()

    def close(self) -> None:
        self._client.close()
        self._auth_client.close()

    def __enter__(self) -> "ShopifyClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _refresh_token(self) -> None:
        """Exchange client_id/client_secret for a token via client_credentials grant."""
        resp = self._auth_client.post(
            f"https://{self._shop_domain}/admin/oauth/access_token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.monotonic() + float(payload.get("expires_in", 86399))
        logger.info("obtained Shopify access token via client_credentials grant")

    def _ensure_fresh_token(self) -> None:
        if self._token_expires_at is None:
            return  # static token from SHOPIFY_ACCESS_TOKEN, nothing to refresh
        if time.monotonic() >= self._token_expires_at - TOKEN_REFRESH_MARGIN_S:
            self._refresh_token()

    def graphql(
        self, document: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute one GraphQL query or mutation.

        Raises GraphQLError if the response's top-level `errors` array is
        non-empty and not a retryable THROTTLED entry — GraphQL returns 200
        even on failure, so a non-empty `errors` array alongside partial
        `data` must not be treated as success.

        THROTTLED (rate-limited) is signalled inside `errors`, not via HTTP
        429, so it is not caught by anything in `_get_with_retry` — it needs
        its own bounded retry, backing off using the cost extension's
        `throttleStatus` rather than a fixed `Retry-After` header (GraphQL
        responses carry no such header).
        """
        payload: dict[str, Any] = {"query": document}
        if variables is not None:
            payload["variables"] = variables

        last_errors: list[Any] | None = None
        for _attempt in range(MAX_RETRIES):
            self._ensure_fresh_token()
            resp = self._client.post(
                f"{self._base_url}/graphql.json",
                json=payload,
                headers={
                    "X-Shopify-Access-Token": self._token,
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            body = resp.json()
            errors = body.get("errors")
            if not errors:
                return body

            if not _is_throttled(errors):
                raise GraphQLError(errors)

            wait_s = _throttle_wait_seconds(body.get("extensions"))
            logger.warning("GraphQL throttled, retrying in %.1fs", wait_s)
            time.sleep(wait_s)
            last_errors = errors

        raise GraphQLError(last_errors)

    def graphql_paginate(
        self, document: str, variables: dict[str, Any], path: tuple[str, ...]
    ) -> Iterator[dict[str, Any]]:
        """Yield individual nodes from a cursor-paginated GraphQL connection.

        `path` names the keys from the response's `data` down to the
        connection object (e.g. ``("orders",)`` for ``data["orders"]``),
        which must carry `pageInfo` and `nodes` — GraphQL's cursor shape is
        a different pagination mechanism from REST's Link headers, so this
        is a separate method rather than an extension of `paginate()`.
        """
        call_variables = dict(variables)
        while True:
            body = self.graphql(document, call_variables)
            connection: Any = body["data"]
            for key in path:
                connection = connection[key]
            for node in connection["nodes"]:
                yield node

            page_info = connection["pageInfo"]
            if not page_info.get("hasNextPage"):
                return
            call_variables["after"] = page_info["endCursor"]

    def paginate(
        self, path: str, params: dict[str, Any], root_key: str
    ) -> Iterator[dict[str, Any]]:
        """Yield individual records from a paginated Shopify REST list endpoint."""
        url: str | None = f"{self._base_url}/{path}"
        next_params: dict[str, Any] | None = dict(params)

        while url is not None:
            resp = self._get_with_retry(url, next_params)
            payload = resp.json()
            for record in payload.get(root_key, []):
                yield record

            # A Link "next" URL already carries its own full query string
            # (including page_info), so params are only sent on the first call.
            url = self._next_page_url(resp)
            next_params = None

    def _get_with_retry(self, url: str, params: dict[str, Any] | None) -> httpx.Response:
        last_resp: httpx.Response | None = None
        for attempt in range(MAX_RETRIES):
            self._ensure_fresh_token()
            resp = self._client.get(
                url, params=params, headers={"X-Shopify-Access-Token": self._token}
            )
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", DEFAULT_RETRY_AFTER_S))
                logger.warning("rate limited by Shopify, retrying in %.1fs", retry_after)
                time.sleep(retry_after)
                last_resp = resp
                continue
            resp.raise_for_status()
            return resp
        assert last_resp is not None
        last_resp.raise_for_status()
        return last_resp

    @staticmethod
    def _next_page_url(resp: httpx.Response) -> str | None:
        link = resp.headers.get("Link")
        if not link:
            return None
        for part in link.split(","):
            segment, _, rel = part.strip().partition(";")
            if 'rel="next"' in rel:
                return segment.strip().strip("<>")
        return None


def _is_throttled(errors: list[Any]) -> bool:
    return any(
        isinstance(e, dict) and e.get("extensions", {}).get("code") == "THROTTLED"
        for e in errors
    )


def _throttle_wait_seconds(extensions: dict[str, Any] | None) -> float:
    """How long to back off, from the cost extension's `throttleStatus`.

    Falls back to DEFAULT_RETRY_AFTER_S if the extension is missing or
    doesn't carry the fields needed to compute a real deficit — untested in
    practice against a live THROTTLED response (this store's bucket/restore
    rate is generous enough that triggering one deliberately would need
    sustained high-volume traffic), so this stays conservative rather than
    assuming the shape is always fully populated.
    """
    if not extensions:
        return DEFAULT_RETRY_AFTER_S
    cost = extensions.get("cost") or {}
    throttle = cost.get("throttleStatus") or {}
    available = throttle.get("currentlyAvailable")
    restore_rate = throttle.get("restoreRate")
    requested = cost.get("requestedQueryCost")
    if available is None or not restore_rate or requested is None:
        return DEFAULT_RETRY_AFTER_S
    deficit = requested - available
    if deficit <= 0:
        return DEFAULT_RETRY_AFTER_S
    return deficit / restore_rate
