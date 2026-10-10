"""``data/`` 目录读写与**自然键幂等合并**（SPEC §4.6）。

为什么不用内容指纹去重：持仓和分红事件是**声明式快照**，不是事件流。
同一份输入导入两次必须结果完全相同；改一个数字后再导入应该**覆盖**旧值，
而不是留下一条"幽灵记录"。所以键取业务自然键：

===========================  ==========================================
记录类型                      自然键
===========================  ==========================================
持仓                          ``code``
现金分红事件                  ``(code, ex_date)``
送转事件（同一事件的附属部分）  ``(code, ex_date, "bonus")``
===========================  ==========================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from dividend_ledger.codes import normalize_code
from dividend_ledger.errors import DataError
from dividend_ledger.jsonio import dump_json_atomic, load_json
from dividend_ledger.models import (
    DATE_FORMAT,
    DateBasis,
    DividendEvent,
    Holding,
    parse_date,
)

__all__ = [
    "DataPaths",
    "ImportAudit",
    "RejectedItem",
    "apply_pay_date_overrides",
    "import_events",
    "import_holdings",
    "load_events",
    "load_holdings",
    "load_pay_date_overrides",
    "save_events",
    "save_holdings",
]

#: ``pay_dates.json`` 复合键的分隔符，``|`` 与 ``@`` 都接受
_KEY_SEPARATORS = ("|", "@")


@dataclass(frozen=True, slots=True)
class DataPaths:
    """``data/`` 目录布局（SPEC §6.2）。"""

    root: Path

    @property
    def holdings(self) -> Path:
        return self.root / "holdings.json"

    @property
    def events(self) -> Path:
        return self.root / "events.json"

    @property
    def pay_dates(self) -> Path:
        return self.root / "pay_dates.json"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    @property
    def ledger(self) -> Path:
        return self.out_dir / "ledger.json"

    @property
    def report(self) -> Path:
        return self.out_dir / "report.json"

    def describe(self) -> dict[str, str]:
        """报告 ``meta.inputs`` 用的相对路径快照。"""
        return {
            "data_dir": str(self.root),
            "holdings": str(self.holdings),
            "events": str(self.events),
            "pay_dates": str(self.pay_dates) if self.pay_dates.exists() else "",
        }


@dataclass(frozen=True, slots=True)
class RejectedItem:
    """被拒绝的一条记录，附可读原因（不允许静默丢弃）。"""

    index: int
    reason: str
    payload: Any = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"index": self.index, "reason": self.reason}
        if self.payload is not None:
            result["payload"] = self.payload
        return result


@dataclass(frozen=True, slots=True)
class ImportAudit:
    """一次导入的审计结果。"""

    source: str
    items_in_file: int
    imported: int
    replaced: int
    unchanged: int
    rejected: tuple[RejectedItem, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> int:
        """文件里被接受的条数（含覆盖与未变化）。"""
        return self.items_in_file - len(self.rejected)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source": self.source,
            "items_in_file": self.items_in_file,
            "imported": self.imported,
            "replaced": self.replaced,
            "unchanged": self.unchanged,
            "accepted": self.accepted,
            "rejected_count": len(self.rejected),
        }
        if self.rejected:
            result["rejected"] = [item.to_dict() for item in self.rejected]
        if self.details:
            result["details"] = dict(self.details)
        return result


# --------------------------------------------------------------------------- #
# 通用：把文件内容规范成"记录列表"
# --------------------------------------------------------------------------- #


def _coerce_records(payload: Any, *, path: Path, record_key: str) -> list[Any]:
    """接受两种写法：裸数组，或 ``{"<record_key>": [...]}`` 信封。"""
    if isinstance(payload, list):
        return list(payload)
    if isinstance(payload, Mapping):
        records = payload.get(record_key)
        if records is None:
            raise DataError(
                f"{path} 是对象但没有 {record_key!r} 数组；"
                f"应为裸数组或 {{ \"{record_key}\": [...] }}"
            )
        if not isinstance(records, list):
            raise DataError(f"{path} 的 {record_key!r} 必须是数组")
        return list(records)
    raise DataError(f"{path} 顶层必须是数组或对象，收到 {type(payload).__name__}")


def _parse_records(
    records: Sequence[Any],
    *,
    parse_one: Any,
    label: str,
) -> tuple[list[Any], list[RejectedItem]]:
    """逐条解析，把失败项收集成 :class:`RejectedItem` 而不是直接中断。"""
    parsed: list[Any] = []
    rejected: list[RejectedItem] = []
    for index, raw in enumerate(records):
        try:
            parsed.append(parse_one(raw, where=f"{label}[{index}]"))
        except DataError as exc:
            rejected.append(RejectedItem(index=index, reason=str(exc), payload=raw))
    return parsed, rejected


# --------------------------------------------------------------------------- #
# 持仓
# --------------------------------------------------------------------------- #


def load_holdings(
    path: Path, *, required: bool = True
) -> tuple[list[Holding], list[RejectedItem]]:
    """读取持仓文件，返回 ``(记录列表, 被拒绝项)``。"""
    payload = load_json(path, required=required)
    if payload is None:
        return [], []
    records = _coerce_records(payload, path=path, record_key="holdings")
    return _parse_records(records, parse_one=Holding.from_dict, label="holdings")


def import_holdings(path: Path, incoming: Iterable[Holding]) -> ImportAudit:
    """把持仓并入账本（自然键 ``code``），返回审计结果。"""
    existing, rejected = load_holdings(path, required=False)
    if rejected:
        raise DataError(
            f"{path} 已有 {len(rejected)} 条无法解析的记录，拒绝在其上合并——"
            f"否则它们会在重写文件时被静默丢弃。首个问题：{rejected[0].reason}"
        )
    index: dict[str, Holding] = {item.code: item for item in existing}

    imported = replaced = unchanged = 0
    for item in incoming:
        previous = index.get(item.code)
        if previous is None:
            imported += 1
        elif previous == item:
            unchanged += 1
        else:
            replaced += 1
        index[item.code] = item

    merged = sorted(index.values(), key=lambda item: item.code)
    save_holdings(path, merged)

    return ImportAudit(
        source=str(path),
        items_in_file=len(incoming) + len(rejected),
        imported=imported,
        replaced=replaced,
        unchanged=unchanged,
        rejected=tuple(rejected),
    )


def replace_holdings(path: Path, holdings: Sequence[Holding]) -> ImportAudit:
    """**整份替换**持仓。

    为什么需要：``import_holdings`` 是按 ``code`` 合并（upsert），这对"改一只股票的
    股数"是对的，但**表达不了删除**——传空数组进去等于什么都不做。
    面板上的"删掉这只"就是靠它实现的。

    持仓是**声明式状态**（当前持有哪几只），所以整份替换是它的自然语义。
    """
    existing, rejected = load_holdings(path, required=False)
    if rejected:
        raise DataError(
            f"{path} 已有 {len(rejected)} 条无法解析的记录；请先修好再替换。"
            f"首个问题：{rejected[0].reason}"
        )

    incoming = list(holdings)
    save_holdings(path, incoming)

    return ImportAudit(
        source=str(path),
        items_in_file=len(incoming),
        imported=len(incoming),
        replaced=0,
        unchanged=0,
        rejected=(),
        details={"removed": len(existing), "mode": "replace"},
    )


def save_holdings(path: Path, holdings: Sequence[Holding]) -> None:
    """按 ``code`` 排序后写出，保证逐字节稳定。"""
    ordered = sorted(holdings, key=lambda item: item.code)
    dump_json_atomic(
        path,
        {"format_version": 1, "holdings": [item.to_dict() for item in ordered]},
    )


# --------------------------------------------------------------------------- #
# 分红事件
# --------------------------------------------------------------------------- #


def load_events(
    path: Path, *, required: bool = True
) -> tuple[list[DividendEvent], list[RejectedItem]]:
    """读取分红事件文件，返回 ``(记录列表, 被拒绝项)``。"""
    payload = load_json(path, required=required)
    if payload is None:
        return [], []
    records = _coerce_records(payload, path=path, record_key="events")
    return _parse_records(records, parse_one=DividendEvent.from_dict, label="events")


def replace_events(path: Path, events: Iterable[DividendEvent]) -> ImportAudit:
    """**清空后重写**事件文件。

    为什么需要它：``sync`` 默认是幂等**合并**，这对同一数据源的重复抓取是正确的；
    但换数据源时（例如先跑 fixture 再跑 eastock/westock）旧记录会残留，
    不同的唯一键还会让同一次除权出现两条、金额被重复累加。
    ``data/events.json`` 是**派生状态**（可从数据源重建），所以清空重写是安全的。

    Returns:
        审计结果。``details["removed"]`` 记录被清掉的旧记录数。
    """
    existing, rejected = load_events(path, required=False)
    if rejected:
        raise DataError(
            f"{path} 已有 {len(rejected)} 条无法解析的记录；"
            f"请先修好再 replace。首个问题：{rejected[0].reason}"
        )

    incoming_list = list(events)
    save_events(path, incoming_list)

    return ImportAudit(
        source=str(path),
        items_in_file=len(incoming_list),
        imported=len(incoming_list),
        replaced=0,
        unchanged=0,
        rejected=(),
        details={"removed": len(existing), "mode": "replace"},
    )


def import_events(path: Path, incoming: Iterable[DividendEvent]) -> ImportAudit:
    """把分红事件并入账本，按键幂等合并。

    合并规则（SPEC §4.6）：

    - 现金分红按 ``(code, ex_date)`` 定位；送转按 ``(code, ex_date, "bonus")`` 定位。
    - 键相同且内容相同 → 计入 ``unchanged``（重复导入的典型情况）。
    - 键相同但内容变了 → 覆盖旧值，计入 ``replaced``，**不留幽灵记录**。
    - 键不存在 → 新增，计入 ``imported``。
    """
    existing, rejected = load_events(path, required=False)
    if rejected:
        raise DataError(
            f"{path} 已有 {len(rejected)} 条无法解析的记录，拒绝在其上合并——"
            f"否则它们会在重写文件时被静默丢弃。首个问题：{rejected[0].reason}"
        )
    cash: dict[tuple[str, ...], DividendEvent] = {e.natural_key: e for e in existing}
    bonus: dict[tuple[str, ...], DividendEvent] = {
        e.bonus_key: e for e in existing if e.has_bonus
    }

    imported = replaced = unchanged = 0
    incoming_list = list(incoming)

    for event in incoming_list:
        # 现金键长度 3、送转键长度 4，用长度区分走哪张表
        keys: list[tuple[Any, ...]] = [event.natural_key]
        if event.has_bonus:
            keys.append(event.bonus_key)

        changed = False
        seen = False
        for key in keys:
            table = cash if len(key) == 3 else bonus
            previous = table.get(key)
            if previous is not None:
                seen = True
                if previous != event:
                    changed = True

        for key in keys:
            table = cash if len(key) == 3 else bonus
            table[key] = event

        if changed:
            replaced += 1
        elif seen:
            unchanged += 1
        else:
            imported += 1

    merged = _merge_tables(cash, bonus)
    save_events(path, merged)

    return ImportAudit(
        source=str(path),
        items_in_file=len(incoming_list) + len(rejected),
        imported=imported,
        replaced=replaced,
        unchanged=unchanged,
        rejected=tuple(rejected),
    )


def _merge_tables(
    cash: Mapping[tuple[str, ...], DividendEvent],
    bonus: Mapping[tuple[str, ...], DividendEvent],
) -> list[DividendEvent]:
    """把两张键表合并成去重后的事件列表。

    一个事件可能同时出现在两张表里（既派现又送股），按自然键去重取其一即可；
    两张表里的对象本就应当完全相同。
    """
    by_natural_key: dict[tuple[str, ...], DividendEvent] = dict(cash)
    for event in bonus.values():
        by_natural_key.setdefault(event.natural_key, event)
    return sorted(
        by_natural_key.values(),
        key=lambda item: (item.code, item.ex_date, item.source),
    )


def save_events(path: Path, events: Sequence[DividendEvent]) -> None:
    """按 ``(code, ex_date)`` 排序后写出。"""
    ordered = sorted(events, key=lambda item: (item.code, item.ex_date, item.source))
    dump_json_atomic(
        path,
        {"format_version": 1, "events": [item.to_dict() for item in ordered]},
    )


# --------------------------------------------------------------------------- #
# 到账日人工覆盖表
# --------------------------------------------------------------------------- #


def _parse_override_key(key: str) -> tuple[str, date]:
    """解析 ``"600519.SH|2024-06-19"`` 形式的复合键。"""
    for separator in _KEY_SEPARATORS:
        if separator in key:
            raw_code, _, raw_date = key.partition(separator)
            try:
                code = normalize_code(raw_code)
            except Exception as exc:
                raise DataError(f"覆盖表键 {key!r} 的代码部分非法：{exc}") from None
            return code, parse_date(
                raw_date.strip(), field_name=f"pay_dates[{key!r}].ex_date"
            )
    raise DataError(
        f"覆盖表键 {key!r} 格式非法；应为 \"<代码>|<除权除息日>\"，"
        f"如 \"600519.SH|2024-06-19\""
    )


def load_pay_date_overrides(
    path: Path,
) -> tuple[dict[tuple[str, str], date], list[RejectedItem]]:
    """读取到账日覆盖表。

    支持两种写法::

        {"600519.SH|2024-06-19": "2024-06-26"}
        {"overrides": [{"code": "600519.SH", "ex_date": "2024-06-19",
                        "pay_date": "2024-06-26", "note": "券商流水"}]}
    """
    payload = load_json(path, required=False)
    if payload is None:
        return {}, []
    if not isinstance(payload, Mapping):
        raise DataError(f"{path} 顶层必须是对象，收到 {type(payload).__name__}")

    overrides: dict[tuple[str, str], date] = {}
    rejected: list[RejectedItem] = []

    if "overrides" in payload:
        raw_items = payload["overrides"]
        if not isinstance(raw_items, list):
            raise DataError(f"{path} 的 'overrides' 必须是数组")
        known = {"code", "ex_date", "pay_date", "note"}
        for index, raw in enumerate(raw_items):
            try:
                if not isinstance(raw, Mapping):
                    raise DataError(f"overrides[{index}] 必须是对象")
                unknown = sorted(set(raw) - known)
                if unknown:
                    raise DataError(f"overrides[{index}] 含未知字段 {unknown}")
                code = normalize_code(raw["code"])
                ex_date = parse_date(
                    raw["ex_date"], field_name=f"overrides[{index}].ex_date"
                )
                pay_date = parse_date(
                    raw["pay_date"], field_name=f"overrides[{index}].pay_date"
                )
                overrides[(code, ex_date.isoformat())] = pay_date
            except (DataError, KeyError) as exc:
                reason = str(exc) if not isinstance(exc, KeyError) else f"缺少字段 {exc}"
                rejected.append(RejectedItem(index=index, reason=reason, payload=raw))
        return overrides, rejected

    for key, value in payload.items():
        try:
            code, ex_date = _parse_override_key(str(key))
            pay_date = parse_date(value, field_name=f"pay_dates[{key!r}]")
            overrides[(code, ex_date.isoformat())] = pay_date
        except DataError as exc:
            rejected.append(RejectedItem(index=-1, reason=str(exc), payload={key: value}))

    return overrides, rejected


def apply_pay_date_overrides(
    events: Sequence[DividendEvent],
    overrides: Mapping[tuple[str, str], date],
) -> tuple[list[DividendEvent], dict[str, Any]]:
    """把覆盖表应用到事件上。

    ``source_ref`` 会打上 ``pay_date_override`` 标记，便于在明细账里区分
    "数据源声明的到账日"与"人工覆盖的到账日"。

    Returns:
        ``(处理后的事件, 统计)``。统计里会列出**未能命中任何事件**的覆盖项——
        这类"僵尸覆盖"必须让用户看见，否则他会以为生效了。
    """
    applied: list[DividendEvent] = []
    hit_keys: set[tuple[str, str]] = set()
    applied_count = 0

    for event in events:
        # 覆盖表按 (code, ex_date) 定位；同一除权日的多条方案共享同一条覆盖
        key = (event.code, event.ex_date.isoformat())
        if key in overrides:
            hit_keys.add(key)
            pay_date = overrides[key]
            applied.append(
                DividendEvent(
                    code=event.code,
                    ex_date=event.ex_date,
                    dividend_per_share=event.dividend_per_share,
                    date_basis=DateBasis.PAY_DATE_OVERRIDE,
                    pay_date=pay_date,
                    per_share_bonus=event.per_share_bonus,
                    per_share_transfer=event.per_share_transfer,
                    right_reg_date=event.right_reg_date,
                    close_prev=event.close_prev,
                    plan_text=event.plan_text,
                    proposal_sn=event.proposal_sn,
                    status=event.status,
                    source=event.source,
                    source_ref={**dict(event.source_ref), "pay_date_override": True},
                )
            )
            applied_count += 1
        else:
            applied.append(event)

    unmatched = sorted(
        f"{code}|{ex_date}"
        for code, ex_date in overrides
        if (code, ex_date) not in hit_keys
    )

    return applied, {
        "overrides_in_file": len(overrides),
        "overrides_applied": applied_count,
        "overrides_unmatched": unmatched,
    }
