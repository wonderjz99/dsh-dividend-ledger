"""证券代码归一化测试（SPEC §6.1）。"""

from __future__ import annotations

import unittest

from dividend_ledger.codes import is_normalized, normalize_code, to_cli_code
from dividend_ledger.errors import CodeError


class NormalizeAcceptsTest(unittest.TestCase):
    """各种输入写法都应归一到同一结果。"""

    def test_bare_six_digits(self) -> None:
        self.assertEqual(normalize_code("600519"), "600519.SH")
        self.assertEqual(normalize_code("000001"), "000001.SZ")
        self.assertEqual(normalize_code("300750"), "300750.SZ")
        self.assertEqual(normalize_code("688111"), "688111.SH")

    def test_exchange_prefix_lower_and_upper(self) -> None:
        self.assertEqual(normalize_code("sh600519"), "600519.SH")
        self.assertEqual(normalize_code("SH600519"), "600519.SH")
        self.assertEqual(normalize_code("sz000001"), "000001.SZ")
        self.assertEqual(normalize_code("bj430139"), "430139.BJ")

    def test_prefix_with_separators(self) -> None:
        self.assertEqual(normalize_code("sh.600519"), "600519.SH")
        self.assertEqual(normalize_code("sh:600519"), "600519.SH")
        self.assertEqual(normalize_code("sh 600519"), "600519.SH")

    def test_suffix_lower_and_upper(self) -> None:
        self.assertEqual(normalize_code("600519.SH"), "600519.SH")
        self.assertEqual(normalize_code("600519.sh"), "600519.SH")
        self.assertEqual(normalize_code("000001.sz"), "000001.SZ")

    def test_exchange_aliases(self) -> None:
        self.assertEqual(normalize_code("600519.XSHG"), "600519.SH")
        self.assertEqual(normalize_code("000001.XSHE"), "000001.SZ")
        self.assertEqual(normalize_code("600519.SSE"), "600519.SH")

    def test_surrounding_whitespace_tolerated(self) -> None:
        self.assertEqual(normalize_code("  600519  "), "600519.SH")

    def test_prefix_and_suffix_agreeing(self) -> None:
        self.assertEqual(normalize_code("sh600519.SH"), "600519.SH")

    def test_beijing_prefixes(self) -> None:
        for raw in ("430139", "830799", "871981", "920819"):
            self.assertEqual(normalize_code(raw).split(".")[1], "BJ", raw)

    def test_shenzhen_fund_segment(self) -> None:
        self.assertEqual(normalize_code("159915"), "159915.SZ")

    def test_shanghai_fund_segment(self) -> None:
        self.assertEqual(normalize_code("510300"), "510300.SH")

    def test_idempotent(self) -> None:
        once = normalize_code("sh600519")
        self.assertEqual(normalize_code(once), once)

    def test_is_normalized(self) -> None:
        self.assertTrue(is_normalized("600519.SH"))
        self.assertFalse(is_normalized("sh600519"))


class ToCliCodeTest(unittest.TestCase):
    """规范代码 → CLI 前缀格式。

    实测：westock CLI **不接受** ``000001.SZ``，会报
    ``错误: 代码 "000001.SZ" 格式不支持；请改用 "sz000001"``。
    所以取数前必须转换。
    """

    def test_converts_all_exchanges(self) -> None:
        self.assertEqual(to_cli_code("600519.SH"), "sh600519")
        self.assertEqual(to_cli_code("000001.SZ"), "sz000001")
        self.assertEqual(to_cli_code("300750.SZ"), "sz300750")
        self.assertEqual(to_cli_code("430139.BJ"), "bj430139")

    def test_roundtrip_through_normalize(self) -> None:
        for canonical in ("600519.SH", "000001.SZ", "300750.SZ", "688111.SH"):
            with self.subTest(canonical):
                self.assertEqual(normalize_code(to_cli_code(canonical)), canonical)

    def test_rejects_non_canonical_input(self) -> None:
        for raw in ("600519", "sh600519", "600519.XX", ""):
            with self.assertRaises(CodeError, msg=raw):
                to_cli_code(raw)


class NormalizeRejectsTest(unittest.TestCase):
    """绝不静默推断：任何歧义或畸形输入都要报错。"""

    def test_explicit_exchange_conflicting_with_segment(self) -> None:
        # 000001 是平安银行（深市），000001.SH 是上证指数——完全是两个标的
        with self.assertRaises(CodeError) as ctx:
            normalize_code("000001.SH")
        self.assertIn("冲突", str(ctx.exception))
        with self.assertRaises(CodeError):
            normalize_code("600519.SZ")

    def test_prefix_and_suffix_conflicting(self) -> None:
        with self.assertRaises(CodeError) as ctx:
            normalize_code("sh000001.SZ")
        self.assertIn("不同交易所", str(ctx.exception))

    def test_wrong_digit_count(self) -> None:
        for raw in ("12345", "1234567", "", "60051"):
            with self.assertRaises(CodeError, msg=raw):
                normalize_code(raw)

    def test_non_numeric(self) -> None:
        for raw in ("abc", "60051a", "茅台", "600-519"):
            with self.assertRaises(CodeError, msg=raw):
                normalize_code(raw)

    def test_unknown_exchange_marker(self) -> None:
        for raw in ("600519.XX", "xx600519"):
            with self.assertRaises(CodeError, msg=raw):
                normalize_code(raw)

    def test_non_string_input(self) -> None:
        for raw in (600519, None, ["600519"]):
            with self.assertRaises(CodeError, msg=repr(raw)):
                normalize_code(raw)  # type: ignore[arg-type]

    def test_error_message_is_actionable(self) -> None:
        """报错必须告诉用户怎么改，而不是只说"非法"。"""
        with self.assertRaises(CodeError) as ctx:
            normalize_code("000001.SH")
        message = str(ctx.exception)
        self.assertIn("000001.SZ", message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
