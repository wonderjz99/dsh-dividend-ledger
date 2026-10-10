"""三分区汇总测试（SPEC §6.3）。

本文件的数字全部是**手工算出来的**，不是从程序输出里复制回来的——
否则测试只能证明"代码没变"，证明不了"算得对"。
"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal

from dividend_ledger.ledger import build_ledger
from dividend_ledger.models import DividendEvent, Holding
from dividend_ledger.report import ReportContext, build_report


def holding(**overrides) -> Holding:
    base = {
        "code": "600519.SH",
        "start_date": "2020-01-01",
        "shares": "100",
        "cost_per_share": "100",
    }
    base.update(overrides)
    return Holding.from_dict(base)


def event(**overrides) -> DividendEvent:
    base = {"code": "600519.SH", "ex_date": "2024-06-19", "dividend_per_share": "10"}
    base.update(overrides)
    return DividendEvent.from_dict(base)


def make_report(holdings, events, *, warnings=()):
    ledger = build_ledger(holdings, events)
    context = ReportContext(
        generated_at="2026-10-10T12:00:00+08:00",
        inputs={"data_dir": "data", "holdings": "data/holdings.json"},
        warnings=tuple(warnings),
    )
    return build_report(ledger, context=context), ledger


class TotalsTest(unittest.TestCase):
    def test_two_events_one_stock(self) -> None:
        """手工算：100 股 × (10 + 20) = 3000；成本 100×100 = 10000；收益率 0.3。"""
        report, _ = make_report(
            [holding(shares="100", cost_per_share="100")],
            [
                event(ex_date="2023-06-16", dividend_per_share="10", close_prev="50"),
                event(ex_date="2024-06-19", dividend_per_share="20", close_prev="40"),
            ],
        )
        totals = report["totals"]
        self.assertEqual(totals["cash_amount"], "3000.00")
        self.assertEqual(totals["cost_basis"], "10000.00")
        self.assertEqual(totals["yield_on_cost"], "0.300000")
        self.assertEqual(totals["event_count"], 2)
        self.assertEqual(totals["stock_count"], 1)

    def test_multiplication_is_exact_for_awkward_numbers(self) -> None:
        """手算：30.876 × 100 = 3087.60。"""
        report, _ = make_report(
            [holding(shares="100")],
            [event(dividend_per_share="30.876")],
        )
        self.assertEqual(report["totals"]["cash_amount"], "3087.60")

    def test_missing_cost_leaves_yield_null_but_keeps_cash(self) -> None:
        """缺成本不能连累金额——金额只依赖股数与每股派息。"""
        report, _ = make_report(
            [holding(cost_per_share=None)],
            [event(dividend_per_share="10")],
        )
        totals = report["totals"]
        self.assertEqual(totals["cash_amount"], "1000.00")
        self.assertIsNone(totals["cost_basis"])
        self.assertIsNone(totals["yield_on_cost"])
        self.assertIn("600519.SH.cost_per_share", report["meta"]["missing_inputs"])

    def test_bonus_and_transfer_totals(self) -> None:
        report, _ = make_report(
            [holding(shares="100")],
            [event(per_share_bonus="0.1", per_share_transfer="0.2")],
        )
        self.assertEqual(report["totals"]["bonus_shares"], "10")
        self.assertEqual(report["totals"]["transfer_shares"], "20")

    def test_empty_ledger_is_valid(self) -> None:
        report, _ = make_report([holding()], [])
        self.assertEqual(report["totals"]["cash_amount"], "0.00")
        self.assertEqual(report["totals"]["event_count"], 0)
        self.assertEqual(report["by_year"], [])
        self.assertEqual(report["by_stock"], [])


class DividendYieldWeightingTest(unittest.TestCase):
    """股息率必须按**股数**加权，而不是把每股派息与股价直接相加。

    否则 1 股茅台和 1 股高速会等价，组合股息率被严重低估。
    """

    def test_share_weighted_not_price_summed(self) -> None:
        # 甲：1000 股 × 股价 10 → 市值 10000；每股派 1 → 现金 1000；个股股息率 10%
        # 乙：10 股   × 股价 100 → 市值 1000；每股派 1 → 现金 10；个股股息率 1%
        # 正确（按股数加权市值）：1010 / 11000 = 0.091818
        # 错误（Σ每股派息/Σ股价）  ：2 / 110 = 0.018182
        report, _ = make_report(
            [
                holding(code="000001.SZ", shares="1000", cost_per_share="10"),
                holding(code="600519.SH", shares="10", cost_per_share="100"),
            ],
            [
                event(code="000001.SZ", ex_date="2024-06-07",
                      dividend_per_share="1", close_prev="10"),
                event(code="600519.SH", ex_date="2024-06-19",
                      dividend_per_share="1", close_prev="100"),
            ],
        )
        totals = report["totals"]
        self.assertEqual(totals["cash_amount"], "1010.00")
        self.assertEqual(totals["dividend_yield"], "0.091818")
        self.assertNotEqual(totals["dividend_yield"], "0.018182")

    def test_partial_price_coverage_is_flagged(self) -> None:
        """有事件缺价格时，股息率只能覆盖一部分，必须标出来。"""
        report, _ = make_report(
            [holding(shares="100")],
            [
                event(ex_date="2023-06-16", dividend_per_share="10", close_prev="50"),
                event(ex_date="2024-06-19", dividend_per_share="10"),
            ],
        )
        totals = report["totals"]
        # 只有第一笔（有价格）参与：现金 1000 ÷ 除权前市值 100×50 = 0.2
        self.assertEqual(totals["dividend_yield"], "0.200000")
        self.assertFalse(totals["dividend_yield_complete"])
        self.assertIn("只覆盖了 1 笔", totals["dividend_yield_note"])

    def test_no_prices_at_all_gives_null(self) -> None:
        report, _ = make_report([holding()], [event()])
        self.assertIsNone(report["totals"]["dividend_yield"])
        self.assertFalse(report["totals"]["dividend_yield_complete"])

    def test_complete_flag_true_when_all_priced(self) -> None:
        report, _ = make_report(
            [holding()], [event(close_prev="50"), event(ex_date="2023-06-16", close_prev="60")]
        )
        self.assertTrue(report["totals"]["dividend_yield_complete"])


class PartitionTest(unittest.TestCase):
    def test_three_partitions_present_with_consistent_fields(self) -> None:
        report, _ = make_report(
            [holding()],
            [event(ex_date="2024-06-19", close_prev="50")],
        )
        for key in ("totals", "by_year", "by_month", "by_stock"):
            self.assertIn(key, report)
        year = report["by_year"][0]
        month = report["by_month"][0]
        stock = report["by_stock"][0]
        for block in (year, month, stock):
            self.assertIn("cash_amount", block)
            self.assertIn("event_count", block)
            self.assertIn("stock_count", block)

    def test_by_year_groups_by_effective_date(self) -> None:
        report, _ = make_report(
            [holding()],
            [
                event(ex_date="2023-06-16", dividend_per_share="10", close_prev="50"),
                event(ex_date="2024-06-19", dividend_per_share="20", close_prev="50"),
            ],
        )
        years = {block["year"]: block["cash_amount"] for block in report["by_year"]}
        self.assertEqual(years, {"2024": "2000.00", "2023": "1000.00"})

    def test_by_year_newest_first(self) -> None:
        report, _ = make_report(
            [holding()],
            [
                event(ex_date="2022-06-17", close_prev="50"),
                event(ex_date="2024-06-19", close_prev="50"),
                event(ex_date="2023-06-16", close_prev="50"),
            ],
        )
        self.assertEqual([b["year"] for b in report["by_year"]], ["2024", "2023", "2022"])

    def test_by_month_uses_effective_date_and_has_no_ratios(self) -> None:
        report, _ = make_report(
            [holding()],
            [event(ex_date="2024-06-19", close_prev="50")],
        )
        month = report["by_month"][0]
        self.assertEqual(month["month"], "2024-06")
        # 月度分区不输出需要分母的比率，避免"月度成本收益率"这种会误导的指标
        self.assertNotIn("yield_on_cost", month)
        self.assertNotIn("cost_basis", month)

    def test_by_year_nests_by_stock(self) -> None:
        report, _ = make_report(
            [
                holding(code="600519.SH", shares="100", cost_per_share="100"),
                holding(code="000001.SZ", shares="200", cost_per_share="50"),
            ],
            [
                event(code="600519.SH", ex_date="2024-06-19",
                      dividend_per_share="10", close_prev="50"),
                event(code="000001.SZ", ex_date="2024-06-07",
                      dividend_per_share="5", close_prev="20"),
            ],
        )
        block = report["by_year"][0]
        self.assertEqual(block["cash_amount"], "2000.00")
        self.assertEqual({s["code"] for s in block["by_stock"]},
                         {"600519.SH", "000001.SZ"})

    def test_by_stock_is_cross_year_cumulative(self) -> None:
        report, _ = make_report(
            [holding()],
            [
                event(ex_date="2023-06-16", dividend_per_share="10", close_prev="50"),
                event(ex_date="2024-06-19", dividend_per_share="20", close_prev="50"),
            ],
        )
        stock = report["by_stock"][0]
        self.assertEqual(stock["cash_amount"], "3000.00")
        self.assertEqual(stock["event_count"], 2)
        self.assertEqual(stock["yield_on_cost"], "0.300000")

    def test_stock_blocks_sorted_by_cash_desc(self) -> None:
        report, _ = make_report(
            [
                holding(code="600519.SH", shares="10", cost_per_share="100"),
                holding(code="000001.SZ", shares="1000", cost_per_share="10"),
            ],
            [
                event(code="600519.SH", ex_date="2024-06-19",
                      dividend_per_share="1", close_prev="10"),
                event(code="000001.SZ", ex_date="2024-06-07",
                      dividend_per_share="1", close_prev="10"),
            ],
        )
        self.assertEqual(report["by_stock"][0]["code"], "000001.SZ")

    def test_year_cost_basis_is_not_sum_of_years(self) -> None:
        """按年成本基数是"该年有分红的股票的全部成本"，跨年相加会重复计算。"""
        report, _ = make_report(
            [holding(shares="100", cost_per_share="100")],
            [
                event(ex_date="2023-06-16", dividend_per_share="10", close_prev="50"),
                event(ex_date="2024-06-19", dividend_per_share="10", close_prev="50"),
            ],
        )
        for block in report["by_year"]:
            self.assertEqual(block["cost_basis"], "10000.00")
        self.assertEqual(report["totals"]["cost_basis"], "10000.00")
        # 两年相加会得到 20000，是错的
        self.assertNotEqual(
            sum(Decimal(b["cost_basis"]) for b in report["by_year"]),
            Decimal(report["totals"]["cost_basis"]),
        )


class MetaTest(unittest.TestCase):
    def test_date_basis_blocks_are_honest(self) -> None:
        """报告必须说清归集依据——不允许把除权日伪装成到账日。"""
        report, _ = make_report([holding()], [event(close_prev="50")])
        meta = report["meta"]
        self.assertEqual(meta["date_basis"], "ex_date")
        self.assertEqual(meta["date_basis_counts"], {"ex_date": 1})
        self.assertIn("除权除息日", meta["date_basis_detail"])

    def test_override_dominates_basis_reporting(self) -> None:
        report, _ = make_report(
            [holding()],
            [
                event(ex_date="2023-06-16", close_prev="50"),
                event(
                    ex_date="2024-06-19",
                    close_prev="50",
                    date_basis="pay_date_override",
                    pay_date="2024-06-26",
                ),
            ],
        )
        self.assertEqual(report["meta"]["date_basis"], "pay_date_override")

    def test_notes_state_tax_and_assumption(self) -> None:
        report, _ = make_report([holding()], [event(close_prev="50")])
        notes = " ".join(report["meta"]["notes"])
        self.assertIn("税前", notes)
        self.assertIn("红利税", notes)
        self.assertIn("股数恒定", notes)

    def test_missing_inputs_listed_as_labels(self) -> None:
        report, _ = make_report(
            [holding(cost_per_share=None)], [event(close_prev="50")]
        )
        self.assertEqual(
            report["meta"]["missing_inputs"], ["600519.SH.cost_per_share"]
        )

    def test_warnings_pass_through(self) -> None:
        report, _ = make_report(
            [holding()], [event(close_prev="50")], warnings=("覆盖项未生效",)
        )
        self.assertEqual(report["meta"]["warnings"], ["覆盖项未生效"])

    def test_excluded_count_noted(self) -> None:
        report, _ = make_report(
            [holding(start_date="2024-06-19")],
            [event(ex_date="2020-01-01", close_prev="50")],
        )
        self.assertIn("排除", " ".join(report["meta"]["notes"]))

    def test_serialisable_to_json_without_floats(self) -> None:
        report, _ = make_report([holding()], [event(close_prev="50")])
        text = json.dumps(report, ensure_ascii=False, indent=2)
        reloaded = json.loads(text)
        self.assertEqual(reloaded["totals"]["cash_amount"], "1000.00")
        self.assertIsInstance(reloaded["totals"]["yield_on_cost"], str)

    def test_report_is_deterministic(self) -> None:
        holdings = [holding()]
        events = [event(close_prev="50"), event(ex_date="2023-06-16", close_prev="60")]
        first, _ = make_report(holdings, events)
        second, _ = make_report(holdings, events)
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
