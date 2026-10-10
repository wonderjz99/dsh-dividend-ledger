"""界面指标测试（`dividend_ledger.metrics`）。

两个股息率口径完全不同，测试要盯住三件事：

1. **动态股息率用 TTM 求和，不是"最近一笔"。** 一年内派两次的股票（宁德时代就有），
   只取最后一笔会低估。
2. **预期股息率只算已公告未实施的**，不做任何历史外推。
3. **缺数据是 None，不是 0。**
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from dividend_ledger.metrics import (
    compute_metrics,
    compute_portfolio,
    group_events_by_code,
    trailing_twelve_months,
)
from dividend_ledger.models import DividendEvent, Holding


def event(**overrides) -> DividendEvent:
    base = {
        "code": "600519.SH",
        "ex_date": "2024-06-19",
        "dividend_per_share": "10",
    }
    base.update(overrides)
    return DividendEvent.from_dict(base)


def holding(**overrides) -> Holding:
    base = {
        "code": "600519.SH",
        "start_date": "2020-01-01",
        "shares": "100",
        "cost_per_share": "100",
    }
    base.update(overrides)
    return Holding.from_dict(base)


REFERENCE = date(2026, 10, 10)


class TtmWindowTest(unittest.TestCase):
    def test_window_is_one_year_back(self) -> None:
        self.assertEqual(trailing_twelve_months(REFERENCE), date(2025, 10, 10))

    def test_sums_multiple_payouts_in_window(self) -> None:
        """一年内两次派息要**相加**（真实世界里宁德时代就是这样）。"""
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("100"),
            events=[
                event(ex_date="2025-12-19", dividend_per_share="20"),
                event(ex_date="2026-06-26", dividend_per_share="30"),
            ],
            reference=REFERENCE,
        )
        self.assertEqual(metrics.ttm_per_share, Decimal("50"))
        self.assertEqual(metrics.ttm_event_count, 2)
        self.assertEqual(metrics.dynamic_yield, Decimal("0.5"))

    def test_event_outside_window_excluded(self) -> None:
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("100"),
            events=[
                event(ex_date="2025-06-01", dividend_per_share="99"),  # 窗口外
                event(ex_date="2026-06-26", dividend_per_share="10"),
            ],
            reference=REFERENCE,
        )
        self.assertEqual(metrics.ttm_per_share, Decimal("10"))
        self.assertEqual(metrics.ttm_event_count, 1)

    def test_boundary_day_is_included(self) -> None:
        """恰好落在窗口起始日的事件要计入（含端点）。"""
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("100"),
            events=[event(ex_date="2025-10-10", dividend_per_share="10")],
            reference=REFERENCE,
        )
        self.assertEqual(metrics.ttm_per_share, Decimal("10"))

    def test_future_implemented_event_excluded_from_ttm(self) -> None:
        """除权日在未来的事件不算已收——它属于 expected 那一侧。"""
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("100"),
            events=[event(ex_date="2026-12-31", dividend_per_share="10", status="confirmed")],
            reference=REFERENCE,
        )
        self.assertEqual(metrics.ttm_per_share, Decimal("0"))
        self.assertEqual(metrics.planned_per_share, Decimal("10"))
        self.assertEqual(metrics.expected_yield, Decimal("0.1"))


class ExpectedYieldTest(unittest.TestCase):
    def test_planned_dividend_divides_by_price(self) -> None:
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("50"),
            events=[event(ex_date="2026-12-31", dividend_per_share="2.5", status="confirmed")],
            reference=REFERENCE,
        )
        self.assertEqual(metrics.planned_per_share, Decimal("2.5"))
        self.assertEqual(metrics.planned_event_count, 1)
        self.assertEqual(metrics.expected_yield, Decimal("0.05"))
        self.assertEqual(metrics.next_ex_date, date(2026, 12, 31))

    def test_proposed_also_counts_as_planned(self) -> None:
        """董事会预案也算"已公告"——它会被标成 proposed，但仍进预期口径。"""
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("50"),
            events=[event(ex_date="2026-12-31", dividend_per_share="2", status="proposed")],
            reference=REFERENCE,
        )
        self.assertEqual(metrics.expected_yield, Decimal("0.04"))

    def test_no_plan_gives_none_not_zero(self) -> None:
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("100"),
            events=[event(ex_date="2026-06-26", dividend_per_share="10")],
            reference=REFERENCE,
        )
        self.assertIsNone(metrics.expected_yield)
        self.assertIn("planned_dividend", metrics.missing)

    def test_multiple_plans_summed(self) -> None:
        """同日多方案（普通 + 特殊分红）要相加。"""
        metrics = compute_metrics(
            code="300750.SZ",
            name="宁德时代",
            price=Decimal("100"),
            events=[
                event(
                    code="300750.SZ",
                    ex_date="2026-12-31",
                    dividend_per_share="2",
                    proposal_sn="1",
                    status="confirmed",
                ),
                event(
                    code="300750.SZ",
                    ex_date="2026-12-31",
                    dividend_per_share="3",
                    proposal_sn="2",
                    status="confirmed",
                ),
            ],
            reference=REFERENCE,
        )
        self.assertEqual(metrics.planned_per_share, Decimal("5"))
        self.assertEqual(metrics.expected_yield, Decimal("0.05"))


class MissingInputTest(unittest.TestCase):
    def test_no_price_means_both_yields_none(self) -> None:
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=None,
            events=[
                event(ex_date="2026-06-26", dividend_per_share="10"),
                event(ex_date="2026-12-31", dividend_per_share="5", status="confirmed"),
            ],
            reference=REFERENCE,
        )
        self.assertIsNone(metrics.dynamic_yield)
        self.assertIsNone(metrics.expected_yield)
        self.assertIn("price", metrics.missing)

    def test_zero_price_treated_as_missing(self) -> None:
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("0"),
            events=[event(ex_date="2026-06-26", dividend_per_share="10")],
            reference=REFERENCE,
        )
        self.assertIsNone(metrics.dynamic_yield)

    def test_no_dividend_in_window_is_flagged(self) -> None:
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("100"),
            events=[],
            reference=REFERENCE,
        )
        self.assertIn("ttm_dividend", metrics.missing)
        self.assertEqual(metrics.ttm_per_share, Decimal("0"))


class HoldingMetricsTest(unittest.TestCase):
    def test_received_total_only_counts_after_start_date(self) -> None:
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("100"),
            events=[
                event(ex_date="2019-06-01", dividend_per_share="50"),  # 早于开始持有
                event(ex_date="2026-06-26", dividend_per_share="10"),
            ],
            holding=holding(start_date="2020-01-01", shares="100", cost_per_share="50"),
            reference=REFERENCE,
        )
        self.assertEqual(metrics.received_total, Decimal("1000"))  # 10 × 100
        self.assertEqual(metrics.yield_on_cost, Decimal("0.2"))  # 1000 ÷ 5000

    def test_missing_cost_flagged(self) -> None:
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("100"),
            events=[event(ex_date="2026-06-26", dividend_per_share="10")],
            holding=holding(cost_per_share=None),
            reference=REFERENCE,
        )
        self.assertIsNone(metrics.yield_on_cost)
        self.assertIn("cost_per_share", metrics.missing)

    def test_not_held_gives_none_shares(self) -> None:
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("100"),
            events=[event(ex_date="2026-06-26", dividend_per_share="10")],
            reference=REFERENCE,
        )
        self.assertIsNone(metrics.shares)
        self.assertIsNone(metrics.yield_on_cost)
        self.assertEqual(metrics.received_total, Decimal("0"))


class SerialisationTest(unittest.TestCase):
    def test_to_dict_uses_strings_and_null(self) -> None:
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("1263"),
            events=[event(ex_date="2026-06-26", dividend_per_share="30.876")],
            holding=holding(cost_per_share="1500", shares="100"),
            reference=REFERENCE,
        )
        payload = metrics.to_dict()
        for key in ("price", "ttmPerShare", "dynamicYield", "expectedYield", "receivedTotal"):
            value = payload[key]
            self.assertTrue(value is None or isinstance(value, str), f"{key} 必须是字符串或 null")

        self.assertEqual(payload["price"], "1263.00")
        self.assertEqual(payload["ttmPerShare"], "30.876000")
        self.assertIsNone(payload["expectedYield"])
        self.assertIsInstance(payload["missing"], list)

    def test_real_maotai_numbers(self) -> None:
        """真实值复核：茅台近 12 个月两次派息合计 52.00 元，现价 1263 → 4.12%。"""
        metrics = compute_metrics(
            code="600519.SH",
            name="贵州茅台",
            price=Decimal("1263"),
            events=[
                event(ex_date="2025-12-19", dividend_per_share="23.957"),
                event(ex_date="2026-06-26", dividend_per_share="28.0242"),
            ],
            reference=REFERENCE,
        )
        self.assertEqual(metrics.ttm_per_share, Decimal("51.9812"))
        # 51.9812 ÷ 1263 ≈ 0.041157
        self.assertAlmostEqual(float(metrics.dynamic_yield), 0.041157, places=5)
        self.assertEqual(metrics.to_dict()["dynamicYield"], "0.041157")


class PortfolioTest(unittest.TestCase):
    """整个持仓的总股息率：**按市值加权**，不是把各股股息率简单平均。"""

    def _metrics(self, code, name, price, ttm, planned, shares, cost=None, received=Decimal("0")):
        return compute_metrics(
            code=code,
            name=name,
            price=price,
            events=[],
            holding=holding(code=code, shares=shares, cost_per_share=cost),
            reference=REFERENCE,
        ) if False else self._direct(code, name, price, ttm, planned, cost, received)

    def _direct(self, code, name, price, ttm, planned, cost, received):
        from dividend_ledger.metrics import StockMetrics

        cost_basis = None if cost is None else Decimal(cost)
        return StockMetrics(
            code=code,
            name=name,
            price=price,
            ttm_per_share=ttm,
            ttm_event_count=1,
            dynamic_yield=None,
            planned_per_share=planned,
            planned_event_count=1 if planned > 0 else 0,
            expected_yield=None,
            next_ex_date=None,
            next_plan_text=None,
            shares=None,
            cost_basis=cost_basis,
            yield_on_cost=None,
            received_total=received,
            missing=(),
        )

    def test_market_value_weighted_not_average(self) -> None:
        """手算：甲 1000 股 × 10 元 = 10000 市值，每股派 1 → 1000 元
                乙 10 股 × 100 元 = 1000 市值，每股派 1 → 10 元
        合计 1010 ÷ 11000 = 0.091818。
        把两股股息率（10% 与 1%）简单平均会得到 5.5%，是错的。
        """
        metrics = [
            self._direct("000001.SZ", "甲", Decimal("10"), Decimal("1"), Decimal("0"), None, Decimal("0")),
            self._direct("600519.SH", "乙", Decimal("100"), Decimal("1"), Decimal("0"), None, Decimal("0")),
        ]
        holdings = [
            holding(code="000001.SZ", shares="1000", cost_per_share=None),
            holding(code="600519.SH", shares="10", cost_per_share=None),
        ]
        summary = compute_portfolio(metrics, holdings)
        self.assertEqual(summary.market_value, Decimal("11000"))
        self.assertEqual(summary.dynamic_cash, Decimal("1010"))
        self.assertAlmostEqual(float(summary.dynamic_yield), 0.091818, places=5)
        self.assertNotEqual(round(float(summary.dynamic_yield), 3), 0.055)

    def test_expected_yield_uses_planned(self) -> None:
        metrics = [
            self._direct("600519.SH", "乙", Decimal("100"), Decimal("0"), Decimal("5"), None, Decimal("0")),
        ]
        holdings = [holding(code="600519.SH", shares="100", cost_per_share=None)]
        summary = compute_portfolio(metrics, holdings)
        self.assertEqual(summary.planned_cash, Decimal("500"))
        self.assertEqual(summary.expected_yield, Decimal("0.05"))

    def test_no_planned_dividend_gives_none(self) -> None:
        metrics = [self._direct("600519.SH", "乙", Decimal("100"), Decimal("4"), Decimal("0"), None, Decimal("0"))]
        summary = compute_portfolio(metrics, [holding(code="600519.SH", shares="100", cost_per_share=None)])
        self.assertIsNone(summary.expected_yield)

    def test_missing_price_excludes_from_market_value_and_flags_incomplete(self) -> None:
        metrics = [
            self._direct("600519.SH", "甲", Decimal("100"), Decimal("4"), Decimal("0"), None, Decimal("0")),
            self._direct("000001.SZ", "乙", None, Decimal("1"), Decimal("0"), None, Decimal("0")),
        ]
        holdings = [
            holding(code="600519.SH", shares="100", cost_per_share=None),
            holding(code="000001.SZ", shares="500", cost_per_share=None),
        ]
        summary = compute_portfolio(metrics, holdings)
        self.assertEqual(summary.priced_count, 1)
        self.assertEqual(summary.stock_count, 2)
        self.assertFalse(summary.complete)
        # 分子仍含乙的现金，分母只有甲的市值 —— 但要如实标 incomplete
        self.assertEqual(summary.market_value, Decimal("10000"))

    def test_no_price_at_all_gives_none_yields(self) -> None:
        metrics = [self._direct("600519.SH", "甲", None, Decimal("4"), Decimal("2"), None, Decimal("0"))]
        summary = compute_portfolio(metrics, [holding(code="600519.SH", shares="100", cost_per_share=None)])
        self.assertIsNone(summary.dynamic_yield)
        self.assertIsNone(summary.expected_yield)
        self.assertFalse(summary.complete)

    def test_cost_basis_and_yield_on_cost(self) -> None:
        metrics = [
            self._direct("600519.SH", "甲", Decimal("100"), Decimal("4"), Decimal("0"), "50", Decimal("5000")),
        ]
        summary = compute_portfolio(metrics, [holding(code="600519.SH", shares="100", cost_per_share="50")])
        # 100 股 × 50 = 5000 成本；已收 5000 → 100%
        self.assertEqual(summary.cost_basis, Decimal("5000"))
        self.assertEqual(summary.received_total, Decimal("5000"))
        self.assertEqual(summary.yield_on_cost, Decimal("1"))

    def test_all_missing_cost_gives_none_cost_basis(self) -> None:
        metrics = [self._direct("600519.SH", "甲", Decimal("100"), Decimal("4"), Decimal("0"), None, Decimal("0"))]
        summary = compute_portfolio(metrics, [holding(code="600519.SH", shares="100", cost_per_share=None)])
        self.assertIsNone(summary.cost_basis)
        self.assertIsNone(summary.yield_on_cost)

    def test_empty_portfolio(self) -> None:
        summary = compute_portfolio([], [])
        self.assertEqual(summary.stock_count, 0)
        self.assertIsNone(summary.dynamic_yield)
        self.assertEqual(summary.market_value, Decimal("0"))

    def test_serialisation_uses_strings(self) -> None:
        metrics = [self._direct("600519.SH", "甲", Decimal("100"), Decimal("4"), Decimal("2"), "50", Decimal("500"))]
        payload = compute_portfolio(
            metrics, [holding(code="600519.SH", shares="100", cost_per_share="50")]
        ).to_dict()
        for key in ("marketValue", "dynamicCash", "plannedCash", "dynamicYield"):
            self.assertIsInstance(payload[key], str, key)
        self.assertIsInstance(payload["complete"], bool)

    def test_real_maotai_pingan_catl_numbers(self) -> None:
        """真实持仓复核：3 只、市值加权动态股息率约 3.91%。"""
        metrics = [
            self._direct("600519.SH", "贵州茅台", Decimal("1263"), Decimal("51.9812"), Decimal("0"), "1500", Decimal("17942.90")),
            self._direct("000001.SZ", "平安银行", Decimal("11.59"), Decimal("0.845"), Decimal("0"), "12", Decimal("2457.00")),
            self._direct("300750.SZ", "宁德时代", Decimal("297.75"), Decimal("8.3697"), Decimal("0"), "250", Decimal("4541.20")),
        ]
        holdings = [
            holding(code="600519.SH", shares="100", cost_per_share="1500"),
            holding(code="000001.SZ", shares="1000", cost_per_share="12"),
            holding(code="300750.SZ", shares="200", cost_per_share="250"),
        ]
        summary = compute_portfolio(metrics, holdings)
        self.assertEqual(summary.market_value, Decimal("197440.00"))
        self.assertAlmostEqual(float(summary.dynamic_yield), 0.039084, places=5)
        self.assertTrue(summary.complete)


class GroupingTest(unittest.TestCase):
    def test_groups_by_code(self) -> None:
        grouped = group_events_by_code(
            [
                event(code="600519.SH"),
                event(code="000001.SZ"),
                event(code="600519.SH", ex_date="2025-06-19"),
            ]
        )
        self.assertEqual(set(grouped), {"600519.SH", "000001.SZ"})
        self.assertEqual(len(grouped["600519.SH"]), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
