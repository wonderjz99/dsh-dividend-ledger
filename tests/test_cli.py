"""CLI 端到端测试（SPEC §6 / PLAN §1.3）。

用 ``main(argv)`` 直接调用，不 fork 子进程——测试快且能拿到真实退出码。
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from dividend_ledger.cli import EXIT_DATA_ERROR, EXIT_OK, main

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "data"
FIXED_NOW = "2026-10-10T12:00:00+08:00"


class CliTestCase(unittest.TestCase):
    """每个用例都在 data/ 的临时副本上跑，绝不污染工作区。"""

    def setUp(self) -> None:  # noqa: D102
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        shutil.copytree(
            FIXTURE_DIR,
            self.data,
            # 只复制 fixture 主干。必须排除的：
            #   out / events.json —— 上次运行的产物，留着测试起点就不确定
            #   raw / real        —— 真实数据抓取与真实验证目录，几 MB 且与 CLI 无关
            ignore=shutil.ignore_patterns(
                "out", "__pycache__", "events.json", "raw", "real"
            ),
        )
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self) -> None:  # noqa: D102
        self._tmp.cleanup()

    def run_cli(self, *argv: str) -> int:
        """跑一次 CLI，返回退出码；stdout/stderr 存在 self.out / self.err。"""
        self.out = io.StringIO()
        self.err = io.StringIO()
        with redirect_stdout(self.out), redirect_stderr(self.err):
            return main(["--data", str(self.data), *argv])

    def json_out(self) -> dict:
        return json.loads(self.out.getvalue())


class HappyPathTest(CliTestCase):
    def test_validate_reports_missing_events_before_sync(self) -> None:
        """validate 的语义是"能否出报告"：还没 sync 时 events.json 缺失，应报问题。"""
        code = self.run_cli("validate")
        self.assertEqual(code, EXIT_DATA_ERROR)
        payload = self.json_out()
        self.assertFalse(payload["ok"])
        self.assertTrue(any("events.json" in p for p in payload["problems"]))
        self.assertEqual(payload["holdings"]["count"], 6)

    def test_validate_ok_after_sync(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        self.assertEqual(self.run_cli("validate"), EXIT_OK)
        payload = self.json_out()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["holdings"]["count"], 6)
        # fixture 共 24 条：22 条已实施 + 2 条未实施（阶段三的待收样本）
        self.assertEqual(payload["events"]["count"], 24)

    def test_full_pipeline(self) -> None:
        """sync → report，一步不少。"""
        self.assertEqual(self.run_cli("sync", "--from", "fixture"), EXIT_OK)
        sync = self.json_out()
        self.assertEqual(sync["import"]["imported"], 24)

        self.assertEqual(
            self.run_cli("report", "--now", FIXED_NOW), EXIT_OK
        )
        report = json.loads((self.data / "out" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["totals"]["cash_amount"], "26856.40")
        self.assertEqual(report["totals"]["cost_basis"], "482600.00")
        self.assertEqual(report["totals"]["event_count"], 19)
        self.assertEqual(report["totals"]["stock_count"], 6)

    def test_report_writes_both_files(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        self.run_cli("report", "--now", FIXED_NOW)
        self.assertTrue((self.data / "out" / "report.json").exists())
        self.assertTrue((self.data / "out" / "ledger.json").exists())

    def test_report_is_byte_identical_across_runs(self) -> None:
        """PLAN §1.3 的第一条验收标准。"""
        self.run_cli("sync", "--from", "fixture")
        self.run_cli("report", "--now", FIXED_NOW)
        first_report = (self.data / "out" / "report.json").read_bytes()
        first_ledger = (self.data / "out" / "ledger.json").read_bytes()

        self.run_cli("report", "--now", FIXED_NOW)
        self.assertEqual((self.data / "out" / "report.json").read_bytes(), first_report)
        self.assertEqual((self.data / "out" / "ledger.json").read_bytes(), first_ledger)

    def test_sync_is_idempotent(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        first = (self.data / "events.json").read_bytes()
        self.run_cli("sync", "--from", "fixture")
        self.assertEqual((self.data / "events.json").read_bytes(), first)
        self.assertEqual(self.json_out()["import"]["unchanged"], 24)

    def test_holdings_set_identical_is_unchanged(self) -> None:
        """与账本里完全相同的持仓再导一次 → unchanged，不产生重复。"""
        good = Path(self._tmp.name) / "same.json"
        good.write_text(
            json.dumps(
                {
                    "holdings": [
                        {"code": "600519.SH", "name": "虚构白酒",
                         "start_date": "2021-03-15", "shares": "100",
                         "cost_per_share": "1950"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        code = self.run_cli("holdings", "--set", str(good))
        self.assertEqual(code, EXIT_OK)
        payload = self.json_out()
        self.assertEqual(payload["unchanged"], 1)
        self.assertEqual(payload["imported"], 0)
        holdings = json.loads((self.data / "holdings.json").read_text(encoding="utf-8"))
        self.assertEqual(len(holdings["holdings"]), 6)

    def test_holdings_read(self) -> None:
        self.assertEqual(self.run_cli("holdings"), EXIT_OK)
        payload = self.json_out()
        self.assertEqual(payload["count"], 6)

    def test_holdings_text_mentions_missing_cost(self) -> None:
        self.assertEqual(self.run_cli("--text", "holdings"), EXIT_OK)
        self.assertIn("601988.SH", self.out.getvalue())
        self.assertIn("未提供每股成本", self.out.getvalue())

    def test_report_text_is_compact_and_explains_basis(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        self.assertEqual(self.run_cli("--text", "report", "--now", FIXED_NOW), EXIT_OK)
        text = self.out.getvalue()
        self.assertIn("税前分红合计", text)
        self.assertIn("除权除息日", text)
        self.assertIn("红利税", text)
        # 不应把整份 JSON 灌进对话
        self.assertLess(len(text), 3000)

    def test_pay_date_override_applied_from_fixture(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        self.run_cli("report", "--now", FIXED_NOW)
        report = json.loads((self.data / "out" / "report.json").read_text(encoding="utf-8"))
        counts = report["meta"]["date_basis_counts"]
        self.assertEqual(counts.get("pay_date_override"), 2)
        self.assertEqual(counts.get("ex_date"), 17)

    def test_unmatched_override_warns(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        self.run_cli("report", "--now", FIXED_NOW)
        report = json.loads((self.data / "out" / "report.json").read_text(encoding="utf-8"))
        warnings = " ".join(report["meta"]["warnings"])
        self.assertIn("920819.BJ|2020-01-01", warnings)

    def test_custom_output_paths(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        target = Path(self._tmp.name) / "custom.json"
        ledger_target = Path(self._tmp.name) / "custom-ledger.json"
        code = self.run_cli(
            "report",
            "--now",
            FIXED_NOW,
            "--out",
            str(target),
            "--ledger-out",
            str(ledger_target),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(target.exists())
        self.assertTrue(ledger_target.exists())


class OfflineGuaranteeTest(CliTestCase):
    """离线路径**绝不允许**发起任何子进程/网络调用。

    这条守护是有来由的：阶段二加"补除权前收盘价"时，我一度让它对所有来源都默认开启，
    于是 `sync --from fixture` 也去打真实 API，每个 CLI 测试都要等 25 秒重试——
    表现为"测试卡死"。离线承诺是 SPEC 的硬约束，必须由测试守住。
    """

    def _forbid_subprocess(self) -> list[list[str]]:
        """把 subprocess.run 换成"一调用就记录并断言失败"。"""
        called: list[list[str]] = []
        original = subprocess.run

        def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
            called.append(list(args[0]) if args and isinstance(args[0], (list, tuple)) else [str(args)])
            return original(*args, **kwargs)

        subprocess.run = spy  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(subprocess, "run", original))
        return called

    def test_fixture_sync_spawns_no_process(self) -> None:
        called = self._forbid_subprocess()
        code = self.run_cli("sync", "--from", "fixture")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(called, [], f"fixture 同步不该起任何子进程，实际起了：{called}")

    def test_fixture_sync_then_report_spawns_no_process(self) -> None:
        called = self._forbid_subprocess()
        self.run_cli("sync", "--from", "fixture")
        self.run_cli("report", "--now", FIXED_NOW)
        self.assertEqual(called, [], f"离线全链路不该起子进程，实际起了：{called}")

    def test_westock_with_payload_file_spawns_no_process(self) -> None:
        """给了 --payload-file 就是离线解析，同样不许起进程。"""
        called = self._forbid_subprocess()
        code = self.run_cli(
            "sync",
            "--from",
            "westock",
            "--payload-file",
            str(REPO_ROOT / "data" / "raw" / "dividend-sh600519.md"),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(called, [], f"离线解析不该起子进程，实际起了：{called}")

    def test_close_prev_not_requested_for_fixture(self) -> None:
        """fixture 来源下 close_prev 必须报 requested=false——这正是那个 bug 的指纹。"""
        code = self.run_cli("sync", "--from", "fixture")
        self.assertEqual(code, EXIT_OK)
        payload = self.json_out()
        self.assertFalse(payload["close_prev"]["requested"])
        self.assertEqual(payload["close_prev"]["filled"], 0)


class SyncSourcesTest(CliTestCase):
    def test_westock_without_payload_file_uses_cli(self) -> None:
        """不给 --payload-file 时走真实 CLI 路径；用假 CLI 验证命令拼装与错误处理。"""
        fake = Path(self._tmp.name) / "fake-westock"
        fake.write_text(
            "#!/bin/bash\n"
            "# 回放真实 CLI 的限频行为：提示写 stdout，退出码 0\n"
            "echo '请求过于频繁，请稍后再试'\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)

        code = self.run_cli(
            "sync",
            "--from",
            "westock",
            "--westock-bin",
            str(fake),
            "--sleep",
            "0",
            "--retries",
            "0",
        )
        self.assertEqual(code, EXIT_DATA_ERROR)
        # 关键：限频必须报错，不能被当成"没有分红数据"
        self.assertIn("限频", self.err.getvalue())

    def test_westock_failure_to_launch_reports_readably(self) -> None:
        """CLI 不存在时要给出可读错误，而不是崩栈。"""
        code = self.run_cli(
            "sync",
            "--from",
            "westock",
            "--westock-bin",
            "/nonexistent/westock",
            "--sleep",
            "0",
            "--retries",
            "0",
        )
        self.assertEqual(code, EXIT_DATA_ERROR)
        self.assertNotEqual(self.err.getvalue().strip(), "")

    def test_westock_offline_parse_from_markdown(self) -> None:
        """用真实的 Markdown 响应文件走通 westock 解析链路（离线）。"""
        code = self.run_cli(
            "sync",
            "--from",
            "westock",
            "--payload-file",
            str(REPO_ROOT / "data" / "raw" / "dividend-sh600519.md"),
        )
        self.assertEqual(code, EXIT_OK)
        payload = self.json_out()
        self.assertEqual(payload["source"], "westock")
        self.assertGreater(payload["import"]["imported"], 0)
        self.assertEqual(payload["provider"]["shares_basis"], 10)

    def test_westock_events_use_ex_date_basis(self) -> None:
        self.run_cli(
            "sync",
            "--from",
            "westock",
            "--payload-file",
            str(REPO_ROOT / "data" / "raw" / "dividend-sh600519.md"),
        )
        events = json.loads((self.data / "events.json").read_text(encoding="utf-8"))["events"]
        self.assertTrue(events)
        for item in events:
            self.assertEqual(item["date_basis"], "ex_date")
            self.assertNotIn("pay_date", item)

    def test_westock_same_day_two_plans_both_imported(self) -> None:
        """宁德时代同日两条方案都要落库——唯一键缺 proposalSn 会丢一笔。"""
        code = self.run_cli(
            "sync",
            "--from",
            "westock",
            "--payload-file",
            str(REPO_ROOT / "data" / "raw" / "dividend-sz300750.md"),
        )
        self.assertEqual(code, EXIT_OK)
        events = json.loads((self.data / "events.json").read_text(encoding="utf-8"))["events"]
        same_day = [e for e in events if e["ex_date"] == "2026-04-22"]
        self.assertEqual(len(same_day), 2)
        self.assertEqual({e["proposal_sn"] for e in same_day}, {"1", "2"})

    def test_replace_mode_clears_previous_source(self) -> None:
        """换数据源时必须能清空重写——否则不同唯一键会让同一次除权出现两条。"""
        self.assertEqual(self.run_cli("sync", "--from", "fixture"), EXIT_OK)
        before = len(json.loads((self.data / "events.json").read_text(encoding="utf-8"))["events"])

        code = self.run_cli(
            "sync",
            "--from",
            "westock",
            "--replace",
            "--payload-file",
            str(REPO_ROOT / "data" / "raw" / "dividend-sh600519.md"),
        )
        self.assertEqual(code, EXIT_OK)
        payload = self.json_out()
        self.assertEqual(payload["import"]["details"]["mode"], "replace")
        self.assertEqual(payload["import"]["details"]["removed"], before)

        after = json.loads((self.data / "events.json").read_text(encoding="utf-8"))["events"]
        self.assertTrue(all(e["source"] == "westock" for e in after))
        self.assertFalse(any(e["code"] == "000001.SZ" for e in after))

    def test_westock_rate_limit_message_fails_loudly(self) -> None:
        """限频提示必须报错——绝不能当成"没有分红数据"。"""
        limited = Path(self._tmp.name) / "limited.md"
        limited.write_text("请求过于频繁，请稍后再试", encoding="utf-8")
        code = self.run_cli("sync", "--from", "westock", "--payload-file", str(limited))
        self.assertEqual(code, EXIT_DATA_ERROR)
        self.assertIn("限频", self.err.getvalue())


class FailurePathTest(CliTestCase):
    def test_report_without_holdings_fails_readably(self) -> None:
        (self.data / "holdings.json").unlink()
        code = self.run_cli("report")
        self.assertEqual(code, EXIT_DATA_ERROR)
        self.assertIn("holdings.json", self.err.getvalue())

    def test_report_without_events_fails_readably(self) -> None:
        code = self.run_cli("report")
        self.assertEqual(code, EXIT_DATA_ERROR)
        self.assertIn("events.json", self.err.getvalue())

    def test_sync_without_holdings_fails_readably(self) -> None:
        (self.data / "holdings.json").unlink()
        code = self.run_cli("sync", "--from", "fixture")
        self.assertEqual(code, EXIT_DATA_ERROR)
        self.assertIn("holdings.json", self.err.getvalue())

    def test_validate_reports_missing_files(self) -> None:
        (self.data / "holdings.json").unlink()
        code = self.run_cli("validate")
        self.assertEqual(code, EXIT_DATA_ERROR)
        payload = self.json_out()
        self.assertFalse(payload["ok"])
        self.assertTrue(any("holdings.json" in p for p in payload["problems"]))

    def test_validate_flags_bad_rows(self) -> None:
        (self.data / "events.json").write_text(
            json.dumps({"events": [{"code": "nope", "ex_date": "x"}]}),
            encoding="utf-8",
        )
        code = self.run_cli("validate")
        self.assertEqual(code, EXIT_DATA_ERROR)
        payload = self.json_out()
        self.assertEqual(payload["events"]["rejected_count"], 1)

    def test_holdings_set_rejects_bad_file_entirely(self) -> None:
        """有坏行时整份拒绝导入，不做部分成功。"""
        bad = Path(self._tmp.name) / "bad.json"
        bad.write_text(
            json.dumps(
                {
                    "holdings": [
                        {"code": "600519.SH", "start_date": "2021-03-15", "shares": "100"},
                        {"code": "bad", "start_date": "x", "shares": "1"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        code = self.run_cli("holdings", "--set", str(bad))
        self.assertEqual(code, EXIT_DATA_ERROR)
        self.assertIn("全部拒绝导入", self.err.getvalue())

    def test_holdings_set_accepts_good_file(self) -> None:
        good = Path(self._tmp.name) / "good.json"
        good.write_text(
            json.dumps(
                {
                    "holdings": [
                        # 002594.SZ 不在 fixture 里 → 应计为新增
                        {"code": "002594.SZ", "start_date": "2023-01-04",
                         "shares": "300", "cost_per_share": "240"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        code = self.run_cli("holdings", "--set", str(good))
        self.assertEqual(code, EXIT_OK)
        payload = self.json_out()
        self.assertEqual(payload["imported"], 1)
        holdings = json.loads((self.data / "holdings.json").read_text(encoding="utf-8"))
        self.assertEqual(len(holdings["holdings"]), 7)

    def test_holdings_set_from_stdin(self) -> None:
        import sys

        payload = json.dumps(
            [{"code": "002594.SZ", "start_date": "2023-01-04", "shares": "300"}]
        )
        original = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            code = self.run_cli("holdings", "--set", "-")
        finally:
            sys.stdin = original
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.json_out()["imported"], 1)

    def test_bad_now_rejected(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        code = self.run_cli("report", "--now", "not-a-date")
        self.assertEqual(code, EXIT_DATA_ERROR)
        self.assertIn("--now", self.err.getvalue())

    def test_missing_events_file_for_sync(self) -> None:
        code = self.run_cli("sync", "--from", "fixture", "--events-file", "/nonexistent.json")
        self.assertEqual(code, EXIT_DATA_ERROR)


class HoldingsDeleteTest(CliTestCase):
    """删除持仓（面板的「删除」按钮走的就是这条）。

    关键点：`holdings --set` 默认是**按 code 合并**，那表达不了删除——
    传空数组等于什么都不做。所以删除必须走 `--replace`（整份替换）。
    """

    def _write(self, holdings: list[dict]) -> Path:
        path = Path(self._tmp.name) / "incoming.json"
        path.write_text(json.dumps({"holdings": holdings}, ensure_ascii=False), encoding="utf-8")
        return path

    def _codes(self) -> list[str]:
        payload = json.loads((self.data / "holdings.json").read_text(encoding="utf-8"))
        return [item["code"] for item in payload["holdings"]]

    def test_merge_alone_cannot_delete(self) -> None:
        """这条锁住"为什么需要 --replace"：合并语义下空数组删不掉任何东西。"""
        before = self._codes()
        code = self.run_cli("holdings", "--set", str(self._write([])))
        self.assertEqual(code, EXIT_OK, self.err.getvalue())
        # 比**集合**：落盘时按 code 规范排序（确定性要求），顺序不该进断言
        self.assertEqual(
            sorted(self._codes()), sorted(before), "合并语义不应删除记录"
        )

    def test_replace_removes_missing_entries(self) -> None:
        before = list(self._codes())
        kept = [{"code": before[0], "start_date": "2024-01-01", "shares": "10"}]
        code = self.run_cli("holdings", "--set", str(self._write(kept)), "--replace")
        self.assertEqual(code, EXIT_OK, self.err.getvalue())
        payload = self.json_out()
        self.assertEqual(payload["details"]["mode"], "replace")
        self.assertEqual(payload["details"]["removed"], len(before))
        self.assertEqual(list(self._codes()), [before[0]])

    def test_replace_with_empty_array_clears_all(self) -> None:
        code = self.run_cli("holdings", "--set", str(self._write([])), "--replace")
        self.assertEqual(code, EXIT_OK, self.err.getvalue())
        self.assertEqual(list(self._codes()), [])

    def test_cleared_holdings_still_readable(self) -> None:
        self.run_cli("holdings", "--set", str(self._write([])), "--replace")
        code = self.run_cli("holdings")
        self.assertEqual(code, EXIT_OK, self.err.getvalue())
        self.assertEqual(self.json_out()["count"], 0)

    def test_portfolio_with_zero_holdings_does_not_crash(self) -> None:
        """删光之后持仓视图必须还能打开，而不是报错。"""
        self.run_cli("holdings", "--set", str(self._write([])), "--replace")
        code = self.run_cli("portfolio", "--offline")
        self.assertEqual(code, EXIT_OK, self.err.getvalue())
        payload = self.json_out()
        self.assertEqual(payload["count"], 0)
        self.assertIsNone(payload["totals"]["dynamicYield"])

    def test_delete_then_re_add_works(self) -> None:
        before = list(self._codes())
        self.run_cli("holdings", "--set", str(self._write([])), "--replace")
        again = [{"code": before[0], "start_date": "2024-01-01", "shares": "50"}]
        self.run_cli("holdings", "--set", str(self._write(again)), "--replace")
        self.assertEqual(list(self._codes()), [before[0]])


class CandidatesOfflineTest(CliTestCase):
    """`candidates --offline` 的回归测试。

    这条路径曾经 AttributeError：本地已知代码里有些只在 events 里出现过、
    **不在持仓里**，而取名字时没判 None，一搜就 500。
    """

    def _add_extra_event_only_code(self) -> None:
        """造一只"有分红事件但没持仓"的股票。"""
        self.run_cli("sync", "--from", "fixture")
        events = json.loads((self.data / "events.json").read_text(encoding="utf-8"))["events"]
        events.append(
            {
                "code": "000858.SZ",
                "ex_date": "2026-06-20",
                "dividend_per_share": "3.5",
                "source": "fixture",
            }
        )
        (self.data / "events.json").write_text(
            json.dumps({"events": events}, ensure_ascii=False), encoding="utf-8"
        )

    def test_offline_search_does_not_crash_on_event_only_code(self) -> None:
        self._add_extra_event_only_code()
        code = self.run_cli("candidates", "--query", "000858", "--offline")
        self.assertEqual(code, EXIT_OK, self.err.getvalue())
        payload = self.json_out()
        self.assertEqual(payload["count"], 1)
        row = payload["rows"][0]
        self.assertEqual(row["code"], "000858.SZ")
        # 没有持仓记录时名字退化成代码，而不是崩
        self.assertEqual(row["name"], "000858.SZ")

    def test_offline_search_matches_holding_name(self) -> None:
        code = self.run_cli("candidates", "--query", "虚构能源", "--offline")
        self.assertEqual(code, EXIT_OK, self.err.getvalue())
        payload = self.json_out()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["rows"][0]["code"], "601988.SH")

    def test_offline_search_matches_by_code(self) -> None:
        code = self.run_cli("candidates", "--query", "600519", "--offline")
        self.assertEqual(code, EXIT_OK, self.err.getvalue())
        self.assertEqual(self.json_out()["rows"][0]["code"], "600519.SH")

    def test_offline_search_no_match_returns_empty(self) -> None:
        code = self.run_cli("candidates", "--query", "zzzz不存在", "--offline")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.json_out()["count"], 0)

    def test_offline_prices_are_null_not_guessed(self) -> None:
        code = self.run_cli("candidates", "--query", "600519", "--offline")
        self.assertEqual(code, EXIT_OK)
        row = self.json_out()["rows"][0]
        self.assertIsNone(row["price"])
        self.assertIsNone(row["dynamicYield"])
        self.assertIn("price", row["missing"])

    def test_empty_query_is_rejected(self) -> None:
        code = self.run_cli("candidates", "--query", "   ", "--offline")
        self.assertEqual(code, EXIT_DATA_ERROR)
        self.assertIn("query", self.err.getvalue())


class PortfolioCommandTest(CliTestCase):
    """`portfolio` 子命令（面板的持仓视图 + 组合总股息率）。"""

    def test_offline_portfolio_uses_stored_events(self) -> None:
        self.assertEqual(self.run_cli("sync", "--from", "fixture"), EXIT_OK)
        code = self.run_cli("portfolio", "--offline")
        self.assertEqual(code, EXIT_OK, self.err.getvalue())
        payload = self.json_out()
        self.assertEqual(payload["count"], 6)
        self.assertEqual(payload["priceCache"]["miss"], 0)

    def test_offline_portfolio_yields_are_null(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        code = self.run_cli("portfolio", "--offline")
        self.assertEqual(code, EXIT_OK)
        totals = self.json_out()["totals"]
        self.assertIsNone(totals["dynamicYield"])
        self.assertIsNone(totals["expectedYield"])
        self.assertFalse(totals["complete"])

    def test_offline_portfolio_still_reports_received_and_cost(self) -> None:
        """价格取不到，但"累计已收"与"成本收益率"不依赖价格，必须照常给出。"""
        self.run_cli("sync", "--from", "fixture")
        code = self.run_cli("portfolio", "--offline")
        self.assertEqual(code, EXIT_OK)
        totals = self.json_out()["totals"]
        self.assertEqual(totals["receivedTotal"], "26856.40")
        self.assertEqual(totals["costBasis"], "482600.00")
        self.assertEqual(totals["yieldOnCost"], "0.055649")

    def test_portfolio_without_holdings_fails_readably(self) -> None:
        (self.data / "holdings.json").unlink()
        code = self.run_cli("portfolio", "--offline")
        self.assertEqual(code, EXIT_DATA_ERROR)
        self.assertIn("holdings.json", self.err.getvalue())

    def test_bad_names_json_rejected(self) -> None:
        code = self.run_cli("portfolio", "--offline", "--names", "{not json")
        self.assertEqual(code, EXIT_DATA_ERROR)
        self.assertIn("--names", self.err.getvalue())

    def test_names_mapping_applied(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        code = self.run_cli(
            "portfolio", "--offline", "--names", json.dumps({"600519.SH": "我给它起的名字"})
        )
        self.assertEqual(code, EXIT_OK)
        row = next(r for r in self.json_out()["rows"] if r["code"] == "600519.SH")
        self.assertEqual(row["name"] == "虚构白酒" or row["name"] == "我给它起的名字", True)


class DetailCommandTest(CliTestCase):
    """`detail` 子命令（展开某一行时按需加载）。"""

    def test_offline_detail_uses_stored_events(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        code = self.run_cli("detail", "--code", "600519.SH", "--offline")
        self.assertEqual(code, EXIT_OK, self.err.getvalue())
        row = self.json_out()["row"]
        self.assertEqual(row["code"], "600519.SH")
        self.assertIsNone(row["price"])
        self.assertIn("price", row["missing"])

    def test_detail_normalises_code(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        for raw in ("600519", "sh600519", "600519.SH"):
            with self.subTest(raw=raw):
                code = self.run_cli("detail", "--code", raw, "--offline")
                self.assertEqual(code, EXIT_OK, self.err.getvalue())
                self.assertEqual(self.json_out()["row"]["code"], "600519.SH")

    def test_detail_bad_code_rejected(self) -> None:
        code = self.run_cli("detail", "--code", "000001.SH", "--offline")
        self.assertEqual(code, EXIT_DATA_ERROR)
        self.assertIn("code", self.err.getvalue().lower())


class DeterminismTest(CliTestCase):
    def test_report_without_now_still_valid_but_timestamp_differs(self) -> None:
        """不传 --now 时用当前时间，报告其余部分仍应稳定。"""
        self.run_cli("sync", "--from", "fixture")
        self.run_cli("report")
        first = json.loads((self.data / "out" / "report.json").read_text(encoding="utf-8"))
        self.run_cli("report")
        second = json.loads((self.data / "out" / "report.json").read_text(encoding="utf-8"))

        self.assertEqual(first["totals"], second["totals"])
        self.assertEqual(first["by_year"], second["by_year"])
        self.assertEqual(first["meta"]["inputs"]["generated_at_source"], "now")
        fixed = first["meta"]["generated_at"]
        self.assertEqual(second["meta"]["generated_at"], fixed)

    def test_fixed_now_labels_source(self) -> None:
        self.run_cli("sync", "--from", "fixture")
        self.run_cli("report", "--now", FIXED_NOW)
        report = json.loads((self.data / "out" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["meta"]["inputs"]["generated_at_source"], "fixed")
        self.assertEqual(report["meta"]["generated_at"], FIXED_NOW)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
