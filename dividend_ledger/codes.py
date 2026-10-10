"""证券代码归一化。

对外统一用 ``600519.SH`` 这种"6 位数字 + 交易所后缀"的形式（SPEC §6.1）。

接受的输入写法::

    600519          # 纯 6 位，自动补后缀
    sh600519        # 前缀小写
    SH600519        # 前缀大写
    600519.SH       # 规范形式
    600519.sh       # 后缀小写
    sh.600519       # 前缀带点
    600519.XSHG     # 交易所惯用别名

设计取舍：**不做静默推断**。显式写了后缀但与前缀规律冲突时（如 ``000001.SH``），
一律拒绝并说明原因——因为 ``000001`` 是平安银行（深市），``000001.SH`` 是上证指数，
静默"纠正"会把两个完全不同的标的混成一个，是账本里最危险的一类错误。
"""

from __future__ import annotations

import re

from dividend_ledger.errors import CodeError

__all__ = [
    "BJ",
    "SH",
    "SZ",
    "EXCHANGES",
    "code_key",
    "is_normalized",
    "normalize_code",
    "to_cli_code",
]

SH = "SH"
SZ = "SZ"
BJ = "BJ"

EXCHANGES: tuple[str, ...] = (SH, SZ, BJ)

# 交易所别名 → 规范后缀。键一律大写后比较。
_EXCHANGE_ALIASES: dict[str, str] = {
    "SH": SH,
    "SS": SH,  # 部分数据源用 SSE / SS 表示上交所
    "SSE": SH,
    "XSHG": SH,
    "SZ": SZ,
    "SZSE": SZ,
    "XSHE": SZ,
    "BJ": BJ,
    "BSE": BJ,
    "NEEQ": BJ,
}

# 纯 6 位代码 → 默认交易所。按首位数字判断；"92" 需先于 "9" 匹配（北交所 920xxx）。
# 注意 9 开头是歧义区：900xxx 为上交所 B 股，920xxx 为北交所。一阶段不支持 B 股，
# 故 9 开头默认归 BJ；若将来要支持沪 B，应显式写 ``900xxx.SH``。
_BARE_PREFIX_TO_EXCHANGE: tuple[tuple[str, str], ...] = (
    ("92", BJ),
    ("4", BJ),
    ("8", BJ),
    ("9", BJ),
    ("6", SH),
    ("5", SH),  # 沪市基金
    ("0", SZ),
    ("3", SZ),
    ("1", SZ),  # 深市基金/债
    ("2", SZ),  # 深市 B 股
)

_INPUT_RE = re.compile(
    r"""^\s*
    (?:(?P<prefix>[A-Za-z]{2,4})\s*[.:]?\s*)?      # 可选前缀：sh / SH / XSHG / sh.
    (?P<digits>\d{6})
    (?:\s*[.:]?\s*(?P<suffix>[A-Za-z]{2,4}))?      # 可选后缀：.SH / .sh / XSHG
    \s*$""",
    re.VERBOSE,
)


def _resolve_exchange(token: str, *, field: str, raw: str) -> str:
    """把交易所标记（前缀或后缀）解析成规范后缀。"""
    key = token.strip().upper()
    try:
        return _EXCHANGE_ALIASES[key]
    except KeyError:
        known = ", ".join(sorted(_EXCHANGE_ALIASES))
        raise CodeError(
            f"无法识别的交易所标记 {token!r}（来自 {raw!r}）；已知标记：{known}"
        ) from None


def _default_exchange(digits: str) -> str:
    """纯 6 位代码按首位数字推断默认交易所。"""
    for prefix, exchange in _BARE_PREFIX_TO_EXCHANGE:
        if digits.startswith(prefix):
            return exchange
    raise CodeError(
        f"无法从 {digits!r} 推断交易所：首位数字不在已知范围内。"
        f"请显式写成 {digits}.SH / {digits}.SZ / {digits}.BJ"
    )


def normalize_code(raw: str) -> str:
    """把各种写法的证券代码归一化为 ``600519.SH`` 形式。

    Args:
        raw: 原始代码字符串。

    Returns:
        规范代码，如 ``600519.SH``。

    Raises:
        CodeError: 格式非法、交易所标记无法识别，或显式交易所与前缀规律冲突。
    """
    if not isinstance(raw, str):
        raise CodeError(f"证券代码必须是字符串，收到 {type(raw).__name__}: {raw!r}")

    match = _INPUT_RE.match(raw)
    if match is None:
        raise CodeError(
            f"无法解析证券代码 {raw!r}；期望 6 位数字，可带 sh/sz/bj 前缀或 .SH/.SZ/.BJ 后缀"
        )

    digits = match.group("digits")
    prefix = match.group("prefix")
    suffix = match.group("suffix")

    if prefix is not None and suffix is not None:
        pre_ex = _resolve_exchange(prefix, field="prefix", raw=raw)
        suf_ex = _resolve_exchange(suffix, field="suffix", raw=raw)
        if pre_ex != suf_ex:
            raise CodeError(
                f"证券代码 {raw!r} 的前缀与后缀指向不同交易所（{pre_ex} vs {suf_ex}）"
            )
        exchange = pre_ex
    elif prefix is not None:
        exchange = _resolve_exchange(prefix, field="prefix", raw=raw)
    elif suffix is not None:
        exchange = _resolve_exchange(suffix, field="suffix", raw=raw)
    else:
        exchange = _default_exchange(digits)

    expected = _default_exchange(digits) if _matches_known_range(digits) else None
    if expected is not None and exchange != expected:
        raise CodeError(
            f"证券代码 {raw!r} 的交易所后缀 {exchange} 与代码段规律冲突"
            f"（{digits} 属于 {expected} 段）。请确认标的："
            f"{digits}.{expected} 与 {digits}.{exchange} 可能是不同标的。"
        )

    return f"{digits}.{exchange}"


def _matches_known_range(digits: str) -> bool:
    """该 6 位代码是否落在已知代码段内。"""
    return any(digits.startswith(prefix) for prefix, _ in _BARE_PREFIX_TO_EXCHANGE)


def is_normalized(raw: str) -> bool:
    """判断字符串是否已经是规范形式。"""
    try:
        return normalize_code(raw) == raw
    except CodeError:
        return False


def code_key(raw: str) -> str:
    """归一化后用作字典键/自然键的稳定形式。"""
    return normalize_code(raw)


def to_cli_code(normalized: str) -> str:
    """把规范代码转成 westock CLI 要求的**前缀**格式。

    **实测发现**：CLI 不接受 ``000001.SZ`` 这种后缀写法，会报
    ``错误: 代码 "000001.SZ" 格式不支持；请改用 "sz000001"``
    （且该错误写 stderr、退出码仍为 0）。

    >>> to_cli_code("600519.SH")
    'sh600519'
    >>> to_cli_code("000001.SZ")
    'sz000001'

    Raises:
        CodeError: 输入不是规范形式。
    """
    if "." not in normalized:
        raise CodeError(
            f"{normalized!r} 不是规范代码（期望 600519.SH 形式），无法转换为 CLI 格式"
        )
    digits, _, exchange = normalized.partition(".")
    if exchange not in EXCHANGES or not digits.isdigit() or len(digits) != 6:
        raise CodeError(f"{normalized!r} 不是规范代码，无法转换为 CLI 格式")
    return f"{exchange.lower()}{digits}"
