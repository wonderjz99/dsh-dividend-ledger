"""分红事件来源。"""

from __future__ import annotations

from dividend_ledger.providers.base import DividendProvider, ProviderResult
from dividend_ledger.providers.fixture import FixtureProvider, load_fixture_events
from dividend_ledger.providers.westock import (
    KLINE_HEADER,
    SEARCH_HEADER,
    StockMatch,
    NOT_IMPLEMENTED_KEYWORDS,
    RATE_LIMIT_MARKERS,
    WESTOCK_SHARES_BASIS,
    WestockProvider,
    enrich_close_prev,
    make_cli_runner,
    make_kline_runner,
    make_price_runner,
    make_search_runner,
    parse_dividend_markdown,
    parse_kline_markdown,
    parse_search_markdown,
    parse_stock_block,
)

__all__ = [
    "KLINE_HEADER",
    "SEARCH_HEADER",
    "StockMatch",
    "NOT_IMPLEMENTED_KEYWORDS",
    "RATE_LIMIT_MARKERS",
    "WESTOCK_SHARES_BASIS",
    "DividendProvider",
    "FixtureProvider",
    "ProviderResult",
    "WestockProvider",
    "enrich_close_prev",
    "load_fixture_events",
    "make_cli_runner",
    "make_kline_runner",
    "make_price_runner",
    "make_search_runner",
    "parse_dividend_markdown",
    "parse_kline_markdown",
    "parse_search_markdown",
    "parse_stock_block",
]
