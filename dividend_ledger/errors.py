"""异常类型。

全部继承 :class:`DividendLedgerError`，便于 CLI 统一捕获后以可读消息退出，
而不是抛出裸 ``ValueError`` 让人猜哪里错了。
"""

from __future__ import annotations


class DividendLedgerError(Exception):
    """本包所有可预期错误的基类。"""


class CodeError(DividendLedgerError, ValueError):
    """证券代码无法归一化。"""


class DecimalError(DividendLedgerError, ValueError):
    """金额/比例字符串无法解析为合法十进制数。

    注意：它是"数值层"的错误；进入数据模型校验时会被包成 :class:`DataError`，
    这样 ``from_dict`` 的调用方只需捕一种异常。
    """


class DataError(DividendLedgerError):
    """输入数据文件缺失、格式错误或字段非法。"""


class ProviderError(DividendLedgerError):
    """分红事件来源不可用或返回了无法解析的数据。"""
