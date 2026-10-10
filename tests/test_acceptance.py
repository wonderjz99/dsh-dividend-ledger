"""端到端验收测试（PLAN.md §1.3）。

本文件证明的是**两个语言之间的接缝**能对上：

1. TypeScript 层实际发出的那条命令字符串（哨兵包裹 + `-m dividend_ledger`），
   用真解释器、真 CLI 跑起来能拿到干净 JSON —— 这是纯离线测试无法替代的一步。
2. 「同一份输入跑两次，输出逐字节一致」。
3. 全程零网络。

命令字符串是从 `plugin/src/python.ts` 的 `withSentinel` / `buildCommand` **抄过来**的，
不是重新发明一份；TS 侧有单测锁定它，这里锁定它真的能跑。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "data"

SENTINEL_BEGIN = "__DIVIDEND_LEDGER_JSON_BEGIN__"
SENTINEL_END = "__DIVIDEND_LEDGER_JSON_END__"

FIXED_NOW = "2026-10-10T12:00:00+08:00"

#: 与 plugin/src/python.ts 的 withSentinel() 保持一致
SENTINEL_TEMPLATE = (
    "printf '%s\\n' '{begin}'; {command}; __dl_status=$?; "
    "printf '%s\\n' '{end}'; exit $__dl_status"
)


def python_executable() -> str:
    """挑一个 3.11+ 的解释器；核心只用标准库，所以任何 3.11+ 都行。"""
    for candidate in ("python3.12", "python3.11", "python3"):
        found = shutil.which(candidate)
        if found is None:
            continue
        if sys.version_info >= (3, 11) and candidate == "python3":
            version = subprocess.run(
                [found, "-c", "import sys; print(sys.version_info >= (3,11))"],
                capture_output=True,
                text=True,
                check=False,
            )
            if version.stdout.strip() != "True":
                continue
        return found
    raise unittest.SkipTest("找不到 python3.11+ 解释器")


def quote(value: str) -> str:
    """与 TS 侧 shellQuote 等价的 POSIX 单引号转义。"""
    return "'" + value.replace("'", "'\\''") + "'"


def build_sentinel_command(python: str, module_dir: Path, args: list[str]) -> str:
    """复刻 TS 侧发出的完整命令字符串。"""
    inner = " ".join(quote(part) for part in [python, "-m", "dividend_ledger", *args])
    return SENTINEL_TEMPLATE.format(
        begin=SENTINEL_BEGIN, command=inner, end=SENTINEL_END
    )


class UpcomingIsolationTest(unittest.TestCase):
    """阶段三铁律的端到端守护：**待收绝不进历史累计**。

    单元测试已经覆盖了逻辑层，这里用真实解释器跑完整链路再确认一次——
    因为"历史合计变大一点"这种失真不会报错，只有对着数字才发现。
    """

    @classmethod
    def setUpClass(cls) -> None:  # noqa: D102
        cls.python = python_executable()

    def setUp(self) -> None:  # noqa: D102
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        shutil.copytree(
            FIXTURE_DIR,
            self.data,
            ignore=shutil.ignore_patterns("out", "__pycache__", "events.json", "raw", "real"),
        )

    def tearDown(self) -> None:  # noqa: D102
        self._tmp.cleanup()

    def cli(self, *args: str) -> dict:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        completed = subprocess.run(
            [self.python, "-m", "dividend_ledger", "--data", str(self.data), *args],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode, 0, f"命令失败：{completed.stderr[:400]}"
        )
        return json.loads(completed.stdout)

    def report(self) -> dict:
        self.cli("sync", "--from", "fixture")
        self.cli("report", "--now", "2026-10-10T00:00:00+08:00")
        return json.loads((self.data / "out" / "report.json").read_text(encoding="utf-8"))

    def test_totals_exclude_upcoming(self) -> None:
        """fixture 里有 2 条未实施事件；历史合计必须仍是 22 条已实施里的 19 笔。"""
        report = self.report()
        self.assertEqual(report["totals"]["cash_amount"], "26856.40")
        self.assertEqual(report["totals"]["event_count"], 19)

    def test_upcoming_block_is_populated(self) -> None:
        report = self.report()
        upcoming = report["upcoming"]
        self.assertEqual(upcoming["eventCount"], 2)
        self.assertEqual(upcoming["confirmedAmount"], "2591.10")
        self.assertEqual(upcoming["proposedAmount"], "3600.00")

    def test_sum_of_parts_does_not_equal_totals(self) -> None:
        """把待收加进历史会得到 33047.50 —— 显式断言**不是**这个值。"""
        report = self.report()
        contaminated = Decimal(report["totals"]["cash_amount"]) + Decimal(
            report["upcoming"]["totalAmount"]
        )
        self.assertEqual(contaminated, Decimal("33047.50"))
        self.assertNotEqual(report["totals"]["cash_amount"], "33047.50")

    def test_report_is_reproducible_with_upcoming(self) -> None:
        """带待收区块时输出仍要逐字节可复现。"""
        self.cli("sync", "--from", "fixture")
        self.cli("report", "--now", "2026-10-10T00:00:00+08:00")
        first = (self.data / "out" / "report.json").read_bytes()
        self.cli("report", "--now", "2026-10-10T00:00:00+08:00")
        self.assertEqual((self.data / "out" / "report.json").read_bytes(), first)

    def test_future_window_reaches_announced_plans(self) -> None:
        """取数窗口必须向未来延伸，否则已公告未实施的方案一条都进不来。"""
        self.cli("sync", "--from", "fixture")
        store = json.loads((self.data / "events.json").read_text(encoding="utf-8"))
        statuses = [e["status"] for e in store["events"] if "status" in e]
        self.assertIn("confirmed", statuses)
        self.assertIn("proposed", statuses)


class EndToEndTest(unittest.TestCase):
    """用真实解释器执行 TS 层发出的命令。"""

    @classmethod
    def setUpClass(cls) -> None:  # noqa: D102
        cls.python = python_executable()

    def setUp(self) -> None:  # noqa: D102
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        shutil.copytree(
            FIXTURE_DIR,
            self.data,
            ignore=shutil.ignore_patterns(
                "out", "__pycache__", "events.json", "raw", "real"
            ),
        )

    def tearDown(self) -> None:  # noqa: D102
        self._tmp.cleanup()

    def run_sentinel(self, *args: str) -> tuple[int, str, str, str]:
        """跑一条哨兵包裹命令，返回 (退出码, stdout, stderr, 哨兵内 JSON)。"""
        command = build_sentinel_command(self.python, REPO_ROOT, ["--data", str(self.data), *args])
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        completed = subprocess.run(
            ["bash", "-c", command],
            cwd=str(self.data),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        stdout = completed.stdout
        begin = stdout.find(SENTINEL_BEGIN)
        end = stdout.find(SENTINEL_END, begin + len(SENTINEL_BEGIN)) if begin >= 0 else -1
        payload = stdout[begin + len(SENTINEL_BEGIN) : end].strip() if begin >= 0 and end >= 0 else ""
        return completed.returncode, stdout, completed.stderr, payload

    # ----------------------------------------------------------------- #

    def test_sync_then_report_through_sentinel(self) -> None:
        """完整链路：sync → report，全程走 TS 层发出的那条命令。"""
        code, _, stderr, payload = self.run_sentinel("sync", "--from", "fixture")
        self.assertEqual(code, 0, f"sync 失败：{stderr}")
        synced = json.loads(payload)
        self.assertTrue(synced["ok"])
        self.assertEqual(synced["import"]["imported"], 24)

        code, _, stderr, payload = self.run_sentinel("report", "--now", FIXED_NOW)
        self.assertEqual(code, 0, f"report 失败：{stderr}")
        report = json.loads(payload)
        self.assertTrue(report["ok"])

        # 与手工核算的数字对齐（见 tests/test_report.py 的同名断言）
        totals = report["totals"]
        self.assertEqual(totals["cash_amount"], "26856.40")
        self.assertEqual(totals["cost_basis"], "482600.00")
        self.assertEqual(totals["event_count"], 19)
        self.assertEqual(totals["stock_count"], 6)

    def test_sentinel_payload_is_pure_json(self) -> None:
        """哨兵取出的那段必须是**可直接解析的纯 JSON**，不能混进别的输出。"""
        self.run_sentinel("sync", "--from", "fixture")
        _, stdout, _, payload = self.run_sentinel("report", "--now", FIXED_NOW)
        json.loads(payload)  # 不抛错即为通过
        # 哨兵之外不应有别的噪声行
        outside = stdout.replace(SENTINEL_BEGIN, "").replace(SENTINEL_END, "").replace(payload, "")
        self.assertEqual(outside.strip(), "", f"哨兵外出现了额外输出：{outside!r}")

    def test_nonzero_exit_keeps_sentinel_and_reports_reason(self) -> None:
        """出错时退出码要透出，原因要在 stderr 里——TS 层就是靠这两条判断的。"""
        code, _, stderr, _ = self.run_sentinel("report")
        self.assertEqual(code, 1, "缺 events.json 时应以退出码 1 结束")
        self.assertIn("events.json", stderr)

    def test_help_exit_code_is_two(self) -> None:
        """用法错误是 2，与数据错误（1）区分开。"""
        code, _, stderr, _ = self.run_sentinel("no-such-command")
        self.assertEqual(code, 2)
        self.assertNotEqual(stderr.strip(), "")

    def test_data_dir_with_spaces_and_quote(self) -> None:
        """路径里有空格和单引号时，转义必须站得住。"""
        weird = Path(self._tmp.name) / "it's a data dir"
        shutil.copytree(self.data, weird)
        command = build_sentinel_command(
            self.python, REPO_ROOT, ["--data", str(weird), "sync", "--from", "fixture"]
        )
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        completed = subprocess.run(
            ["bash", "-c", command], env=env, capture_output=True, text=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(SENTINEL_BEGIN, completed.stdout)


class ByteIdentityTest(unittest.TestCase):
    """PLAN §1.3 的第一条验收标准：连跑两次逐字节一致。"""

    @classmethod
    def setUpClass(cls) -> None:  # noqa: D102
        cls.python = python_executable()

    def setUp(self) -> None:  # noqa: D102
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        shutil.copytree(
            FIXTURE_DIR,
            self.data,
            ignore=shutil.ignore_patterns(
                "out", "__pycache__", "events.json", "raw", "real"
            ),
        )

    def tearDown(self) -> None:  # noqa: D102
        self._tmp.cleanup()

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        return subprocess.run(
            [self.python, "-m", "dividend_ledger", "--data", str(self.data), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_report_is_byte_identical(self) -> None:
        self.assertEqual(self.cli("sync", "--from", "fixture").returncode, 0)

        self.assertEqual(self.cli("report", "--now", FIXED_NOW).returncode, 0)
        report_first = (self.data / "out" / "report.json").read_bytes()
        ledger_first = (self.data / "out" / "ledger.json").read_bytes()

        self.assertEqual(self.cli("report", "--now", FIXED_NOW).returncode, 0)
        self.assertEqual((self.data / "out" / "report.json").read_bytes(), report_first)
        self.assertEqual((self.data / "out" / "ledger.json").read_bytes(), ledger_first)

    def test_repeated_sync_is_byte_identical(self) -> None:
        self.assertEqual(self.cli("sync", "--from", "fixture").returncode, 0)
        first = (self.data / "events.json").read_bytes()
        self.assertEqual(self.cli("sync", "--from", "fixture").returncode, 0)
        self.assertEqual((self.data / "events.json").read_bytes(), first)


class OfflineGuaranteeTest(unittest.TestCase):
    """SPEC 的硬要求：一阶段全程不联网。"""

    def test_no_network_calls_in_core(self) -> None:
        """核心源码里不允许出现任何网络客户端。"""
        forbidden = (
            "import socket",
            "import urllib",
            "import http.client",
            "import requests",
            "from socket",
            "from urllib",
            "urlopen(",
        )
        offenders: list[str] = []
        for path in sorted((REPO_ROOT / "dividend_ledger").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")
        self.assertEqual(offenders, [], f"核心不应包含网络调用：{offenders}")

    def test_core_imports_only_stdlib(self) -> None:
        """零第三方依赖：所有 import 都必须落在标准库或本包内。"""
        allowed_prefixes = ("dividend_ledger", "__future__")
        stdlib = set(sys.stdlib_module_names)
        offenders: list[str] = []

        for path in sorted((REPO_ROOT / "dividend_ledger").rglob("*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped.startswith(("import ", "from ")):
                    continue
                module = stripped.split()[1].split(".")[0]
                if module in stdlib or module.startswith(allowed_prefixes):
                    continue
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {stripped}")

        self.assertEqual(offenders, [], f"核心出现了非标准库依赖：{offenders}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
