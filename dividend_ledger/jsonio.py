"""JSON 读写工具。

两条约定：

- **写出必须是原子的**：先写临时文件再 ``os.replace``，避免中途失败留下半个账本文件。
- **写出必须逐字节稳定**：固定 ``indent``、固定键序（由模型 ``to_dict`` 决定）、
  固定换行与结尾换行，保证"同一份输入跑两次结果完全一致"可被 ``diff`` 验证。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from dividend_ledger.errors import DataError

__all__ = ["dump_json_atomic", "load_json", "render_json"]

_JSON_KWARGS: dict[str, Any] = {
    "ensure_ascii": False,  # 保留中文，方案原文要能直接读
    "indent": 2,
    "separators": (",", ": "),
    "sort_keys": False,
}


def render_json(payload: Any) -> str:
    """把对象渲染成稳定的 JSON 文本（含结尾换行）。"""
    return json.dumps(payload, **_JSON_KWARGS) + "\n"


def load_json(path: Path, *, required: bool = True) -> Any | None:
    """读取 JSON 文件。

    Args:
        path: 文件路径。
        required: 为 ``True`` 时文件不存在会报错；为 ``False`` 时返回 ``None``。

    Raises:
        DataError: 文件不存在（且 required）、内容不是合法 JSON。
    """
    if not path.exists():
        if required:
            raise DataError(f"找不到数据文件：{path}")
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DataError(f"无法读取 {path}：{exc}") from None
    if not text.strip():
        raise DataError(f"数据文件为空：{path}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataError(f"{path} 不是合法 JSON：第 {exc.lineno} 行 {exc.msg}") from None


def dump_json_atomic(path: Path, payload: Any) -> None:
    """原子写出 JSON：先写同目录临时文件，再 ``os.replace`` 覆盖目标。"""
    text = render_json(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
