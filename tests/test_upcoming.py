"""待收分红测试（阶段三）。

这个模块盯的核心只有一件事：**待收绝不进历史累计**。

因为这条一旦破了，"我已经收了多少"就失真了——而且失真得非常隐蔽：
历史合计只是"变大了一点"，不会报错、不会崩溃，只有对着数字才发现不对。
实测就发生过：未实施方案被算进 `totals.cash_amount`，
26856.40 被算成 33047.50。

其余盯：归月依据、空态、送转假设、排序可复现。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from dividend_ledger.ledger import build_ledger, filter_events_for_holding
from dividend_ledger.models import DividendEvent, DividendStatus, Holding
from dividend_ledger.report import ReportContext, build_report
from dividend_ledger.upcoming import build_upcoming

TODAY = date(2026, 10, 10)


def holding(**overrides) -> Holding:
    base = {
        "code": "600519.SH",
        "name": "贵州茅台",
        "start_date": "2020-01-01",
        "shares": "100",
        "cost_per_share": "100",
    }
    base.update(overrides)
    return Holding.from_dict(base)


def event(**overrides) -> DividendEvent:
    base = {
        "code": "600519.SH",
        "ex_date": "2026-06-19",
        "dividend_per_share": "10",
    }
    base.update(overrides)
    return DividendEvent.from_dict(base)


class NoContaminationTest(unittest.TestCase):
    """阶段三的铁律：待收金额**绝不**进历史累计。"""

    def test_future_events_excluded_from_ledger(self) -> None:
        ledger = build_ledger(
            [holding()],
            [
                event(ex_date="2026-06-19", dividend_per_share="10"),
                event(
                    ex_date="2027-06-19",
                    dividend_per_share="25",
                    status="confirmed",
                    proposal_sn="1",
                ),
            ],
        )
        self.assertEqual(len(ledger.entries), 1)
        self.assertEqual(sum(e.cash_amount for e in ledger.entries), Decimal("1000"))

    def test_proposed_also_excluded(self) -> None:
        ledger = build_ledger(
            [holding()],
            [event(ex_date="2027-06-19", dividend_per_share="25", status="proposed")],
        )
        self.assertEqual(ledger.entries, ())

    def test_totals_do_not_include_upcoming(self) -> None:
        """端到端：报告的历史合计必须与"只有已实施事件"时完全一致。"""
        events = [
            event(ex_date="2026-06-19", dividend_per_share="10"),
            event(
                ex_date="2027-06-19",
                dividend_per_share="25",
                status="confirmed",
                proposal_sn="1",
            ),
            event(
                ex_date="2027-09-19",
                dividend_per_share="5",
                status="proposed",
                proposal_sn="2",
            ),
        ]
        holdings = [holding()]

        with_upcoming = build_report(
            build_ledger(holdings, events),
            context=ReportContext(
                generated_at="2026-10-10T00:00:00+08:00",
                inputs={},
                upcoming=build_upcoming(events, holdings),
            ),
        )
        only_implemented = build_report(
            build_ledger(holdings, [events[0]]),
            context=ReportContext(generated_at="2026-10-10T00:00:00+08:00", inputs={}),
        )

        self.assertEqual(
            with_upcoming["totals"]["cash_amount"],
            only_implemented["totals"]["cash_amount"],
            "有待收时历史合计必须与无待收时完全相同",
        )
        self.assertEqual(with_upcoming["totals"]["cash_amount"], "1000.00")
        # 而待收确实被算出来了，只是单独放
        self.assertEqual(with_upcoming["upcoming"]["confirmedAmount"], "2500.00")
        self.assertEqual(with_upcoming["upcoming"]["proposedAmount"], "500.00")

    def test_implemented_status_explicit_is_history(self) -> None:
        """显式写 implemented 的进历史。"""
        ledger = build_ledger(
            [holding()], [event(ex_date="2026-06-19", status="implemented")]
        )
        self.assertEqual(len(ledger.entries), 1)

    def test_filter_skips_future_without_listing_as_excluded(self) -> None:
        """待收是"被另一个区块接管"，不是"被丢弃"——不该进 excluded。"""
        included, excluded = filter_events_for_holding(
            holding(),
            [event(ex_date="2027-06-19", status="confirmed")],
        )
        self.assertEqual(included, [])
        self.assertEqual(excluded, [])


class BuildUpcomingTest(unittest.TestCase):
    def test_confirmed_amount_is_per_share_times_shares(self) -> None:
        summary = build_upcoming(
            [event(ex_date="2027-06-19", dividend_per_share="25.911", status="confirmed")],
            [holding(shares="100")],
        )
        self.assertEqual(summary.confirmed_amount, Decimal("2591.10"))
        self.assertEqual(summary.proposed_amount, Decimal("0"))
        self.assertEqual(len(summary.rows), 1)

    def test_proposed_counted_separately(self) -> None:
        summary = build_upcoming(
            [
                event(
                    ex_date="2027-06-19",
                    dividend_per_share="10",
                    status="confirmed",
                    proposal_sn="1",
                ),
                event(
                    ex_date="2027-06-19",
                    dividend_per_share="2",
                    status="proposed",
                    proposal_sn="2",
                ),
            ],
            [holding()],
        )
        self.assertEqual(summary.confirmed_amount, Decimal("1000"))
        self.assertEqual(summary.proposed_amount, Decimal("200"))
        self.assertEqual(summary.total_amount, Decimal("1200"))

    def test_implemented_never_appears(self) -> None:
        summary = build_upcoming([event(status="implemented")], [holding()])
        self.assertTrue(summary.is_empty)
        self.assertEqual(summary.total_amount, Decimal("0"))

    def test_unheld_stock_excluded(self) -> None:
        """没持有就没有"待收"——不能凭空列出别人的分红。"""
        summary = build_upcoming(
            [
                event(
                    code="000858.SZ",
                    ex_date="2027-06-19",
                    status="confirmed",
                )
            ],
            [holding(code="600519.SH")],
        )
        self.assertTrue(summary.is_empty)

    def test_empty_is_empty_rows_not_error(self) -> None:
        summary = build_upcoming([], [holding()])
        payload = summary.to_dict()
        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["byMonth"], [])
        self.assertEqual(payload["byStock"], [])
        self.assertEqual(payload["confirmedAmount"], "0.00")

    def test_two_plans_same_ex_date_kept_separate(self) -> None:
        """同一除权日的普通 + 特殊分红要分别列出、金额相加而不是合并。"""
        summary = build_upcoming(
            [
                event(
                    ex_date="2027-04-22",
                    dividend_per_share="2.178",
                    status="confirmed",
                    proposal_sn="1",
                ),
                event(
                    ex_date="2027-04-22",
                    dividend_per_share="4.779",
                    status="confirmed",
                    proposal_sn="2",
                ),
            ],
            [holding(shares="100")],
        )
        self.assertEqual(len(summary.rows), 2)
        self.assertEqual(summary.confirmed_amount, Decimal("695.70"))

    def test_sorted_for_reproducibility(self) -> None:
        summary = build_upcoming(
            [
                event(
                    code="600519.SH",
                    ex_date="2027-09-01",
                    status="confirmed",
                    proposal_sn="1",
                ),
                event(
                    code="000001.SZ",
                    ex_date="2027-06-01",
                    status="confirmed",
                    proposal_sn="2",
                ),
            ],
            [holding(code="600519.SH"), holding(code="000001.SZ", shares="50")],
        )
        self.assertEqual([r.month for r in summary.rows], ["2027-06", "2027-09"])

    def test_multiplication_is_decimal_exact(self) -> None:
        """0.1 × 3 在浮点下是 0.30000000000000004；十进制必须精确。"""
        summary = build_upcoming(
            [event(ex_date="2027-06-19", dividend_per_share="0.1", status="confirmed")],
            [holding(shares="3")],
        )
        self.assertEqual(summary.confirmed_amount, Decimal("0.3"))
        self.assertEqual(summary.to_dict()["confirmedAmount"], "0.30")


class MonthBasisTest(unittest.TestCase):
    def test_default_basis_is_ex_date(self) -> None:
        summary = build_upcoming(
            [event(ex_date="2027-06-19", status="confirmed")], [holding()]
        )
        row = summary.rows[0]
        self.assertEqual(row.month, "2027-06")
        self.assertEqual(row.month_basis, "ex_date")
        self.assertIsNone(row.pay_date)

    def test_pay_date_wins_when_known(self) -> None:
        """有到账日（人工覆盖）时按到账月归集——那才是"钱哪个月到"。"""
        summary = build_upcoming(
            [
                event(
                    ex_date="2027-06-19",
                    pay_date="2027-07-05",
                    status="confirmed",
                )
            ],
            [holding()],
        )
        row = summary.rows[0]
        self.assertEqual(row.month, "2027-07")
        self.assertEqual(row.month_basis, "pay_date")
        self.assertTrue(row.to_dict()["payDateKnown"])


class SerialisationTest(unittest.TestCase):
    def test_all_money_is_strings(self) -> None:
        summary = build_upcoming(
            [event(ex_date="2027-06-19", dividend_per_share="1.5", status="confirmed")],
            [holding(shares="100")],
        )
        payload = summary.to_dict()
        for key in ("confirmedAmount", "proposedAmount", "totalAmount"):
            self.assertIsInstance(payload[key], str, key)
        row = payload["rows"][0]
        for key in ("perShare", "shares", "cashAmount"):
            self.assertIsInstance(row[key], str, key)
        self.assertIsInstance(row["certain"], bool)

    def test_by_month_groups_and_sums(self) -> None:
        summary = build_upcoming(
            [
                event(
                    ex_date="2027-06-19",
                    dividend_per_share="10",
                    status="confirmed",
                    proposal_sn="1",
                ),
                event(
                    ex_date="2027-06-25",
                    dividend_per_share="3",
                    status="proposed",
                    proposal_sn="2",
                ),
                event(ex_date="2027-09-01", dividend_per_share="1", status="confirmed"),
            ],
            [holding(shares="100")],
        )
        months = summary.by_month()
        self.assertEqual([m["month"] for m in months], ["2027-06", "2027-09"])
        june = months[0]
        self.assertEqual(june["cashAmount"], "1300.00")
        self.assertEqual(june["confirmedAmount"], "1000.00")
        self.assertEqual(june["proposedAmount"], "300.00")
        self.assertEqual(june["eventCount"], 2)

    def test_format_version_stable(self) -> None:
        """upcoming 的输出要能在两次运行间逐字节一致。"""
        events = [
            event(
                ex_date="2027-06-19",
                dividend_per_share="10",
                status="confirmed",
                proposal_sn="1",
            )
        ]
        first = json.dumps(build_upcoming(events, [holding()]).to_dict())
        second = json.dumps(build_upcoming(events, [holding()]).to_dict())
        self.assertEqual(first, second)


class SharesAssumptionTest(unittest.TestCase):
    def test_uses_current_shares_no_projection(self) -> None:
        """待收 = 每股派息 × **当前**股数。一阶段假设持有期内股数恒定，
        所以**不做**送转推算——这条把假设固定在测试里。"""
        summary = build_upcoming(
            [
                event(
                    ex_date="2027-06-19",
                    dividend_per_share="10",
                    status="confirmed",
                    per_share_bonus="0.5",  # 送股也不改变待收股数
                )
            ],
            [holding(shares="100")],
        )
        row = summary.rows[0]
        self.assertEqual(row.shares, Decimal("100"))
        self.assertEqual(row.cash_amount, Decimal("1000"))

    def test_shares_formatting_drops_trailing_zeros(self) -> None:
        summary = build_upcoming(
            [event(ex_date="2027-06-19", status="confirmed")],
            [holding(shares="100.000000")],
        )
        self.assertEqual(summary.to_dict()["rows"][0]["shares"], "100")


class StatusParsingTest(unittest.TestCase):
    def test_explicit_status_respected(self) -> None:
        for raw, expected in (
            ("implemented", DividendStatus.IMPLEMENTED),
            ("confirmed", DividendStatus.CONFIRMED),
            ("proposed", DividendStatus.PROPOSED),
        ):
            with self.subTest(raw=raw):
                self.assertIs(event(status=raw, ex_date="2027-06-19").status, expected)

    def test_future_date_without_status_infers_confirmed(self) -> None:
        """没给 status 时按日期推断：未来 → confirmed（fixture 靠这条）。"""
        self.assertIs(
            event(ex_date="2027-06-19").status, DividendStatus.CONFIRMED
        )

    def test_past_date_without_status_infers_implemented(self) -> None:
        self.assertIs(
            event(ex_date="2020-06-19").status, DividendStatus.IMPLEMENTED
        )

    def test_invalid_status_rejected(self) -> None:
        from dividend_ledger.errors import DataError

        with self.assertRaises(DataError) as ctx:
            event(status="也许吧", ex_date="2027-06-19")
        self.assertIn("status", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
