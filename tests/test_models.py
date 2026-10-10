"""数据模型严格性测试（SPEC §3）。"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from dividend_ledger.errors import DataError
from dividend_ledger.models import (
    DateBasis,
    DividendEvent,
    DividendStatus,
    Holding,
    LedgerEntry,
    parse_date,
)


class ParseDateTest(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(parse_date("2024-06-19", field_name="d"), date(2024, 6, 19))
        self.assertEqual(parse_date(date(2024, 6, 19), field_name="d"), date(2024, 6, 19))

    def test_format_is_strict(self) -> None:
        """``2024-6-19`` 这种写法必须拒绝——不严格就会出现两种日期形态。"""
        for raw in ("2024-6-19", "20240619", "2024/06/19", "19-06-2024"):
            with self.assertRaises(DataError, msg=raw):
                parse_date(raw, field_name="d")

    def test_impossible_date_rejected(self) -> None:
        for raw in ("2024-02-30", "2023-02-29", "2024-13-01"):
            with self.assertRaises(DataError, msg=raw):
                parse_date(raw, field_name="d")

    def test_non_string_rejected(self) -> None:
        for raw in (None, 20240619, ["2024-06-19"]):
            with self.assertRaises(DataError, msg=repr(raw)):
                parse_date(raw, field_name="d")


class HoldingTest(unittest.TestCase):
    def test_minimal(self) -> None:
        holding = Holding.from_dict(
            {"code": "sh600519", "start_date": "2021-03-15", "shares": "100"}
        )
        self.assertEqual(holding.code, "600519.SH")
        self.assertIsNone(holding.cost_per_share)
        self.assertIsNone(holding.cost_basis)

    def test_cost_basis_computed(self) -> None:
        holding = Holding.from_dict(
            {
                "code": "600519.SH",
                "start_date": "2021-03-15",
                "shares": "100",
                "cost_per_share": "1950",
            }
        )
        self.assertEqual(holding.cost_basis, Decimal("195000"))

    def test_fractional_shares_allowed(self) -> None:
        """送股会产生碎股，不能只接受整数。"""
        holding = Holding.from_dict(
            {"code": "600519.SH", "start_date": "2021-03-15", "shares": "100.5"}
        )
        self.assertEqual(holding.shares, Decimal("100.5"))

    def test_required_fields(self) -> None:
        for missing in ("code", "start_date", "shares"):
            payload = {"code": "600519.SH", "start_date": "2021-03-15", "shares": "100"}
            del payload[missing]
            with self.assertRaises(DataError, msg=missing) as ctx:
                Holding.from_dict(payload)
            self.assertIn(missing, str(ctx.exception))

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(DataError) as ctx:
            Holding.from_dict(
                {
                    "code": "600519.SH",
                    "start_date": "2021-03-15",
                    "shares": "100",
                    "cost": "1950",  # 拼错成 cost
                }
            )
        self.assertIn("未知字段", str(ctx.exception))

    def test_negative_shares_and_cost_rejected(self) -> None:
        with self.assertRaises(DataError):
            Holding.from_dict(
                {"code": "600519.SH", "start_date": "2021-03-15", "shares": "-1"}
            )
        with self.assertRaises(DataError):
            Holding.from_dict(
                {
                    "code": "600519.SH",
                    "start_date": "2021-03-15",
                    "shares": "1",
                    "cost_per_share": "-1",
                }
            )

    def test_float_shares_rejected(self) -> None:
        with self.assertRaises(DataError):
            Holding.from_dict(
                {"code": "600519.SH", "start_date": "2021-03-15", "shares": 100.0}
            )

    def test_roundtrip(self) -> None:
        payload = {
            "code": "600519.SH",
            "start_date": "2021-03-15",
            "shares": "100",
            "cost_per_share": "1950",
            "name": "虚构白酒",
            "account": "test-account-a",
        }
        holding = Holding.from_dict(payload)
        # 价格口径恒 2 位小数
        self.assertEqual(
            holding.to_dict(),
            {**payload, "cost_per_share": "1950.00"},
        )
        self.assertEqual(Holding.from_dict(holding.to_dict()), holding)

    def test_immutable(self) -> None:
        holding = Holding.from_dict(
            {"code": "600519.SH", "start_date": "2021-03-15", "shares": "100"}
        )
        with self.assertRaises(Exception):
            holding.shares = Decimal("200")  # type: ignore[misc]

    def test_empty_optional_string_becomes_none(self) -> None:
        holding = Holding.from_dict(
            {
                "code": "600519.SH",
                "start_date": "2021-03-15",
                "shares": "100",
                "name": "   ",
            }
        )
        self.assertIsNone(holding.name)


class DividendEventTest(unittest.TestCase):
    def test_minimal_defaults_to_ex_date_basis(self) -> None:
        event = DividendEvent.from_dict(
            {"code": "600519.SH", "ex_date": "2024-06-19", "dividend_per_share": "30.876"}
        )
        self.assertIs(event.date_basis, DateBasis.EX_DATE)
        self.assertIsNone(event.pay_date)
        self.assertEqual(event.effective_date, date(2024, 6, 19))
        self.assertEqual(event.per_share_bonus, Decimal("0"))
        self.assertFalse(event.has_bonus)

    def test_natural_key(self) -> None:
        event = DividendEvent.from_dict(
            {"code": "sh600519", "ex_date": "2024-06-19", "dividend_per_share": "1"}
        )
        # 缺 proposal_sn 时退化为空串，保证一阶段 fixture 行为不变
        self.assertEqual(event.natural_key, ("600519.SH", "2024-06-19", ""))
        self.assertEqual(event.bonus_key, ("600519.SH", "2024-06-19", "", "bonus"))

    def test_same_ex_date_different_proposal_are_distinct(self) -> None:
        """实测发现：同一除权除息日可以有多条方案（普通分红 + 特殊分红）。

        宁德时代 2026-04-22 就是 21.78 元 + 47.79 元两条。若唯一键不含 proposal_sn，
        它们会被合并成一条，**直接丢掉一笔分红**。
        """
        first = DividendEvent.from_dict(
            {
                "code": "300750.SZ",
                "ex_date": "2026-04-22",
                "dividend_per_share": "2.178",
                "proposal_sn": "1",
            }
        )
        second = DividendEvent.from_dict(
            {
                "code": "300750.SZ",
                "ex_date": "2026-04-22",
                "dividend_per_share": "4.779",
                "proposal_sn": "2",
            }
        )
        self.assertNotEqual(first.natural_key, second.natural_key)
        self.assertEqual(first.natural_key, ("300750.SZ", "2026-04-22", "1"))
        self.assertEqual(second.natural_key, ("300750.SZ", "2026-04-22", "2"))

    def test_status_inferred_from_ex_date(self) -> None:
        """状态按除权除息日与今天的关系推断——真实数据里"方案实施"也会有未来日期。"""
        past = DividendEvent.from_dict(
            {"code": "600519.SH", "ex_date": "2020-06-01", "dividend_per_share": "1"}
        )
        future = DividendEvent.from_dict(
            {"code": "600519.SH", "ex_date": "2099-06-01", "dividend_per_share": "1"}
        )
        self.assertIs(past.status, DividendStatus.IMPLEMENTED)
        self.assertIs(future.status, DividendStatus.CONFIRMED)
        self.assertFalse(past.is_future)
        self.assertTrue(future.is_future)

    def test_status_can_be_declared_explicitly(self) -> None:
        event = DividendEvent.from_dict(
            {
                "code": "600519.SH",
                "ex_date": "2099-06-01",
                "dividend_per_share": "1",
                "status": "proposed",
            }
        )
        self.assertIs(event.status, DividendStatus.PROPOSED)

    def test_invalid_status_rejected(self) -> None:
        with self.assertRaises(DataError) as ctx:
            DividendEvent.from_dict(
                {
                    "code": "600519.SH",
                    "ex_date": "2024-06-19",
                    "dividend_per_share": "1",
                    "status": "maybe",
                }
            )
        self.assertIn("status", str(ctx.exception))

    def test_basis_must_match_pay_date(self) -> None:
        """``date_basis`` 与 ``pay_date`` 不自洽会让报告的归集依据撒谎。"""
        with self.assertRaises(DataError) as ctx:
            DividendEvent.from_dict(
                {
                    "code": "600519.SH",
                    "ex_date": "2024-06-19",
                    "dividend_per_share": "1",
                    "date_basis": "pay_date_declared",
                }
            )
        self.assertIn("pay_date", str(ctx.exception))

    def test_pay_date_basis_used_for_effective_date(self) -> None:
        event = DividendEvent.from_dict(
            {
                "code": "600519.SH",
                "ex_date": "2024-06-19",
                "dividend_per_share": "1",
                "date_basis": "pay_date_declared",
                "pay_date": "2024-06-26",
            }
        )
        self.assertEqual(event.effective_date, date(2024, 6, 26))

    def test_invalid_basis_rejected(self) -> None:
        with self.assertRaises(DataError) as ctx:
            DividendEvent.from_dict(
                {
                    "code": "600519.SH",
                    "ex_date": "2024-06-19",
                    "dividend_per_share": "1",
                    "date_basis": "guessed",
                }
            )
        self.assertIn("date_basis", str(ctx.exception))

    def test_required_fields(self) -> None:
        for missing in ("code", "ex_date", "dividend_per_share"):
            payload = {
                "code": "600519.SH",
                "ex_date": "2024-06-19",
                "dividend_per_share": "1",
            }
            del payload[missing]
            with self.assertRaises(DataError, msg=missing):
                DividendEvent.from_dict(payload)

    def test_source_ref_must_be_object(self) -> None:
        with self.assertRaises(DataError):
            DividendEvent.from_dict(
                {
                    "code": "600519.SH",
                    "ex_date": "2024-06-19",
                    "dividend_per_share": "1",
                    "source_ref": "nope",
                }
            )

    def test_roundtrip_keeps_all_fields(self) -> None:
        payload = {
            "code": "600519.SH",
            "ex_date": "2024-06-19",
            "dividend_per_share": "30.876",
            "date_basis": "ex_date",
            "source": "westock",
            "per_share_bonus": "0.1",
            "per_share_transfer": "0.2",
            "right_reg_date": "2024-06-18",
            "close_prev": "1500",
            "plan_text": "10派308.76元送1股",
        }
        event = DividendEvent.from_dict(payload)
        # 每股口径恒 6 位小数、价格口径恒 2 位小数；status 总是输出
        self.assertEqual(
            event.to_dict(),
            {
                **payload,
                "dividend_per_share": "30.876000",
                "per_share_bonus": "0.100000",
                "per_share_transfer": "0.200000",
                "close_prev": "1500.00",
                "status": "implemented",
            },
        )
        self.assertEqual(DividendEvent.from_dict(event.to_dict()), event)

    def test_json_output_uses_strings_not_floats(self) -> None:
        event = DividendEvent.from_dict(
            {
                "code": "600519.SH",
                "ex_date": "2024-06-19",
                "dividend_per_share": "30.876",
                "close_prev": "1500",
                "per_share_bonus": "0.1",
            }
        )
        payload = event.to_dict()
        for key in ("dividend_per_share", "close_prev", "per_share_bonus"):
            self.assertIsInstance(payload[key], str, key)


class LedgerEntryTest(unittest.TestCase):
    def _entry(self, **overrides) -> LedgerEntry:
        base = dict(
            code="600519.SH",
            name="虚构白酒",
            ex_date=date(2024, 6, 19),
            effective_date=date(2024, 6, 19),
            date_basis=DateBasis.EX_DATE,
            shares_at_event=Decimal("100"),
            dividend_per_share=Decimal("30.876"),
            cash_amount=Decimal("3087.60"),
            bonus_shares=Decimal("10"),
            transfer_shares=Decimal("0"),
            cost_basis=Decimal("195000"),
            yield_on_cost=Decimal("0.015833"),
            dividend_yield=Decimal("0.020584"),
            tax_reference=None,
            source="fixture",
        )
        base.update(overrides)
        return LedgerEntry(**base)  # type: ignore[arg-type]

    def test_shares_serialised_as_integer_when_whole(self) -> None:
        payload = self._entry().to_dict()
        self.assertEqual(payload["shares_at_event"], "100")
        self.assertEqual(payload["bonus_shares"], "10")

    def test_none_ratios_stay_null(self) -> None:
        payload = self._entry(yield_on_cost=None, dividend_yield=None).to_dict()
        self.assertIsNone(payload["yield_on_cost"])
        self.assertIsNone(payload["dividend_yield"])
        self.assertIsNone(payload["tax_reference"])

    def test_missing_inputs_listed(self) -> None:
        payload = self._entry(missing_inputs=("cost_per_share",)).to_dict()
        self.assertEqual(payload["missing_inputs"], ["cost_per_share"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
