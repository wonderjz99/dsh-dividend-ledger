"""westock 解析器测试。

**最重要的三条**（都来自真实核对，见 ``docs/westock-verified-behaviour.md``）：

1. ``cashDiviRMB`` / ``bonusShareRatio`` / ``tranAddShareRatio`` 是"每 10 股"口径，
   漏掉 ÷10 会让所有金额差 10 倍。
2. 同一除权除息日**可以有多条**方案（普通分红 + 特殊分红），靠 ``proposalSn`` 区分；
   唯一键不含它就会合并丢数据。
3. **限频时 CLI 退出码仍是 0**，只把"请求过于频繁"写进 stdout。
   解析器必须报错，绝不能返回空列表——否则账本会静默少算分红。

载荷取自 ``data/raw/*.md``（真实 CLI 输出，已落盘），所以这些测试跑的是**真数据**。
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from dividend_ledger.errors import DataError, ProviderError
from dividend_ledger.models import DateBasis, DividendStatus
from dividend_ledger.providers.base import DividendProvider
from dividend_ledger.providers.westock import (
    EXPECTED_HEADER,
    KLINE_HEADER,
    SEARCH_HEADER,
    WESTOCK_SHARES_BASIS,
    WestockProvider,
    enrich_close_prev,
    make_cli_runner,
    make_kline_runner,
    make_search_runner,
    parse_dividend_markdown,
    parse_kline_markdown,
    parse_search_markdown,
    parse_stock_block,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"

MAOTAI = RAW_DIR / "dividend-sh600519.md"
PINGAN = RAW_DIR / "dividend-sz000001.md"
CATL = RAW_DIR / "dividend-sz300750.md"

KLINE_MAOTAI = RAW_DIR / "kline-sh600519.md"
KLINE_PINGAN = RAW_DIR / "kline-sz000001.md"
KLINE_CATL = RAW_DIR / "kline-sz300750.md"

#: 核对当天。固定它，测试才与"今天"无关。
TODAY = date(2026, 10, 10)


def load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse(path: Path, **kwargs):
    return parse_dividend_markdown(load(path), today=TODAY, **kwargs)


def build_block(rows: list[str]) -> str:
    """按真实表头拼一个最小 Markdown 载荷，用于构造边界行。"""
    header = "| " + " | ".join(EXPECTED_HEADER) + " |"
    separator = "| " + " | ".join(["---"] * len(EXPECTED_HEADER)) + " |"
    return "\n".join(["#### sh600519", "", "**分红历史**", "", header, separator, *rows])


class RealPayloadTest(unittest.TestCase):
    """真实载荷必须全部解析成功，且关键字段值正确。"""

    def test_all_raw_payloads_parse_without_rejection(self) -> None:
        for path in (MAOTAI, PINGAN, CATL):
            with self.subTest(path.name):
                result = parse(path)
                self.assertGreater(len(result.events), 0)
                self.assertEqual(tuple(result.rejected), (), f"{path.name} 不应有被拒行")

    def test_header_matches_expectation(self) -> None:
        """表头字段顺序锁死：字段错位比报错危险得多。"""
        self.assertEqual(
            EXPECTED_HEADER,
            (
                "bonusShareRatio",
                "cashDiviRMB",
                "dividendFlag",
                "dividendPlan",
                "dividendType",
                "exDiviDate",
                "procedure",
                "proposalSn",
                "reportEndDate",
                "rightRegDate",
                "totalCashDiviComRMB",
                "tranAddShareRatio",
            ),
        )

    def test_changed_header_is_rejected_not_silently_misparsed(self) -> None:
        tampered = load(MAOTAI).replace("| cashDiviRMB |", "| cashDividend |")
        with self.assertRaises(DataError) as ctx:
            parse_dividend_markdown(tampered, today=TODAY)
        self.assertIn("表头与预期不一致", str(ctx.exception))

    def test_code_comes_from_section_header(self) -> None:
        """行里没有代码字段，只能靠 `#### <代码>` 段头。"""
        self.assertTrue(all(e.code == "600519.SH" for e in parse(MAOTAI).events))
        self.assertTrue(all(e.code == "000001.SZ" for e in parse(PINGAN).events))
        self.assertTrue(all(e.code == "300750.SZ" for e in parse(CATL).events))

    def test_a_share_has_no_pay_date(self) -> None:
        """A 股字段集没有派息到账日 → 一律落在 ex_date 口径。"""
        for path in (MAOTAI, PINGAN, CATL):
            for event in parse(path).events:
                self.assertIsNone(event.pay_date)
                self.assertIs(DateBasis.EX_DATE, event.date_basis)
                self.assertEqual(event.effective_date, event.ex_date)

    def test_source_ref_keeps_raw_values(self) -> None:
        event = parse(MAOTAI).events[0]
        self.assertEqual(event.source, "westock")
        self.assertEqual(event.source_ref["shares_basis"], 10)
        self.assertIn("raw_cashDiviRMB", event.source_ref)
        self.assertIsNotNone(event.source_ref["procedure"])

    def test_multi_stock_payload_parses_both_sections(self) -> None:
        """多股票输出（逗号分隔查询）应能各自分段解析。"""
        combined = load(MAOTAI) + "\n" + load(PINGAN)
        result = parse_dividend_markdown(combined, today=TODAY)
        self.assertEqual({e.code for e in result.events}, {"600519.SH", "000001.SZ"})


class SharesBasisTest(unittest.TestCase):
    """量纲回归测试：防止"每 10 股"被当成"每股"。"""

    def test_basis_constant_is_ten(self) -> None:
        self.assertEqual(WESTOCK_SHARES_BASIS, Decimal(10))

    def test_cash_converted_from_per_ten_to_per_share(self) -> None:
        """真实行：`10派308.760元` → 每股 30.876 元（不是 308.76）。"""
        target = next(e for e in parse(MAOTAI).events if e.ex_date == date(2024, 6, 19))
        self.assertEqual(target.dividend_per_share, Decimal("30.876"))
        self.assertEqual(target.plan_text, "10派308.760元")

    def test_transfer_ratio_converted(self) -> None:
        """真实行：`10派25.200元转8.000股` → 每股派 2.52 + 每股转 0.8。"""
        target = next(e for e in parse(CATL).events if e.ex_date == date(2023, 4, 26))
        self.assertEqual(target.dividend_per_share, Decimal("2.52"))
        self.assertEqual(target.per_share_transfer, Decimal("0.8"))
        self.assertEqual(target.per_share_bonus, Decimal("0"))
        self.assertTrue(target.has_bonus)

    def test_reverse_engineered_share_count_matches_reality(self) -> None:
        """最强的一条验证：用总派现反推总股本，与真实股本吻合。

        ``totalCashDiviComRMB ÷ (cashDiviRMB ÷ 10)`` = 总股本。
        若 ``cashDiviRMB`` 是每股口径，反推结果会小 10 倍（差一个数量级）。
        """
        event = next(e for e in parse(MAOTAI).events if e.ex_date == date(2024, 6, 19))
        total = Decimal("38786363272.80")  # 真实输出里的 totalCashDiviComRMB
        shares = total / event.dividend_per_share
        # 茅台总股本 ≈ 12.56 亿股
        self.assertLess(abs(shares - Decimal("1256197800")), Decimal("1000"))

    def test_provider_scale_guard_rejects_unconverted_value(self) -> None:
        """若有人把"每 10 股"的值直接塞进每股字段，量纲体检必须报警。"""

        class Dummy(DividendProvider):
            name = "dummy"
            shares_basis = 10

            def list_events(self, codes, start, end):  # pragma: no cover
                raise NotImplementedError

        rows = [{"exDiviDate": "20240619", "cashDiviRMB": "20000"}]  # ÷10 后每股 2000 元
        events, _ = parse_stock_block(rows, code_hint="sh600519", today=TODAY)
        with self.assertRaises(ProviderError) as ctx:
            Dummy()._assert_per_share_scale(events[0])
        self.assertIn("口径", str(ctx.exception))


class SameDayMultiplePlansTest(unittest.TestCase):
    """同一除权日多条方案——真实数据里就存在，不能合并。"""

    def test_catl_has_two_plans_on_same_ex_date(self) -> None:
        by_date: dict[date, list] = {}
        for event in parse(CATL).events:
            by_date.setdefault(event.ex_date, []).append(event)

        for target in (date(2024, 4, 30), date(2026, 4, 22)):
            with self.subTest(str(target)):
                self.assertEqual(len(by_date[target]), 2, f"{target} 应保留两条方案")

        group = by_date[date(2026, 4, 22)]
        self.assertEqual(
            {e.proposal_sn for e in group}, {"1", "2"}, "靠 proposalSn 区分普通/特殊分红"
        )
        self.assertEqual(
            sorted(e.dividend_per_share for e in group),
            [Decimal("2.178"), Decimal("4.779")],
        )

    def test_sum_of_both_plans_is_preserved(self) -> None:
        """两条方案合计每股 6.957 元；合并成一条会丢掉其中一笔。"""
        group = [e for e in parse(CATL).events if e.ex_date == date(2026, 4, 22)]
        self.assertEqual(sum(e.dividend_per_share for e in group), Decimal("6.957"))

    def test_natural_keys_differ(self) -> None:
        group = [e for e in parse(CATL).events if e.ex_date == date(2026, 4, 22)]
        self.assertEqual(len({e.natural_key for e in group}), 2)

    def test_duplicate_row_with_same_proposal_sn_raises(self) -> None:
        """同一天 + 同一个 proposalSn 重复出现才是真重复，必须报错。"""
        row = (
            "|  | 308.76 | 是 | 10派308.760元 | 有分红 | 20240619 | 方案实施 | 1 "
            "| 20231231 | 20240618 | 38786363272.80 |  |"
        )
        with self.assertRaises(ProviderError) as ctx:
            parse_dividend_markdown(build_block([row, row]), today=TODAY)
        self.assertIn("出现多次", str(ctx.exception))


class RateLimitTest(unittest.TestCase):
    """限频必须报错——这是最危险的静默失败。"""

    def test_rate_limit_message_raises(self) -> None:
        for text in (
            "请求过于频繁，请稍后再试",
            "请求过于频繁，请稍后再试\n",
            "please retry: too many requests",
            "Rate limit exceeded",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ProviderError) as ctx:
                    parse_dividend_markdown(text, today=TODAY)
                self.assertIn("限频", str(ctx.exception))

    def test_rate_limit_never_returns_empty_list(self) -> None:
        """核心要求：绝不能把限频当成"这只股票没有分红"。"""
        with self.assertRaises(ProviderError):
            parse_dividend_markdown("请求过于频繁，请稍后再试", today=TODAY)

    def test_empty_output_raises(self) -> None:
        for text in ("", "   \n  "):
            with self.assertRaises(ProviderError):
                parse_dividend_markdown(text, today=TODAY)

    def test_section_without_table_returns_empty_not_error(self) -> None:
        """有段头但没表格 = 这只股票没有分红数据，是**空结果**而不是解析失败。"""
        result = parse_dividend_markdown(
            "#### sh600519\n\n**分红历史**\n\n（无数据）", today=TODAY
        )
        self.assertEqual(tuple(result.events), ())
        self.assertEqual(result.provenance["stocks_in_payload"], 1)

    def test_payload_without_any_section_header_raises(self) -> None:
        with self.assertRaises(DataError) as ctx:
            parse_dividend_markdown("随便一段文字，没有表格也没有段头", today=TODAY)
        self.assertIn("分红表格", str(ctx.exception))



class StatusTest(unittest.TestCase):
    """状态由 procedure 文本 + 除权日与今天的关系共同决定。"""

    def test_all_real_rows_are_implemented_because_dates_are_past(self) -> None:
        for path in (MAOTAI, PINGAN, CATL):
            for event in parse(path).events:
                with self.subTest(f"{path.name} {event.ex_date}"):
                    self.assertIs(DividendStatus.IMPLEMENTED, event.status)
                    self.assertLessEqual(event.ex_date, TODAY)

    def test_future_ex_date_becomes_confirmed(self) -> None:
        """`方案实施` + 未来除权日 = 已公告未实施，即"未来预期分红"的来源。"""
        row = (
            "|  | 300.00 | 是 | 10派300.000元 | 有分红 | 20261231 | 方案实施 | 1 "
            "| 20251231 | 20261230 | 1.00 |  |"
        )
        event = parse_dividend_markdown(build_block([row]), today=TODAY).events[0]
        self.assertIs(DividendStatus.CONFIRMED, event.status)
        self.assertTrue(event.is_future)

    def test_proposal_keyword_becomes_proposed(self) -> None:
        row = (
            "|  | 300.00 | 否 | 10派300.000元（预案） | 有分红 | 20261231 | 董事会预案 | 1 "
            "| 20251231 |  | 1.00 |  |"
        )
        event = parse_dividend_markdown(build_block([row]), today=TODAY).events[0]
        self.assertIs(DividendStatus.PROPOSED, event.status)

    def test_implemented_row_kept_not_excluded(self) -> None:
        """`方案实施` 不能被当成"未实施"排除——这是真实取值。"""
        self.assertTrue(
            all(e.status is DividendStatus.IMPLEMENTED for e in parse(MAOTAI).events)
        )


class EdgeCaseTest(unittest.TestCase):
    def test_missing_ex_date_skipped_with_reason(self) -> None:
        events, rejected = parse_stock_block(
            [{"exDiviDate": "", "cashDiviRMB": "10"}], code_hint="sh600519", today=TODAY
        )
        self.assertEqual(events, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("缺少除权除息日", rejected[0].reason)

    def test_row_with_neither_cash_nor_bonus_skipped(self) -> None:
        rows = [
            {
                "exDiviDate": "20240619",
                "cashDiviRMB": "",
                "bonusShareRatio": "",
                "tranAddShareRatio": "",
                "dividendPlan": "不分配不转增",
            }
        ]
        events, rejected = parse_stock_block(rows, code_hint="sh600519", today=TODAY)
        self.assertEqual(events, [])
        self.assertIn("既无现金分红也无送转", rejected[0].reason)

    def test_pure_bonus_row_kept(self) -> None:
        """只送转不派现也是有效分红事件。"""
        events, _ = parse_stock_block(
            [{"exDiviDate": "20240619", "cashDiviRMB": "", "tranAddShareRatio": "3"}],
            code_hint="sh600519",
            today=TODAY,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].dividend_per_share, Decimal("0"))
        self.assertEqual(events[0].per_share_transfer, Decimal("0.3"))

    def test_empty_cells_become_zero(self) -> None:
        """真实数据里"没有值"是空字符串，不是 0 也不是 null。"""
        events, _ = parse_stock_block(
            [
                {
                    "exDiviDate": "20240619",
                    "cashDiviRMB": "10",
                    "bonusShareRatio": "",
                    "tranAddShareRatio": "-",
                }
            ],
            code_hint="sh600519",
            today=TODAY,
        )
        self.assertEqual(events[0].per_share_bonus, Decimal("0"))
        self.assertEqual(events[0].per_share_transfer, Decimal("0"))

    def test_thousands_separator_accepted(self) -> None:
        events, _ = parse_stock_block(
            [{"exDiviDate": "20240619", "cashDiviRMB": "2,000"}],
            code_hint="sh600519",
            today=TODAY,
        )
        self.assertEqual(events[0].dividend_per_share, Decimal("200"))

    def test_dash_separated_date_accepted(self) -> None:
        events, _ = parse_stock_block(
            [{"exDiviDate": "2024-06-19", "cashDiviRMB": "10"}],
            code_hint="sh600519",
            today=TODAY,
        )
        self.assertEqual(events[0].ex_date, date(2024, 6, 19))

    def test_invalid_date_rejected(self) -> None:
        events, rejected = parse_stock_block(
            [{"exDiviDate": "2024年6月19日", "cashDiviRMB": "10"}],
            code_hint="sh600519",
            today=TODAY,
        )
        self.assertEqual(events, [])
        self.assertEqual(len(rejected), 1)

    def test_negative_cash_rejected(self) -> None:
        events, rejected = parse_stock_block(
            [{"exDiviDate": "20240619", "cashDiviRMB": "-10"}],
            code_hint="sh600519",
            today=TODAY,
        )
        self.assertEqual(events, [])
        self.assertEqual(len(rejected), 1)

    def test_missing_code_hint_rejected(self) -> None:
        events, rejected = parse_stock_block(
            [{"exDiviDate": "20240619", "cashDiviRMB": "10"}],
            code_hint=None,
            today=TODAY,
        )
        self.assertEqual(events, [])
        self.assertIn("缺少股票代码段头", rejected[0].reason)

    def test_dividend_flag_recorded_raw(self) -> None:
        """`dividendFlag` 真实值是"是"，不是 0/1——原样留存便于溯源。"""
        self.assertEqual(parse(MAOTAI).events[0].source_ref["dividendFlag"], "是")

    def test_window_filter_uses_ex_date(self) -> None:
        result = parse(CATL, start=date(2024, 1, 1), end=date(2024, 12, 31))
        self.assertTrue(result.events)
        for event in result.events:
            self.assertEqual(event.ex_date.year, 2024)


class KlineParserTest(unittest.TestCase):
    """`westock kline` 解析——用来补 `close_prev`（股息率的分母）。"""

    def test_real_kline_parses(self) -> None:
        closes = parse_kline_markdown(load(KLINE_MAOTAI))
        self.assertGreater(len(closes), 1000)
        days = sorted(closes)
        self.assertLess(days[0], days[-1])

    def test_header_constant_matches_reality(self) -> None:
        self.assertEqual(
            KLINE_HEADER,
            ("date", "open", "last", "high", "low", "volume", "amount", "exchange", "change_pct"),
        )

    def test_previous_trading_day_lookup(self) -> None:
        """除权日前一交易日的收盘价——真实值。"""
        closes = parse_kline_markdown(load(KLINE_MAOTAI))
        previous = max(day for day in closes if day < date(2024, 6, 19))
        self.assertEqual(previous, date(2024, 6, 18))
        self.assertEqual(closes[previous], Decimal("1387.16"))

    def test_changed_kline_header_rejected(self) -> None:
        tampered = load(KLINE_MAOTAI).replace("| last |", "| close |")
        with self.assertRaises(DataError) as ctx:
            parse_kline_markdown(tampered)
        self.assertIn("K 线表头与预期不一致", str(ctx.exception))

    def test_rate_limit_raises(self) -> None:
        with self.assertRaises(ProviderError):
            parse_kline_markdown("请求过于频繁，请稍后再试")

    def test_empty_raises(self) -> None:
        with self.assertRaises(ProviderError):
            parse_kline_markdown("   ")


class ClosePrevEnrichmentTest(unittest.TestCase):
    """补齐 `close_prev`：分红接口不给价格，只能另取 K 线。"""

    def test_fills_from_kline(self) -> None:
        events = parse(MAOTAI).events
        closes = {"600519.SH": parse_kline_markdown(load(KLINE_MAOTAI))}
        enriched, filled = enrich_close_prev(events, closes)

        self.assertEqual(filled, len(events))
        self.assertTrue(all(e.close_prev is not None for e in enriched))
        target = next(e for e in enriched if e.ex_date == date(2024, 6, 19))
        self.assertEqual(target.close_prev, Decimal("1387.16"))

    def test_original_events_not_mutated(self) -> None:
        events = parse(MAOTAI).events
        closes = {"600519.SH": parse_kline_markdown(load(KLINE_MAOTAI))}
        enrich_close_prev(events, closes)
        self.assertTrue(all(e.close_prev is None for e in events))

    def test_missing_kline_leaves_none_not_guessed(self) -> None:
        """没有 K 线就不补——绝不用别的价格冒充。"""
        events = parse(MAOTAI).events
        enriched, filled = enrich_close_prev(events, {})
        self.assertEqual(filled, 0)
        self.assertTrue(all(e.close_prev is None for e in enriched))

    def test_event_without_earlier_trading_day_untouched(self) -> None:
        """除权日早于所有 K 线数据时没有"前一交易日"，只能留空。"""
        events, _ = parse_stock_block(
            [{"exDiviDate": "20200101", "cashDiviRMB": "10"}],
            code_hint="sh600519",
            today=TODAY,
        )
        closes = {"600519.SH": {date(2024, 1, 2): Decimal("100")}}
        enriched, filled = enrich_close_prev(events, closes)
        self.assertEqual(filled, 0)
        self.assertIsNone(enriched[0].close_prev)

    def test_zero_close_not_used(self) -> None:
        """收盘价为 0（脏数据）不能当分母，否则股息率是无穷大。"""
        events, _ = parse_stock_block(
            [{"exDiviDate": "20240619", "cashDiviRMB": "10"}],
            code_hint="sh600519",
            today=TODAY,
        )
        closes = {"600519.SH": {date(2024, 6, 18): Decimal("0")}}
        enriched, filled = enrich_close_prev(events, closes)
        self.assertEqual(filled, 0)
        self.assertIsNone(enriched[0].close_prev)

    def test_enriched_event_records_provenance(self) -> None:
        events = parse(MAOTAI).events
        closes = {"600519.SH": parse_kline_markdown(load(KLINE_MAOTAI))}
        enriched, _ = enrich_close_prev(events, closes)
        self.assertEqual(enriched[0].source_ref["close_prev_from"], "westock kline")

    def test_multi_stock_enrichment(self) -> None:
        combined = load(MAOTAI) + "\n" + load(PINGAN) + "\n" + load(CATL)
        result = parse_dividend_markdown(combined, today=TODAY)
        closes = {
            "600519.SH": parse_kline_markdown(load(KLINE_MAOTAI)),
            "000001.SZ": parse_kline_markdown(load(KLINE_PINGAN)),
            "300750.SZ": parse_kline_markdown(load(KLINE_CATL)),
        }
        enriched, filled = enrich_close_prev(result.events, closes)
        self.assertEqual(filled, len(result.events))
        self.assertTrue(all(e.close_prev is not None for e in enriched))


class KlineRunnerTest(unittest.TestCase):
    def test_builds_kline_argv_with_cli_code(self) -> None:
        captured: list[list[str]] = []

        def fake(argv):
            captured.append(list(argv))
            return load(KLINE_PINGAN)

        runner = make_kline_runner(binary="/fake/westock", run=fake, sleep_seconds=0, retries=0)
        result = runner(["000001.SZ"], date(2021, 1, 1), date(2026, 10, 10))

        self.assertEqual(
            captured[0],
            [
                "/fake/westock",
                "kline",
                "sz000001",
                "--period",
                "day",
                "--start",
                "2021-01-01",
                "--end",
                "2026-10-10",
            ],
        )
        self.assertIn("000001.SZ", result)
        self.assertGreater(len(result["000001.SZ"]), 1000)

    def test_kline_failure_does_not_raise(self) -> None:
        """K 线只是锦上添花：取不到就不补，不该让整次同步失败。"""
        messages: list[str] = []
        runner = make_kline_runner(
            binary="westock",
            run=lambda argv: "请求过于频繁，请稍后再试",
            sleep_seconds=0,
            retries=0,
            on_progress=messages.append,
        )
        result = runner(["600519.SH"], date(2021, 1, 1), date(2026, 10, 10))
        self.assertEqual(result, {})
        self.assertTrue(any("股息率将为空" in m for m in messages))


class SearchParserTest(unittest.TestCase):
    """`westock search` 解析——客户端搜索框的数据来源。"""

    REAL = (
        "\n**股票** — 共 1 条，显示前 1\n\n"
        "| code | name | type |\n"
        "| --- | --- | --- |\n"
        "| sh600519 | 贵州茅台 | GP-A |\n"
    )

    def test_header_constant(self) -> None:
        self.assertEqual(SEARCH_HEADER, ("code", "name", "type"))

    def test_parses_real_output(self) -> None:
        matches = parse_search_markdown(self.REAL)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].code, "600519.SH")
        self.assertEqual(matches[0].name, "贵州茅台")
        self.assertEqual(matches[0].raw_code, "sh600519")
        self.assertEqual(matches[0].kind, "GP-A")

    def test_no_result_is_empty_list_not_error(self) -> None:
        """CLI 无匹配时只回一句中文提示——这是**正常空结果**，不是错误。"""
        for text in ("未找到匹配的结果（股票）。", "无匹配结果", ""):
            with self.subTest(text=text):
                self.assertEqual(parse_search_markdown(text), [])

    def test_rate_limit_raises(self) -> None:
        with self.assertRaises(ProviderError):
            parse_search_markdown("请求过于频繁，请稍后再试")

    def test_changed_header_rejected(self) -> None:
        tampered = self.REAL.replace("| code | name | type |", "| symbol | name | type |")
        with self.assertRaises(DataError) as ctx:
            parse_search_markdown(tampered)
        self.assertIn("表头与预期不一致", str(ctx.exception))

    def test_non_a_share_rows_skipped(self) -> None:
        """港股代码在当前账本里无法表示，跳过而不是报错。"""
        text = (
            "| code | name | type |\n| --- | --- | --- |\n"
            "| hk00700 | 腾讯控股 | GP-H |\n"
            "| sh600519 | 贵州茅台 | GP-A |\n"
        )
        matches = parse_search_markdown(text)
        self.assertEqual([m.code for m in matches], ["600519.SH"])

    def test_multiple_rows(self) -> None:
        text = (
            "| code | name | type |\n| --- | --- | --- |\n"
            "| sh600519 | 贵州茅台 | GP-A |\n"
            "| sz000001 | 平安银行 | GP-A |\n"
        )
        self.assertEqual(len(parse_search_markdown(text)), 2)

    def test_runner_builds_search_command(self) -> None:
        captured: list[list[str]] = []

        def fake(argv):
            captured.append(list(argv))
            return self.REAL

        runner = make_search_runner(binary="/fake/westock", run=fake, retries=0)
        matches = runner("茅台")
        self.assertEqual(captured[0], ["/fake/westock", "search", "茅台"])
        self.assertEqual(matches[0].code, "600519.SH")

    def test_runner_retries_on_rate_limit(self) -> None:
        calls: list[int] = []

        def flaky(argv):
            calls.append(1)
            return "请求过于频繁，请稍后再试" if len(calls) < 2 else self.REAL

        runner = make_search_runner(
            binary="westock", run=flaky, retries=2, retry_backoff_seconds=0
        )
        self.assertEqual(len(runner("茅台")), 1)
        self.assertEqual(len(calls), 2)

    def test_runner_exhausted_raises(self) -> None:
        runner = make_search_runner(
            binary="westock",
            run=lambda argv: "请求过于频繁，请稍后再试",
            retries=1,
            retry_backoff_seconds=0,
        )
        with self.assertRaises(ProviderError):
            runner("茅台")


class ProviderTest(unittest.TestCase):
    def test_without_runner_refuses_to_go_online(self) -> None:
        provider = WestockProvider()
        with self.assertRaises(ProviderError) as ctx:
            provider.list_events(["600519.SH"], date(2020, 1, 1), date(2026, 1, 1))
        self.assertIn("未注入 runner", str(ctx.exception))

    def test_injected_runner_end_to_end(self) -> None:
        def runner(codes, start, end):
            return {"text": load(MAOTAI)}

        provider = WestockProvider(runner, today=TODAY)
        result = provider.list_events(["600519.SH"], date(2000, 1, 1), date(2030, 1, 1))
        self.assertTrue(result.events)
        self.assertEqual(result.provenance["provider"], "westock")
        self.assertEqual(result.provenance["shares_basis"], 10)
        self.assertEqual(result.provenance["stocks_in_payload"], 1)

    def test_runner_returning_bare_string_works(self) -> None:
        provider = WestockProvider(lambda c, s, e: load(PINGAN), today=TODAY)
        result = provider.list_events(["000001.SZ"], date(2000, 1, 1), date(2030, 1, 1))
        self.assertTrue(result.events)

    def test_runner_returning_garbage_rejected(self) -> None:
        provider = WestockProvider(lambda c, s, e: 12345, today=TODAY)
        with self.assertRaises(DataError):
            provider.list_events(["600519.SH"], date(2000, 1, 1), date(2030, 1, 1))


class CliRunnerTest(unittest.TestCase):
    """`make_cli_runner` 的命令拼装与多代码合并（不真跑 CLI）。"""

    def test_builds_expected_argv_and_appends_all(self) -> None:
        captured: list[list[str]] = []

        runner = make_cli_runner(
            binary="/fake/westock",
            run=lambda argv: captured.append(list(argv)) or load(MAOTAI),
            sleep_seconds=0,
            years=6,
            request_all=True,
            retries=0,
        )
        runner(["600519.SH"], date(2020, 1, 1), date(2026, 1, 1))

        # 关键：CLI 要前缀格式 sh600519，不是规范后缀格式 600519.SH
        self.assertEqual(
            captured[0], ["/fake/westock", "dividend", "sh600519", "--years", "6", "--all"]
        )

    def test_deep_market_code_converted_to_prefix_form(self) -> None:
        """深市代码必须转成 sz000001——实测 CLI 直接拒绝 000001.SZ 这种写法。"""
        captured: list[list[str]] = []
        runner = make_cli_runner(
            binary="westock",
            run=lambda argv: captured.append(list(argv)) or load(PINGAN),
            sleep_seconds=0,
            retries=0,
        )
        runner(["000001.SZ"], date(2020, 1, 1), date(2026, 1, 1))
        self.assertEqual(captured[0][2], "sz000001")

    def test_format_error_from_stderr_is_not_retried(self) -> None:
        """格式类错误重试无意义：应当立刻失败，并把 stderr 的原因带出来。"""
        calls: list[int] = []
        messages: list[str] = []

        def fake(argv):
            calls.append(1)
            return "", '错误: 代码 "000001.SZ" 格式不支持；请改用 "sz000001"'

        runner = make_cli_runner(
            binary="westock",
            run=fake,
            sleep_seconds=0,
            retries=3,
            retry_backoff_seconds=0,
            on_progress=messages.append,
        )
        with self.assertRaises(ProviderError) as ctx:
            runner(["000001.SZ"], date(2020, 1, 1), date(2026, 1, 1))
        self.assertEqual(len(calls), 1, "永久性错误不应重试")
        self.assertIn("格式不支持", str(ctx.exception))

    def test_stderr_diagnostic_is_surfaced_on_exhaustion(self) -> None:
        """CLI 退出码为 0 且 stdout 为空时，原因只能从 stderr 拿。"""
        runner = make_cli_runner(
            binary="westock",
            run=lambda argv: ("", "error: upstream timeout"),
            sleep_seconds=0,
            retries=0,
            retry_backoff_seconds=0,
        )
        with self.assertRaises(ProviderError) as ctx:
            runner(["600519.SH"], date(2020, 1, 1), date(2026, 1, 1))
        self.assertIn("upstream timeout", str(ctx.exception))

    def test_all_flag_can_be_disabled(self) -> None:
        captured: list[list[str]] = []
        runner = make_cli_runner(
            binary="westock",
            run=lambda argv: captured.append(list(argv)) or load(MAOTAI),
            sleep_seconds=0,
            request_all=False,
            retries=0,
        )
        runner(["600519.SH"], date(2020, 1, 1), date(2026, 1, 1))
        self.assertNotIn("--all", captured[0])

    def test_merges_multiple_codes_into_one_payload(self) -> None:
        calls: list[str] = []

        def fake(argv):
            # argv[2] 是 CLI 前缀格式：sh600519 / sz000001
            calls.append(argv[2])
            return load(MAOTAI) if argv[2] == "sh600519" else load(PINGAN)

        runner = make_cli_runner(binary="westock", run=fake, sleep_seconds=0, retries=0)
        payload = runner(["600519.SH", "000001.SZ"], date(2020, 1, 1), date(2026, 1, 1))

        self.assertEqual(calls, ["sh600519", "sz000001"])
        result = parse_dividend_markdown(payload["text"], today=TODAY)
        self.assertEqual({e.code for e in result.events}, {"600519.SH", "000001.SZ"})

    def test_rate_limit_is_retried_then_succeeds(self) -> None:
        """限频不是致命错误：应当重试，并在恢复后成功取到数据。"""
        calls: list[int] = []

        def flaky(argv):
            calls.append(1)
            if len(calls) < 3:
                return "请求过于频繁，请稍后再试"
            return load(MAOTAI)

        runner = make_cli_runner(
            binary="westock",
            run=flaky,
            sleep_seconds=0,
            retries=3,
            retry_backoff_seconds=0,
        )
        payload = runner(["600519.SH"], date(2020, 1, 1), date(2026, 1, 1))
        self.assertEqual(len(calls), 3, "两次限频后第三次应成功")
        self.assertTrue(parse_dividend_markdown(payload["text"], today=TODAY).events)

    def test_retry_exhausted_reports_reason(self) -> None:
        """重试用尽必须报错，并把原因说清楚。"""
        runner = make_cli_runner(
            binary="westock",
            run=lambda argv: "请求过于频繁，请稍后再试",
            sleep_seconds=0,
            retries=1,
            retry_backoff_seconds=0,
        )
        with self.assertRaises(ProviderError) as ctx:
            runner(["600519.SH"], date(2020, 1, 1), date(2026, 1, 1))
        message = str(ctx.exception)
        self.assertIn("已重试 1 次", message)
        self.assertIn("请求过于频繁", message)

    def test_empty_stdout_is_retried(self) -> None:
        """空 stdout 也要重试（CLI 偶发无输出）。"""
        calls: list[int] = []

        def flaky(argv):
            calls.append(1)
            return "" if len(calls) < 2 else load(PINGAN)

        runner = make_cli_runner(
            binary="westock",
            run=flaky,
            sleep_seconds=0,
            retries=2,
            retry_backoff_seconds=0,
        )
        payload = runner(["000001.SZ"], date(2020, 1, 1), date(2026, 1, 1))
        self.assertEqual(len(calls), 2)
        self.assertTrue(parse_dividend_markdown(payload["text"], today=TODAY).events)

    def test_retry_still_fails_loudly_after_exhaustion(self) -> None:
        """重试用尽后是 ProviderError，**不是**空结果。"""
        runner = make_cli_runner(
            binary="westock",
            run=lambda argv: "",
            sleep_seconds=0,
            retries=1,
            retry_backoff_seconds=0,
        )
        with self.assertRaises(ProviderError):
            runner(["600519.SH"], date(2020, 1, 1), date(2026, 1, 1))

    def test_progress_callback_reports_each_code(self) -> None:
        messages: list[str] = []
        runner = make_cli_runner(
            binary="westock",
            run=lambda argv: load(MAOTAI),
            sleep_seconds=0,
            retries=0,
            on_progress=messages.append,
        )
        runner(["600519.SH", "000001.SZ"], date(2020, 1, 1), date(2026, 1, 1))
        self.assertEqual(len([m for m in messages if "取 600519.SH" in m]), 1)
        self.assertEqual(len([m for m in messages if "取 000001.SZ" in m]), 1)

    def test_default_sleep_is_positive_to_avoid_rate_limit(self) -> None:
        """实测连续 16 次请求即限频，所以默认间隔必须 > 0。"""
        import inspect

        signature = inspect.signature(make_cli_runner)
        self.assertGreater(signature.parameters["sleep_seconds"].default, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
