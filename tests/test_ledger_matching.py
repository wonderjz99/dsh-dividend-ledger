"""归属匹配与单事件计算测试（SPEC §4.2 / §4.4）。

边界是本文件的主角：
- ``ex_date == start_date`` → **计入**（当天除权除息，你已经是股东）
- ``ex_date == start_date - 1 天`` → **排除**
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from dividend_ledger.ledger import (
    MISSING_CLOSE_PREV,
    MISSING_COST_PER_SHARE,
    build_ledger,
    build_ledger_entry,
    filter_events_for_holding,
)
from dividend_ledger.models import DividendEvent, Holding


def holding(**overrides) -> Holding:
    base = {
        "code": "600519.SH",
        "start_date": "2024-06-01",
        "shares": "100",
        "cost_per_share": "1000",
    }
    base.update(overrides)
    return Holding.from_dict(base)


def event(**overrides) -> DividendEvent:
    """默认**不带** close_prev——这样"缺价格"是默认状态，测起来不容易漏。"""
    base = {
        "code": "600519.SH",
        "ex_date": "2024-06-19",
        "dividend_per_share": "10",
    }
    base.update(overrides)
    return DividendEvent.from_dict(base)


class MatchingBoundaryTest(unittest.TestCase):
    def test_same_day_is_included(self) -> None:
        """开始持有日当天除权除息 → 算你的。"""
        owned, dropped = filter_events_for_holding(
            holding(start_date="2024-06-19"), [event(ex_date="2024-06-19")]
        )
        self.assertEqual(len(owned), 1)
        self.assertEqual(dropped, [])

    def test_one_day_before_start_is_excluded(self) -> None:
        owned, dropped = filter_events_for_holding(
            holding(start_date="2024-06-19"), [event(ex_date="2024-06-18")]
        )
        self.assertEqual(owned, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn("早于开始持有日", dropped[0].reason)

    def test_later_event_is_included(self) -> None:
        owned, _ = filter_events_for_holding(
            holding(start_date="2024-06-01"), [event(ex_date="2024-06-19")]
        )
        self.assertEqual(len(owned), 1)

    def test_other_code_ignored_entirely(self) -> None:
        """别的股票的事件既不计入也不算"被排除"——它跟这条持仓无关。"""
        owned, dropped = filter_events_for_holding(
            holding(), [event(code="000001.SZ", ex_date="2024-06-19")]
        )
        self.assertEqual(owned, [])
        self.assertEqual(dropped, [])

    def test_exclusion_uses_ex_date_not_pay_date(self) -> None:
        """排除判定只看除权除息日，哪怕到账日在持有期内也不算你的。"""
        owned, dropped = filter_events_for_holding(
            holding(start_date="2024-06-19"),
            [
                event(
                    ex_date="2024-06-18",
                    date_basis="pay_date_declared",
                    pay_date="2024-07-01",
                )
            ],
        )
        self.assertEqual(owned, [])
        self.assertEqual(len(dropped), 1)

    def test_inclusion_uses_ex_date_even_if_pay_date_earlier(self) -> None:
        owned, _ = filter_events_for_holding(
            holding(start_date="2024-06-19"),
            [
                event(
                    ex_date="2024-06-19",
                    date_basis="pay_date_declared",
                    pay_date="2024-06-20",
                )
            ],
        )
        self.assertEqual(len(owned), 1)

    def test_owned_events_sorted_by_ex_date(self) -> None:
        owned, _ = filter_events_for_holding(
            holding(start_date="2020-01-01"),
            [
                event(ex_date="2024-06-19"),
                event(ex_date="2022-06-17"),
                event(ex_date="2023-06-16"),
            ],
        )
        self.assertEqual(
            [e.ex_date for e in owned],
            [date(2022, 6, 17), date(2023, 6, 16), date(2024, 6, 19)],
        )

    def test_excluded_sorted_by_ex_date(self) -> None:
        _, dropped = filter_events_for_holding(
            holding(start_date="2024-06-19"),
            [event(ex_date="2020-06-10"), event(ex_date="2021-06-10")],
        )
        self.assertEqual(
            [d.ex_date for d in dropped], [date(2020, 6, 10), date(2021, 6, 10)]
        )


class EntryCalculationTest(unittest.TestCase):
    def test_cash_amount(self) -> None:
        entry = build_ledger_entry(holding(shares="100"), event(dividend_per_share="10"))
        self.assertEqual(entry.cash_amount, Decimal("1000"))

    def test_cash_amount_is_exact_for_awkward_decimals(self) -> None:
        """30.876 × 100 必须精确等于 3087.60，不能有浮点尾巴。"""
        entry = build_ledger_entry(
            holding(shares="100"), event(dividend_per_share="30.876")
        )
        self.assertEqual(entry.cash_amount, Decimal("3087.60"))
        self.assertEqual(entry.to_dict()["cash_amount"], "3087.60")

    def test_bonus_and_transfer_shares(self) -> None:
        entry = build_ledger_entry(
            holding(shares="100"),
            event(dividend_per_share="10", per_share_bonus="0.1", per_share_transfer="0.2"),
        )
        self.assertEqual(entry.bonus_shares, Decimal("10.0"))
        self.assertEqual(entry.transfer_shares, Decimal("20.0"))

    def test_yield_on_cost(self) -> None:
        # 1000 / 100000 = 0.01
        entry = build_ledger_entry(
            holding(shares="100", cost_per_share="1000"),
            event(dividend_per_share="10", close_prev="500"),
        )
        self.assertEqual(entry.yield_on_cost, Decimal("0.01"))

    def test_dividend_yield(self) -> None:
        # 10 / 500 = 0.02
        entry = build_ledger_entry(holding(), event(dividend_per_share="10", close_prev="500"))
        self.assertEqual(entry.dividend_yield, Decimal("0.02"))

    def test_missing_cost_yields_none_not_zero(self) -> None:
        """缺成本 → None。用 0 冒充会让人以为"没赚到钱"。"""
        entry = build_ledger_entry(
            holding(cost_per_share=None), event(dividend_per_share="10")
        )
        self.assertIsNone(entry.yield_on_cost)
        self.assertIn(MISSING_COST_PER_SHARE, entry.missing_inputs)
        self.assertIsNone(entry.to_dict()["yield_on_cost"])

    def test_missing_close_prev_yields_none_not_zero(self) -> None:
        entry = build_ledger_entry(holding(), event(dividend_per_share="10"))
        self.assertIsNone(entry.dividend_yield)
        self.assertIn(MISSING_CLOSE_PREV, entry.missing_inputs)

    def test_zero_cost_treated_as_missing(self) -> None:
        entry = build_ledger_entry(
            holding(cost_per_share="0"), event(dividend_per_share="10")
        )
        self.assertIsNone(entry.yield_on_cost)

    def test_zero_close_prev_treated_as_missing(self) -> None:
        entry = build_ledger_entry(holding(), event(close_prev="0"))
        self.assertIsNone(entry.dividend_yield)

    def test_both_missing_recorded_together(self) -> None:
        entry = build_ledger_entry(
            holding(cost_per_share=None), event(dividend_per_share="10", close_prev=None)
        )
        self.assertEqual(
            set(entry.missing_inputs), {MISSING_COST_PER_SHARE, MISSING_CLOSE_PREV}
        )

    def test_tax_reference_is_none_in_phase_one(self) -> None:
        """税率为可配置规则位且默认不启用，所以参考净额保持 None。"""
        entry = build_ledger_entry(holding(), event())
        self.assertIsNone(entry.tax_reference)

    def test_shares_constant_during_holding(self) -> None:
        """一阶段假设股数恒定：事件用到的股数就是持仓股数。"""
        entry = build_ledger_entry(holding(shares="200"), event())
        self.assertEqual(entry.shares_at_event, Decimal("200"))


class BuildLedgerTest(unittest.TestCase):
    def test_single_stock_two_years(self) -> None:
        ledger = build_ledger(
            [holding(start_date="2020-01-01", shares="100")],
            [
                event(ex_date="2022-06-17", dividend_per_share="25"),
                event(ex_date="2024-06-19", dividend_per_share="30"),
            ],
        )
        self.assertEqual(len(ledger.entries), 2)
        self.assertEqual(sum(e.cash_amount for e in ledger.entries), Decimal("5500"))

    def test_excluded_events_surfaced(self) -> None:
        ledger = build_ledger(
            [holding(start_date="2024-06-19")],
            [event(ex_date="2020-01-01"), event(ex_date="2024-06-19")],
        )
        self.assertEqual(len(ledger.entries), 1)
        self.assertEqual(len(ledger.excluded), 1)
        self.assertEqual(ledger.excluded[0].ex_date, date(2020, 1, 1))

    def test_date_basis_counts(self) -> None:
        ledger = build_ledger(
            [holding(start_date="2020-01-01")],
            [
                event(ex_date="2024-06-19"),
                event(
                    ex_date="2023-06-16",
                    date_basis="pay_date_override",
                    pay_date="2023-06-23",
                ),
            ],
        )
        self.assertEqual(
            ledger.date_basis_counts, {"ex_date": 1, "pay_date_override": 1}
        )

    def test_missing_inputs_deduplicated_per_kind(self) -> None:
        """同一只股票缺成本，不应在每笔事件上重复报一次。"""
        ledger = build_ledger(
            [holding(start_date="2020-01-01", cost_per_share=None)],
            [
                event(ex_date="2022-06-17"),
                event(ex_date="2023-06-16"),
                event(ex_date="2024-06-19"),
            ],
        )
        kinds = [item.kind for item in ledger.missing_inputs]
        self.assertEqual(kinds.count(MISSING_COST_PER_SHARE), 1)
        self.assertEqual(
            ledger.missing_inputs[0].label(), "600519.SH.cost_per_share"
        )

    def test_price_missing_reported_once_per_stock(self) -> None:
        ledger = build_ledger(
            [holding(start_date="2020-01-01")],
            [
                event(ex_date="2022-06-17", close_prev=None),
                event(ex_date="2023-06-16", close_prev=None),
            ],
        )
        kinds = [item.kind for item in ledger.missing_inputs]
        self.assertEqual(kinds.count(MISSING_CLOSE_PREV), 1)

    def test_duplicate_holding_code_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_ledger(
                [holding(), holding(shares="200")],
                [event()],
            )

    def test_entries_sorted_by_effective_date(self) -> None:
        ledger = build_ledger(
            [
                holding(code="600519.SH", start_date="2020-01-01"),
                holding(code="000001.SZ", start_date="2020-01-01"),
            ],
            [
                event(code="600519.SH", ex_date="2024-06-19"),
                event(code="000001.SZ", ex_date="2022-06-10"),
            ],
        )
        self.assertEqual(
            [e.effective_date for e in ledger.entries],
            [date(2022, 6, 10), date(2024, 6, 19)],
        )

    def test_effective_date_uses_override(self) -> None:
        ledger = build_ledger(
            [holding(start_date="2020-01-01")],
            [
                event(
                    ex_date="2024-06-19",
                    date_basis="pay_date_override",
                    pay_date="2024-06-26",
                )
            ],
        )
        entry = ledger.entries[0]
        self.assertEqual(entry.ex_date, date(2024, 6, 19))
        self.assertEqual(entry.effective_date, date(2024, 6, 26))

    def test_ledger_dict_shape(self) -> None:
        ledger = build_ledger([holding()], [event()])
        payload = ledger.to_dict()
        for key in ("format_version", "meta", "entries", "excluded_events",
                    "missing_inputs", "stats"):
            self.assertIn(key, payload)
        self.assertEqual(payload["stats"]["entries"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
