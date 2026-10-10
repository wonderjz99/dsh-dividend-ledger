"""Decimal 精度与舍入测试（SPEC §4.5）。"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal

from dividend_ledger import decimals as d
from dividend_ledger.errors import DecimalError


class ParseTest(unittest.TestCase):
    def test_string_is_preferred(self) -> None:
        self.assertEqual(d.parse_decimal("25.911", field="x"), Decimal("25.911"))
        self.assertEqual(d.parse_decimal("  25.911  ", field="x"), Decimal("25.911"))
        self.assertEqual(d.parse_decimal("0", field="x"), Decimal("0"))

    def test_decimal_and_int_accepted(self) -> None:
        self.assertEqual(d.parse_decimal(Decimal("1.5"), field="x"), Decimal("1.5"))
        self.assertEqual(d.parse_decimal(3, field="x"), Decimal(3))

    def test_float_is_rejected(self) -> None:
        """浮点数是账本的头号精度污染源，必须挡在门外。"""
        with self.assertRaises(DecimalError) as ctx:
            d.parse_decimal(0.1, field="amount")
        self.assertIn("浮点数", str(ctx.exception))

    def test_bool_is_rejected(self) -> None:
        with self.assertRaises(DecimalError):
            d.parse_decimal(True, field="x")

    def test_garbage_rejected(self) -> None:
        for raw in ("", "  ", "abc", "1.2.3", "--", "1,200"):
            with self.assertRaises(DecimalError, msg=raw):
                d.parse_decimal(raw, field="x")

    def test_non_finite_rejected(self) -> None:
        for raw in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaises(DecimalError, msg=raw):
                d.parse_decimal(raw, field="x")

    def test_unsupported_type_rejected(self) -> None:
        for raw in (None, [], {}, object()):
            with self.assertRaises(DecimalError, msg=repr(raw)):
                d.parse_decimal(raw, field="x")


class ParseIntTest(unittest.TestCase):
    def test_accepts_integral(self) -> None:
        self.assertEqual(d.parse_int("2024", field="year"), 2024)
        self.assertEqual(d.parse_int(0, field="n"), 0)

    def test_rejects_fraction_and_negative(self) -> None:
        with self.assertRaises(DecimalError):
            d.parse_int("2024.5", field="year")
        with self.assertRaises(DecimalError):
            d.parse_int("-1", field="n")


class FormatTest(unittest.TestCase):
    def test_money_always_two_decimals(self) -> None:
        self.assertEqual(d.format_money(Decimal("1200")), "1200.00")
        self.assertEqual(d.format_money(Decimal("0")), "0.00")
        self.assertEqual(d.format_money(Decimal("-3.5")), "-3.50")

    def test_per_share_always_six_decimals(self) -> None:
        self.assertEqual(d.format_per_share(Decimal("0.1")), "0.100000")
        self.assertEqual(d.format_per_share(Decimal("0")), "0.000000")

    def test_price_two_decimals(self) -> None:
        self.assertEqual(d.format_price(Decimal("380")), "380.00")
        self.assertEqual(d.format_price(Decimal("8.8")), "8.80")

    def test_shares_trims_trailing_zeros(self) -> None:
        self.assertEqual(d.format_shares(Decimal("40.000000")), "40")
        self.assertEqual(d.format_shares(Decimal("100")), "100")
        self.assertEqual(d.format_shares(Decimal("0.000000")), "0")

    def test_ratio_always_six_decimals(self) -> None:
        self.assertEqual(d.format_ratio(Decimal("0.0556494")), "0.055649")

    def test_no_scientific_notation(self) -> None:
        """``1E-6`` 这种写法会让下游解析者踩坑，必须展开。"""
        self.assertEqual(d.format_per_share(Decimal("0.000001")), "0.000001")
        self.assertNotIn("E", d.format_per_share(Decimal("0.000001")))
        self.assertNotIn("e", d.format_ratio(Decimal("0.000001")))

    def test_negative_zero_normalised(self) -> None:
        self.assertEqual(d.format_money(Decimal("-0")), "0.00")
        self.assertEqual(d.format_shares(Decimal("-0")), "0")


class RoundingTest(unittest.TestCase):
    """银行家舍入（ROUND_HALF_EVEN）：.005 向偶数靠。"""

    def test_banker_rounding(self) -> None:
        self.assertEqual(d.format_money(Decimal("0.005")), "0.00")
        self.assertEqual(d.format_money(Decimal("0.015")), "0.02")
        self.assertEqual(d.format_money(Decimal("0.025")), "0.02")
        self.assertEqual(d.format_money(Decimal("0.035")), "0.04")

    def test_rounding_applied_once_at_output(self) -> None:
        """十条 0.005 相加：先舍入会得 0，只在输出舍入才得 0.05。"""
        total = d.add_all([Decimal("0.005")] * 10)
        self.assertEqual(total, Decimal("0.050"))
        self.assertEqual(d.format_money(total), "0.05")


class ArithmeticTest(unittest.TestCase):
    def test_decimal_addition_is_exact(self) -> None:
        """0.1 + 0.2 用 Decimal 必须精确等于 0.3——这正是拒绝 float 的理由。"""
        total = d.add_all([Decimal("0.1"), Decimal("0.2")])
        self.assertEqual(total, Decimal("0.3"))
        self.assertEqual(d.format_money(total), "0.30")

    def test_divide_ratio_rejects_zero_denominator(self) -> None:
        """分母为 0 必须报错，由调用方转成 None，而不是悄悄返回 0。"""
        with self.assertRaises(DecimalError):
            d.divide_ratio(Decimal("1"), Decimal("0"))

    def test_no_float_appears_in_json(self) -> None:
        """JSON 输出里不允许出现浮点字面量。"""
        payload = {
            "amount": d.format_money(Decimal("26856.4")),
            "per_share": d.format_per_share(Decimal("21.675")),
            "ratio": d.format_ratio(Decimal("0.055649")),
        }
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(".0,", text)
        self.assertEqual(json.loads(text)["amount"], "26856.40")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
