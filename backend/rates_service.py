"""Read access to LIVE interest rates.

A rate is live only when its ``content_version`` is the published version of its
rate_table content item (maker -> checker -> legal workflow). Used by the public rates
page, calculators, FD booking and loan pricing so every module quotes the same number.
"""
from __future__ import annotations

import sqlite3

LIVE_RATES_SQL = """
SELECT r.*, v.published_at, i.slug AS rate_table_slug
FROM rates r
JOIN content_versions v ON v.id = r.content_version_id AND v.status = 'published'
JOIN content_items i ON i.id = v.content_item_id AND i.published_version_id = v.id
"""


def published_rates(conn: sqlite3.Connection, product_type: str | None = None) -> list[dict]:
    sql = LIVE_RATES_SQL
    params: tuple = ()
    if product_type:
        sql += " WHERE r.product_type = ?"
        params = (product_type,)
    sql += " ORDER BY r.product_type, COALESCE(r.tenor_min_days, 0), COALESCE(r.slab_min_paise, 0)"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _covers(rate: dict, tenure_days: int | None, amount_paise: int | None) -> bool:
    if tenure_days is not None:
        if rate.get("tenor_min_days") is not None and tenure_days < rate["tenor_min_days"]:
            return False
        if rate.get("tenor_max_days") is not None and tenure_days > rate["tenor_max_days"]:
            return False
    if amount_paise is not None:
        if rate.get("slab_min_paise") is not None and amount_paise < rate["slab_min_paise"]:
            return False
        if rate.get("slab_max_paise") is not None and amount_paise > rate["slab_max_paise"]:
            return False
    return True


def find_rate(conn: sqlite3.Connection, product_type: str, *, tenure_days: int | None = None,
              amount_paise: int | None = None) -> dict | None:
    """The live rate row for a product covering the tenure/amount, or None."""
    for rate in published_rates(conn, product_type):
        if _covers(rate, tenure_days, amount_paise):
            return rate
    return None


def fd_rate(conn: sqlite3.Connection, tenure_days: int, senior: bool, amount_paise: int | None = None) -> dict | None:
    return find_rate(conn, "fd_senior" if senior else "fd_general", tenure_days=tenure_days, amount_paise=amount_paise)
