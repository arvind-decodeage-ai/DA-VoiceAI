"""Standalone Shopify -> Postgres sync.

Deliberately decoupled from agent/, api/, and CallState: pulls orders and
customers from the Shopify Admin API into the `shopify` Postgres schema so
that schema exists and is populated, independent of whether/when anything
else in this repo reads it.
"""
