"""命令行入口。

设计原则（SPEC §7.1）：**JSON 是权威产物，CLI 只是它的一层薄壳**。
所以每个子命令默认输出 JSON；``--text`` 才渲染成人读的一段话。
TS 适配层调用本 CLI 时一律走 JSON。

退出码约定：

- ``0`` 成功
- ``1`` 数据/校验错误（消息可直接给用户看）
- ``2`` 命令行用法错误（argparse 默认）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn, Sequence

from dividend_ledger import __version__
from dividend_ledger.codes import normalize_code
from dividend_ledger.decimals import format_money
from dividend_ledger.errors import DataError, DividendLedgerError
from dividend_ledger.jsonio import dump_json_atomic, load_json, render_json
from dividend_ledger.ledger import build_ledger
from dividend_ledger.metrics import compute_metrics, compute_portfolio, group_events_by_code
from dividend_ledger.upcoming import build_upcoming
from dividend_ledger.pricecache import load_price_cache
from dividend_ledger.models import DATE_FORMAT, DividendEvent, Holding
from dividend_ledger.providers import (
    FixtureProvider,
    StockMatch,
    WestockProvider,
    enrich_close_prev,
    make_cli_runner,
    make_kline_runner,
    make_price_runner,
    make_search_runner,
    parse_kline_markdown,
)
from dividend_ledger.render import (
    render_holdings_text,
    render_import_text,
    render_report_text,
)
from dividend_ledger.report import ReportContext, build_report
from dividend_ledger.store import (
    DataPaths,
    apply_pay_date_overrides,
    import_events,
    import_holdings,
    load_events,
    load_holdings,
    load_pay_date_overrides,
    replace_events,
    replace_holdings,
)

__all__ = ["main"]

#: 报告时间戳的默认时区：A 股场景一律用东八区
_CN_TZ = timezone(timedelta(hours=8))

EXIT_OK = 0
EXIT_DATA_ERROR = 1


class _CliError(Exception):
    """带退出码的 CLI 错误。"""

    def __init__(self, message: str, exit_code: int = EXIT_DATA_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="dividend-ledger",
        description="A 股分红账本：由持仓与分红方案推算应得分红（离线核心）",
        epilog=(
            "注意：全局开关（--data / --json / --text）要写在子命令之前，"
            "例如 dividend-ledger --text report"
        ),
    )
    parser.add_argument("--version", action="version", version=f"dividend-ledger {__version__}")
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("data"),
        help="数据目录，默认 ./data（写在子命令之前）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="强制 JSON 输出（默认：除 report/show 外均为 JSON）",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="输出人读文本而非 JSON",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_validate(subparsers)
    _add_holdings(subparsers)
    _add_sync(subparsers)
    _add_report(subparsers)
    _add_candidates(subparsers)
    _add_stock_detail(subparsers)
    _add_portfolio(subparsers)

    return parser


def _add_validate(subparsers: Any) -> None:
    parser = subparsers.add_parser("validate", help="校验数据文件是否可解析")
    parser.set_defaults(handler=_handle_validate)


def _add_holdings(subparsers: Any) -> None:
    parser = subparsers.add_parser("holdings", help="查看或写入持仓")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--set",
        dest="set_path",
        type=str,
        default=None,
        metavar="PATH",
        help="从 JSON 文件写入持仓；用 - 表示从 stdin 读",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="整份替换持仓（面板的删除/覆盖用它；默认是按 code 合并）",
    )
    parser.set_defaults(handler=_handle_holdings)


def _add_sync(subparsers: Any) -> None:
    parser = subparsers.add_parser("sync", help="同步分红事件")
    parser.add_argument(
        "--from",
        dest="source",
        choices=("fixture", "westock"),
        default="fixture",
        help="数据来源，默认 fixture（离线）",
    )
    parser.add_argument(
        "--events-file",
        type=Path,
        default=None,
        help="fixture 来源的事件文件，默认 <data>/events.fixture.json",
    )
    parser.add_argument(
        "--payload-file",
        type=Path,
        default=None,
        help="westock 来源的 Markdown 响应文件（离线解析，不联网）",
    )
    parser.add_argument(
        "--westock-bin",
        type=str,
        default="westock",
        help="westock CLI 可执行文件（真实联网取数时使用），默认 westock",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=10,
        help="联网取数时回溯年数，默认 10",
    )
    parser.add_argument(
        "--no-all",
        action="store_true",
        help="联网取数时不带 --all（默认带，以取得未实施方案）",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=8.0,
        help="联网取数时每只股票之间的间隔秒数，默认 8（实测连续请求会限频）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="命中限频或空输出时的重试次数，默认 3",
    )
    parser.add_argument(
        "--future-days",
        type=int,
        default=400,
        help=(
            "取数窗口向未来延伸的天数，默认 400（约 13 个月）。"
            "必须大于 0，否则取不到已公告未实施的方案（阶段三的『待收』）"
        ),
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="先清空 events.json 再写入（换数据源时用；默认是幂等合并）",
    )
    parser.add_argument(
        "--close-prev",
        action="store_true",
        help="强制补除权前收盘价（离线来源下需要配合 --kline-file）",
    )
    parser.add_argument(
        "--no-close-prev",
        action="store_true",
        help="不补除权前收盘价（省一半请求，但股息率会为空）",
    )
    parser.add_argument(
        "--kline-file",
        type=Path,
        action="append",
        default=None,
        help="离线补收盘价用的 K 线 Markdown 文件；按持仓代码顺序可重复传",
    )
    parser.set_defaults(handler=_handle_sync)


def _add_report(subparsers: Any) -> None:
    parser = subparsers.add_parser("report", help="生成明细账与三分区汇总")
    parser.add_argument("--out", type=Path, default=None, help="报告输出路径")
    parser.add_argument("--ledger-out", type=Path, default=None, help="明细账输出路径")
    parser.add_argument(
        "--overrides",
        type=Path,
        default=None,
        help="到账日人工覆盖表路径，默认 <data>/pay_dates.json（存在才生效）",
    )
    parser.add_argument(
        "--now",
        type=str,
        default=None,
        help="固定报告时间戳（ISO 8601），用于可复现/可 diff 的输出",
    )
    parser.set_defaults(handler=_handle_report)


def _add_candidates(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "candidates",
        help="给界面用的搜索候选：命中股票 + 动态股息率 + 预期股息率",
    )
    parser.add_argument("--query", type=str, required=True, help="搜索关键词（代码或名称）")
    parser.add_argument(
        "--limit", type=int, default=10, help="最多返回多少条候选，默认 10"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="只用本地已抓事件计算，不联网（价格为 null，股息率也为 null）",
    )
    parser.add_argument(
        "--westock-bin", type=str, default="westock", help="westock CLI 路径"
    )
    parser.add_argument(
        "--price-limit",
        type=int,
        default=5,
        help="最多为几条候选联网取价（限频很紧），默认 5",
    )
    parser.set_defaults(handler=_handle_candidates)


def _add_stock_detail(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "detail",
        help="单只股票的界面指标（展开某一行时按需加载）",
    )
    parser.add_argument("--code", type=str, required=True, help="证券代码")
    parser.add_argument("--name", type=str, default="", help="证券名称（选填）")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="联网抓取该股近 1 年分红并落盘（默认只用本地已有数据）",
    )
    parser.add_argument(
        "--offline", action="store_true", help="完全离线（等价于不传 --refresh）"
    )
    parser.add_argument("--westock-bin", type=str, default="westock", help="westock CLI 路径")
    parser.set_defaults(handler=_handle_detail)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 主入口，返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = DataPaths(root=Path(args.data))
    try:
        return int(args.handler(args, paths))
    except _CliError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return exc.exit_code
    except DividendLedgerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_DATA_ERROR
    except OSError as exc:
        print(f"文件操作失败：{exc}", file=sys.stderr)
        return EXIT_DATA_ERROR


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


def _emit(args: argparse.Namespace, payload: Any, text: str) -> None:
    """按 ``--text`` 决定输出 JSON 还是人读文本。"""
    if getattr(args, "text", False) and not getattr(args, "json", False):
        print(text)
    else:
        sys.stdout.write(render_json(payload))


def _now_iso(raw: str | None) -> tuple[str, str]:
    """返回 ``(时间戳, 来源说明)``。"""
    if raw is None:
        return datetime.now(_CN_TZ).isoformat(timespec="seconds"), "now"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise _CliError(f"--now 不是合法 ISO 8601 时间：{raw!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CN_TZ)
    return parsed.isoformat(timespec="seconds"), "fixed"


# --------------------------------------------------------------------------- #
# 各子命令实现
# --------------------------------------------------------------------------- #


def _handle_validate(args: argparse.Namespace, paths: DataPaths) -> int:
    holdings, holdings_rejected = load_holdings(paths.holdings, required=False)
    events, events_rejected = load_events(paths.events, required=False)
    _, override_rejected = load_pay_date_overrides(paths.pay_dates)

    problems: list[str] = []
    if not paths.holdings.exists():
        problems.append(f"缺少持仓文件：{paths.holdings}")
    if not paths.events.exists():
        problems.append(f"缺少分红事件文件：{paths.events}")

    payload = {
        "ok": not problems and not (holdings_rejected or events_rejected or override_rejected),
        "data_dir": str(paths.root),
        "holdings": {
            "path": str(paths.holdings),
            "count": len(holdings),
            "rejected_count": len(holdings_rejected),
            "rejected": [item.to_dict() for item in holdings_rejected],
        },
        "events": {
            "path": str(paths.events),
            "count": len(events),
            "rejected_count": len(events_rejected),
            "rejected": [item.to_dict() for item in events_rejected],
        },
        "pay_dates": {
            "path": str(paths.pay_dates),
            "exists": paths.pay_dates.exists(),
            "rejected_count": len(override_rejected),
            "rejected": [item.to_dict() for item in override_rejected],
        },
        "problems": problems,
    }

    text_lines = [
        f"数据目录：{paths.root}",
        f"  持仓      {len(holdings)} 条"
        + (f"（拒绝 {len(holdings_rejected)}）" if holdings_rejected else ""),
        f"  分红事件  {len(events)} 条"
        + (f"（拒绝 {len(events_rejected)}）" if events_rejected else ""),
        f"  覆盖表    {'存在' if paths.pay_dates.exists() else '不存在（可选）'}",
    ]
    if problems:
        text_lines.append("")
        text_lines.extend(f"  问题：{item}" for item in problems)
    if holdings_rejected or events_rejected or override_rejected:
        text_lines.append("")
        for item in (*holdings_rejected, *events_rejected, *override_rejected):
            text_lines.append(f"  拒绝：{item.reason}")

    _emit(args, payload, "\n".join(text_lines))
    return EXIT_OK if payload["ok"] else EXIT_DATA_ERROR


def _handle_holdings(args: argparse.Namespace, paths: DataPaths) -> int:
    if args.set_path is None:
        holdings, rejected = load_holdings(paths.holdings, required=True)
        payload = {
            "ok": True,
            "path": str(paths.holdings),
            "count": len(holdings),
            "holdings": [item.to_dict() for item in holdings],
            "rejected": [item.to_dict() for item in rejected],
        }
        _emit(args, payload, render_holdings_text(holdings))
        return EXIT_OK

    incoming = _read_holdings_input(args.set_path)
    # 面板的"删除"与"整份覆盖"需要替换语义：合并（upsert）表达不了删除
    audit = (
        replace_holdings(paths.holdings, incoming)
        if args.replace
        else import_holdings(paths.holdings, incoming)
    )
    payload = {"ok": True, "path": str(paths.holdings), **audit.to_dict()}
    _emit(args, payload, render_import_text(audit.to_dict(), label="持仓"))
    return EXIT_OK


def _read_holdings_input(spec: str) -> list[Holding]:
    """从文件或 stdin 读取待导入的持仓。"""
    if spec == "-":
        raw = sys.stdin.read()
        if not raw.strip():
            raise _CliError("stdin 为空，没有可导入的持仓")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _CliError(f"stdin 不是合法 JSON：第 {exc.lineno} 行 {exc.msg}") from None
        source_label = "stdin"
    else:
        path = Path(spec)
        payload = load_json(path, required=True)
        source_label = str(path)

    if isinstance(payload, list):
        records: list[Any] = list(payload)
    elif isinstance(payload, dict) and isinstance(payload.get("holdings"), list):
        records = list(payload["holdings"])
    else:
        raise _CliError(
            f"{source_label} 顶层必须是持仓数组，或 {{\"holdings\": [...]}} 信封"
        )

    holdings: list[Holding] = []
    problems: list[str] = []
    for index, raw in enumerate(records):
        try:
            holdings.append(Holding.from_dict(raw, where=f"holdings[{index}]"))
        except DividendLedgerError as exc:
            problems.append(f"[{index}] {exc}")

    if problems:
        preview = "\n".join(f"  {item}" for item in problems[:10])
        more = f"\n  …还有 {len(problems) - 10} 条" if len(problems) > 10 else ""
        raise _CliError(
            f"{source_label} 中有 {len(problems)} 条持仓无法解析，已全部拒绝导入：\n"
            f"{preview}{more}"
        )
    # 注意：**空数组是合法的**——那表示"把持仓清空"（面板上的删除最后一只就走这里）。
    # 只有当文件里**一条记录都没有**（不是数组为空，而是根本没给出数组）时才报错，
    # 那种情况几乎一定是文件写坏了。
    return holdings


def _handle_sync(args: argparse.Namespace, paths: DataPaths) -> int:
    holdings, _ = load_holdings(paths.holdings, required=True)
    if not holdings:
        raise _CliError(f"{paths.holdings} 里没有持仓，无从取分红事件")

    codes = sorted({item.code for item in holdings})
    start = min(item.start_date for item in holdings)
    # 窗口必须**向前延伸**：阶段三要的"待收"就是除权除息日在未来的已公告方案，
    # 窗口截止到今天会把它们全部丢掉（实测：fixture 里 2027 年的两笔一条都进不来）。
    # 这个 bug 很隐蔽——历史合计完全正常，只是"待收"永远为空。
    end = _today() + timedelta(days=max(0, args.future_days))

    if args.source == "fixture":
        events_file = args.events_file or (paths.root / "events.fixture.json")
        provider = FixtureProvider(Path(events_file))
    else:
        payload_file = args.payload_file
        if payload_file is not None:
            # 离线：解析已落盘的 Markdown，不联网
            provider = WestockProvider(_make_file_runner(Path(payload_file)))
        else:
            # 联网：真正调用 westock CLI。注意 CLI 在限频时退出码仍为 0，
            # 靠解析器识别提示文本，所以这里不预检退出码。
            provider = WestockProvider(
                make_cli_runner(
                    binary=args.westock_bin,
                    sleep_seconds=max(0.0, args.sleep),
                    years=args.years,
                    request_all=not args.no_all,
                    # 进度写 stderr，不污染 stdout 的 JSON
                    retries=max(0, args.retries),
                    on_progress=lambda message: print(message, file=sys.stderr),
                ),
                years=args.years,
            )

    result = provider.list_events(codes, start, end)
    events = list(result.events)

    # 补齐除权前收盘价（决定股息率能不能算）。两个约束：
    #   1. K 线只是"锦上添花"：取不到就不补，不让整次同步失败；
    #   2. **只有真实联网取数才默认开启**——fixture 是离线路径，
    #      绝不能因为"顺手补个价格"就偷偷发网络请求（SPEC 的零网络承诺）。
    going_live = args.source == "westock" and args.payload_file is None
    want_close_prev = args.close_prev or (going_live and not args.no_close_prev)
    if args.no_close_prev:
        want_close_prev = False

    close_prev_stats: dict[str, Any] = {"requested": want_close_prev, "filled": 0}
    if want_close_prev:
        try:
            if args.kline_file is not None and not going_live:
                kline_files = args.kline_file if isinstance(args.kline_file, list) else [args.kline_file]
                closes = _load_kline_files([Path(item) for item in kline_files], codes)
            else:
                kline_runner = make_kline_runner(
                    binary=args.westock_bin,
                    sleep_seconds=max(0.0, args.sleep),
                    retries=max(0, args.retries),
                    on_progress=lambda message: print(message, file=sys.stderr),
                )
                closes = kline_runner(codes, start, end)
            events, filled = enrich_close_prev(events, closes)
            close_prev_stats["filled"] = filled
            close_prev_stats["stocks_with_kline"] = len(closes)
        except (DividendLedgerError, OSError) as exc:
            close_prev_stats["error"] = str(exc)
            print(f"警告：补齐除权前收盘价失败，股息率将为空：{exc}", file=sys.stderr)

    audit = (
        replace_events(paths.events, events)
        if args.replace
        else import_events(paths.events, events)
    )

    upcoming = [event for event in events if event.is_future]
    payload = {
        "ok": True,
        "source": args.source,
        "provider": dict(result.provenance or {}),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "codes": codes,
        "provider_rejected_count": len(result.rejected),
        "provider_rejected": [item.to_dict() for item in result.rejected],
        "close_prev": close_prev_stats,
        "upcoming_count": len(upcoming),
        "upcoming": [
            {
                "code": event.code,
                "ex_date": event.ex_date.isoformat(),
                "status": event.status.value,
                "dividend_per_share": str(event.dividend_per_share),
                "plan_text": event.plan_text,
            }
            for event in sorted(upcoming, key=lambda item: (item.ex_date, item.code))
        ],
        "import": audit.to_dict(),
        "events_path": str(paths.events),
    }

    text = render_import_text(audit.to_dict(), label="分红事件")
    if upcoming:
        text += f"\n\n已公告未实施（待收）{len(upcoming)} 笔："
        for event in sorted(upcoming, key=lambda item: item.ex_date)[:5]:
            text += (
                f"\n  {event.ex_date.isoformat()}  {event.code}  "
                f"每股 {event.dividend_per_share}  [{event.status.value}]"
            )
        if len(upcoming) > 5:
            text += f"\n  …还有 {len(upcoming) - 5} 笔，见 JSON 输出"
    if result.rejected:
        text += f"\n\n来源侧跳过 {len(result.rejected)} 行（未实施/取消/无派现等）："
        for item in result.rejected[:5]:
            text += f"\n  [{item.index}] {item.reason}"
        if len(result.rejected) > 5:
            text += f"\n  …还有 {len(result.rejected) - 5} 行，见 JSON 输出"

    _emit(args, payload, text)
    return EXIT_OK


def _fetch_prices_with_cache(
    paths: DataPaths, codes: Sequence[str], *, bin_name: str, today: date
) -> tuple[dict[str, Decimal], str | None, dict[str, int]]:
    """带**按日缓存**的取价。

    收盘价一天只变一次，且 westock 限频很紧（runner 里每只之间要 sleep），
    所以命中缓存就不发请求——面板反复打开不会反复等 16 秒。

    Returns:
        ``(价格表, 错误消息, {"hit": n, "miss": n})``
    """
    cache = load_price_cache(paths.root)
    cache.drop_stale(today=today)

    prices: dict[str, Decimal] = {}
    missing: list[str] = []
    for code in codes:
        cached = cache.get(code, today=today)
        if cached is not None:
            prices[code] = cached
        else:
            missing.append(code)

    stats = {"hit": len(prices), "miss": len(missing)}
    error: str | None = None
    if missing:
        try:
            fetched = make_price_runner(binary=bin_name)(missing)
        except DividendLedgerError as exc:
            error = str(exc)
            fetched = {}
        prices.update(fetched)
        for code, value in fetched.items():
            cache.put(code, value, today=today)
        try:
            cache.save()
        except DataError:
            # 缓存写不进去不该让功能不可用
            pass
    return prices, error, stats


def _load_kline_files(
    paths: Sequence[Path], codes: Sequence[str]
) -> dict[str, dict[date, Decimal]]:
    """离线加载 K 线文件。

    每个文件可能是任意一只股票的 K 线，文件里**没有代码字段**（真实输出不含代码），
    所以按顺序与 ``codes`` 一一对应。数量不匹配时明确报错，不猜。
    """
    if len(paths) != len(codes):
        raise DataError(
            f"--kline-file 数量（{len(paths)}）与股票数（{len(codes)}）不一致；"
            f"请按 {list(codes)} 的顺序逐个传入"
        )
    result: dict[str, dict[date, Decimal]] = {}
    for code, path in zip(codes, paths):
        if not path.exists():
            raise DataError(f"找不到 K 线文件：{path}")
        result[code] = parse_kline_markdown(path.read_text(encoding="utf-8"))
    return result


def _make_file_runner(path: Path) -> Any:
    """构造"从本地文件读 westock Markdown 响应"的 runner（离线测试用）。"""

    def runner(codes: Sequence[str], start: date, end: date) -> Any:
        if not path.exists():
            raise DataError(f"找不到 westock 响应文件：{path}")
        return {"text": path.read_text(encoding="utf-8"), "codes": list(codes)}

    return runner


def _handle_report(args: argparse.Namespace, paths: DataPaths) -> int:
    holdings, holdings_rejected = load_holdings(paths.holdings, required=True)
    events, events_rejected = load_events(paths.events, required=True)

    warnings: list[str] = []
    if holdings_rejected:
        warnings.append(
            f"持仓文件有 {len(holdings_rejected)} 条被拒绝记录（见 validate 输出）"
        )
    if events_rejected:
        warnings.append(
            f"事件文件有 {len(events_rejected)} 条被拒绝记录（见 validate 输出）"
        )

    # 到账日人工覆盖
    overrides_path = args.overrides or paths.pay_dates
    overrides, override_rejected = load_pay_date_overrides(Path(overrides_path))
    override_stats: dict[str, Any] = {}
    if overrides:
        events, override_stats = apply_pay_date_overrides(events, overrides)
        unmatched = override_stats.get("overrides_unmatched") or []
        if unmatched:
            warnings.append(
                "以下到账日覆盖没有匹配到任何分红事件，未生效："
                + "、".join(str(item) for item in unmatched)
            )
    if override_rejected:
        warnings.append(
            f"覆盖表有 {len(override_rejected)} 条无法解析，已忽略："
            + "；".join(item.reason for item in override_rejected[:3])
        )

    if not holdings:
        raise _CliError(f"{paths.holdings} 里没有持仓，无法生成报告")

    ledger = build_ledger(holdings, events)
    generated_at, stamp_source = _now_iso(args.now)

    context = ReportContext(
        generated_at=generated_at,
        inputs={
            **paths.describe(),
            "overrides_path": str(overrides_path) if overrides else "",
            "generated_at_source": stamp_source,
        },
        audit={
            "holdings_rejected": len(holdings_rejected),
            "events_rejected": len(events_rejected),
            "pay_dates": override_stats,
        },
        warnings=tuple(warnings),
        upcoming=build_upcoming(events, holdings),
    )
    report = build_report(ledger, context=context)

    ledger_out = Path(args.ledger_out) if args.ledger_out else paths.ledger
    report_out = Path(args.out) if args.out else paths.report

    dump_json_atomic(
        ledger_out,
        ledger.to_dict(meta={"generated_at": generated_at, "inputs": context.inputs}),
    )
    dump_json_atomic(report_out, report)

    payload = {
        "ok": True,
        "report_path": str(report_out),
        "ledger_path": str(ledger_out),
        "totals": report["totals"],
        "meta": report["meta"],
    }
    _emit(args, payload, render_report_text(report, ledger=ledger))
    return EXIT_OK


def _handle_candidates(args: argparse.Namespace, paths: DataPaths) -> int:
    """搜索候选 + 逐股指标。

    设计取舍：**只对有本地分红数据的候选联网取价**。理由是两个限频都很紧：
    `westock search` 与 `westock kline` 各占一次请求，若对 10 条候选都取价，
    一次搜索要 11 次请求，必然触发限频。没有分红数据的股票本来也算不出股息率。
    """
    query = str(args.query).strip()
    if query == "":
        raise _CliError("--query 不能为空")

    holdings, _ = load_holdings(paths.holdings, required=False)
    holding_by_code = {item.code: item for item in holdings}
    events, _ = load_events(paths.events, required=False)
    by_code = group_events_by_code(events)

    matches: list[Any] = []
    search_error: str | None = None
    if not args.offline:
        try:
            search_runner = make_search_runner(binary=args.westock_bin)
            matches = search_runner(query)
        except DividendLedgerError as exc:
            search_error = str(exc)
    else:
        # 离线：只按本地已知代码/名称做包含匹配。
        # 注意：本地已知代码的并集里，有些只在 events 里出现过而**不在持仓里**，
        # 所以取名字必须先判 None（这里曾经 AttributeError 过）。
        needle = query.lower()

        def known_name(code: str) -> str:
            holding = holding_by_code.get(code)
            return (holding.name if holding is not None and holding.name else "") or code

        matches = [
            StockMatch(code=code, name=known_name(code), raw_code=code)
            for code in sorted({*by_code, *holding_by_code})
            if needle in code.lower() or needle in known_name(code).lower()
        ]

    matches = matches[: max(0, args.limit)]

    # 只为"本地有分红数据"的候选取价
    priced: dict[str, Decimal] = {}
    price_error: str | None = None
    if not args.offline:
        to_price = [m.code for m in matches if by_code.get(m.code)][: max(0, args.price_limit)]
        if to_price:
            priced, price_error, _ = _fetch_prices_with_cache(
                paths, to_price, bin_name=args.westock_bin, today=_today()
            )

    today = _today()
    rows = [
        compute_metrics(
            code=match.code,
            name=match.name,
            price=priced.get(match.code),
            events=by_code.get(match.code, []),
            holding=holding_by_code.get(match.code),
            reference=today,
        ).to_dict()
        | {"kind": match.kind, "rawCode": match.raw_code}
        for match in matches
    ]

    payload = {
        "ok": True,
        "query": query,
        "count": len(rows),
        "rows": rows,
        "offline": bool(args.offline),
        "searchError": search_error,
        "priceError": price_error,
        "asOf": today.isoformat(),
        "dataDir": str(paths.root),
    }

    if args.text:
        lines = [f"搜索「{query}」：{len(rows)} 条（截至 {today.isoformat()}）", ""]
        for row in rows:
            dyn = row["dynamicYield"] or "—"
            exp = row["expectedYield"] or "—"
            lines.append(
                f"  {row['code']:<12}{row['name']:<12}价 {row['price'] or '—':<10}"
                f"动态 {dyn:<10}预期 {exp}"
            )
        if search_error:
            lines.append(f"\n  搜索出错：{search_error}")
        if not upcoming.is_empty:
            lines += ["", f"  待收 {len(upcoming.rows)} 笔（未实施，不计入上面的已收）"]
            for row in upcoming.rows:
                mark = "确定" if not row.is_proposed else "预案"
                lines.append(
                    f"    {row.month}  {row.code:<12}{row.name:<10}"
                    f"{row.per_share:>9} × {row.shares:>8} 股 = "
                    f"{format_money(row.cash_amount):>12}  [{mark}]"
                )
            lines.append(
                f"    合计 确定 {format_money(upcoming.confirmed_amount)}"
                f" / 预案 {format_money(upcoming.proposed_amount)}"
            )
        if price_error:
            lines.append(f"  取价出错：{price_error}")
        _emit(args, payload, "\n".join(lines))
    else:
        _emit(args, payload, "")

    return EXIT_OK


def _add_portfolio(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "portfolio",
        help="持仓逐股指标 + 组合总股息率（面板的持仓视图）",
    )
    parser.add_argument("--names", type=str, default="", help="代码→名称，JSON 对象字符串")
    parser.add_argument(
        "--offline", action="store_true", help="不联网（价格为 null，股息率也为 null）"
    )
    parser.add_argument("--westock-bin", type=str, default="westock", help="westock CLI 路径")
    parser.add_argument(
        "--price-limit",
        type=int,
        default=20,
        help="最多为多少只持仓取价，默认 20",
    )
    parser.set_defaults(handler=_handle_portfolio)


def _handle_detail(args: argparse.Namespace, paths: DataPaths) -> int:
    """单只股票的指标。**按需**才联网，用于面板里展开某一行。

    为什么要拆出来：搜索列表若对 10 条命中都取价，一次搜索 = 1 次 search +
    10 次 kline，必然触发限频。所以列表只返回"本地已有什么"，展开某一行时才
    为那一只补数据（1 次分红 + 1 次取价）。
    """
    try:
        code = normalize_code(str(args.code))
    except Exception as exc:
        raise _CliError(f"--code 非法：{exc}") from None

    holdings, _ = load_holdings(paths.holdings, required=False)
    holding = next((item for item in holdings if item.code == code), None)

    events, _ = load_events(paths.events, required=False)
    code_events = [event for event in events if event.code == code]
    name = (args.name or "").strip() or (holding.name if holding else "") or code

    fetched = False
    fetch_error: str | None = None
    if args.refresh and not args.offline:
        start = (
            holding.start_date
            if holding is not None
            else _today() - timedelta(days=365 * 2)
        )
        try:
            provider = WestockProvider(
                make_cli_runner(
                    binary=args.westock_bin,
                    sleep_seconds=0.0,
                    years=2,
                    on_progress=lambda message: print(message, file=sys.stderr),
                ),
                years=2,
            )
            result = provider.list_events([code], start, _today() + timedelta(days=400))
            if result.events:
                # 用 replace 语义合并该股票（其它股票不动）：先把老记录留下，
                # 交给 import_events 按自然键幂等覆盖
                import_events(paths.events, result.events)
                events, _ = load_events(paths.events, required=False)
                code_events = [event for event in events if event.code == code]
                fetched = True
        except DividendLedgerError as exc:
            fetch_error = str(exc)

    price: Decimal | None = None
    price_error: str | None = None
    if not args.offline:
        priced, price_error, _ = _fetch_prices_with_cache(
            paths, [code], bin_name=args.westock_bin, today=_today()
        )
        price = priced.get(code)

    metrics = compute_metrics(
        code=code,
        name=name,
        price=price,
        events=code_events,
        holding=holding,
        reference=_today(),
    )

    payload = {
        "ok": True,
        "row": metrics.to_dict() | {"name": name},
        "fetched": fetched,
        "fetchError": fetch_error,
        "priceError": price_error,
        "offline": bool(args.offline),
        "asOf": _today().isoformat(),
    }
    if args.text:
        row = payload["row"]
        _emit(
            args,
            payload,
            f"{row['code']} {row['name']}\n"
            f"  价格 {row['price'] or '—'}  动态股息率 {row['dynamicYield'] or '—'}"
            f"  预期股息率 {row['expectedYield'] or '—'}",
        )
    else:
        _emit(args, payload, "")
    return EXIT_OK


def _handle_portfolio(args: argparse.Namespace, paths: DataPaths) -> int:
    """持仓的逐股指标 + 组合合计。

    只需要**本地已抓到的**分红事件，外加一次取价；不抓分红（那是 sync 的事），
    所以不会把限频打满。
    """
    holdings, _ = load_holdings(paths.holdings, required=True)
    events, _ = load_events(paths.events, required=False)
    by_code = group_events_by_code(events)

    names: dict[str, str] = {}
    if isinstance(args.names, str) and args.names.strip():
        try:
            parsed = json.loads(args.names)
            if isinstance(parsed, dict):
                names = {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            raise _CliError("--names 必须是 JSON 对象字符串，如 {\"600519.SH\":\"贵州茅台\"}") from None
    for item in holdings:
        if item.name:
            names.setdefault(item.code, item.name)

    priced: dict[str, Decimal] = {}
    price_error: str | None = None
    price_stats = {"hit": 0, "miss": 0}
    if not args.offline and holdings:
        wanted = [item.code for item in holdings][: max(0, args.price_limit)]
        if wanted:
            priced, price_error, price_stats = _fetch_prices_with_cache(
                paths, wanted, bin_name=args.westock_bin, today=_today()
            )

    today = _today()
    metrics = [
        compute_metrics(
            code=item.code,
            name=names.get(item.code) or item.code,
            price=priced.get(item.code),
            events=by_code.get(item.code, []),
            holding=item,
            reference=today,
        )
        for item in holdings
    ]
    summary = compute_portfolio(metrics, holdings)
    # 待收单列——**不并入 totals**，界面上也分开展示
    upcoming = build_upcoming(events, holdings)

    payload = {
        "ok": True,
        "count": len(metrics),
        "rows": [item.to_dict() for item in metrics],
        "totals": summary.to_dict(),
        "upcoming": upcoming.to_dict(),
        "priceError": price_error,
        "priceCache": price_stats,
        "offline": bool(args.offline),
        "asOf": today.isoformat(),
        "dataDir": str(paths.root),
    }

    if args.text:
        lines = [f"持仓 {len(metrics)} 只（截至 {today.isoformat()}）", ""]
        for row in payload["rows"]:
            lines.append(
                f"  {row['code']:<12}{row['name']:<10}{row['shares'] or '—':>10} 股  "
                f"价 {row['price'] or '—':<9} 动态 {row['dynamicYield'] or '—':<10}"
                f"预期 {row['expectedYield'] or '—'}"
            )
        t = payload["totals"]
        lines += [
            "",
            f"  持仓市值   {t['marketValue']}",
            f"  动态股息率 {t['dynamicYield'] or '—'}",
            f"  预期股息率 {t['expectedYield'] or '—'}",
            f"  成本收益率 {t['yieldOnCost'] or '—'}"
            + ("" if t["complete"] else "  ← 部分覆盖（有持仓取不到价）"),
        ]
        if not upcoming.is_empty:
            lines += ["", f"  待收 {len(upcoming.rows)} 笔（未实施，不计入上面的已收）"]
            for row in upcoming.rows:
                mark = "确定" if not row.is_proposed else "预案"
                lines.append(
                    f"    {row.month}  {row.code:<12}{row.name:<10}"
                    f"{row.per_share:>9} × {row.shares:>8} 股 = "
                    f"{format_money(row.cash_amount):>12}  [{mark}]"
                )
            lines.append(
                f"    合计 确定 {format_money(upcoming.confirmed_amount)}"
                f" / 预案 {format_money(upcoming.proposed_amount)}"
            )
        if price_error:
            lines.append(f"  取价出错：{price_error}")
        _emit(args, payload, "\n".join(lines))
    else:
        _emit(args, payload, "")
    return EXIT_OK


def _today() -> date:
    """今天（东八区）。"""
    return datetime.now(_CN_TZ).date()


def _fail(message: str) -> NoReturn:  # pragma: no cover - 便捷调试入口
    raise _CliError(message)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
