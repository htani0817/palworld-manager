# -*- coding: utf-8 -*-
"""INI の保存値と Palworld REST API の稼働値を比較する。"""

import json
import math
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Optional

import ini_editor
import palworld_client as pal
from config import settings


_NUMBER_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)
_OPENING = {"(": ")", "[": "]", "{": "}"}
_CLOSING = set(_OPENING.values())


class ConfigDiffError(RuntimeError):
    """設定差分の取得に失敗したときの基底例外。"""


class IniNotFoundError(ConfigDiffError):
    """PalWorldSettings.ini が存在しない。"""


class IniReadError(ConfigDiffError):
    """INI が読めない、または OptionSettings が不正。"""


class RuntimeSettingsError(ConfigDiffError):
    """Palworld REST API から稼働値を取得できない。"""


def _has_balanced_outer_pair(value: str) -> bool:
    """文字列全体が1組の括弧で囲まれているかを確認する。"""
    if len(value) < 2 or value[0] not in _OPENING:
        return False
    expected = _OPENING[value[0]]
    if value[-1] != expected:
        return False

    stack: list[str] = []
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(value):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char in _OPENING:
            stack.append(_OPENING[char])
            continue
        if char in _CLOSING:
            if not stack or stack.pop() != char:
                return False
            # 最外の括弧が途中で閉じたものは「全体を囲む括弧」ではない。
            if not stack and index != len(value) - 1:
                return False
    return quote is None and not stack


def _split_top_level(value: str, delimiter: str = ",") -> list[str]:
    """引用符とネスト括弧の内側を避けて分割する。"""
    if not value.strip():
        return []

    parts: list[str] = []
    start = 0
    stack: list[str] = []
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(value):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in _OPENING:
            stack.append(_OPENING[char])
        elif char in _CLOSING:
            if stack and stack[-1] == char:
                stack.pop()
        elif char == delimiter and not stack:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return parts


def _split_assignment(value: str) -> Optional[tuple[str, str]]:
    """リスト要素の ``Key=Value`` をトップレベルで分ける。"""
    stack: list[str] = []
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(value):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in _OPENING:
            stack.append(_OPENING[char])
        elif char in _CLOSING:
            if stack and stack[-1] == char:
                stack.pop()
        elif char == "=" and not stack:
            key = value[:index].strip()
            if key:
                return key, value[index + 1 :].strip()
            return None
    return None


def _decode_quoted(value: str) -> str:
    if len(value) < 2 or value[0] != value[-1] or value[0] not in {'"', "'"}:
        return value
    if value[0] == '"':
        try:
            decoded = json.loads(value)
            if isinstance(decoded, str):
                return decoded
        except (json.JSONDecodeError, TypeError):
            pass
    inner = value[1:-1]
    return inner.replace(f"\\{value[0]}", value[0]).replace("\\\\", "\\")


def _normalise_list_item(value: str) -> Any:
    assignment = _split_assignment(value)
    if assignment is not None:
        key, raw_value = assignment
        return ("assignment", key, _normalise_value(raw_value))
    return _normalise_value(value)


def _normalise_number(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return number if number.is_finite() else None
    return None


def _normalise_value(value: Any) -> Any:
    """比較専用の値に変換する。表示用の元値は変更しない。"""
    if isinstance(value, bool):
        return ("bool", value)

    number = _normalise_number(value)
    if number is not None:
        return ("number", number)

    if isinstance(value, Mapping):
        entries = ((str(key), _normalise_value(item)) for key, item in value.items())
        return ("mapping", tuple(sorted(entries, key=lambda item: item[0].casefold())))

    if isinstance(value, (list, tuple)):
        return ("list", tuple(_normalise_value(item) for item in value))

    if value is None:
        return ("null", None)

    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = _decode_quoted(text).strip()

    lowered = text.casefold()
    if lowered == "true":
        return ("bool", True)
    if lowered == "false":
        return ("bool", False)

    if _NUMBER_RE.fullmatch(text):
        try:
            number = Decimal(text)
            if number.is_finite():
                return ("number", number)
        except InvalidOperation:
            pass

    if _has_balanced_outer_pair(text):
        inner = text[1:-1]
        return ("list", tuple(_normalise_list_item(part) for part in _split_top_level(inner)))

    return ("string", text)


def _json_safe_value(value: Any) -> Any:
    """表示用の値から非有限数とJSON非対応型を除く。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return str(value)


def compare_settings(
    file_settings: Mapping[str, Any],
    runtime_settings: Mapping[str, Any],
    *,
    secret_keys: Optional[list[str]] = None,
) -> dict[str, Any]:
    """保存値と稼働値の差分を、秘密値を含めずに返す。"""
    secrets = {
        key.casefold()
        for key in (secret_keys if secret_keys is not None else ini_editor.secret_key_names())
    }
    file_values = {
        key: value
        for key, value in file_settings.items()
        if isinstance(key, str) and key.casefold() not in secrets
    }
    runtime_values = {
        key: value
        for key, value in runtime_settings.items()
        if isinstance(key, str) and key.casefold() not in secrets
    }

    counts = {"same": 0, "pending": 0, "file_only": 0, "runtime_only": 0}
    items: list[dict[str, Any]] = []
    for key in sorted(set(file_values) | set(runtime_values), key=lambda item: (item.casefold(), item)):
        in_file = key in file_values
        in_runtime = key in runtime_values
        if in_file and in_runtime:
            status = (
                "same"
                if _normalise_value(file_values[key]) == _normalise_value(runtime_values[key])
                else "pending"
            )
        elif in_file:
            status = "file_only"
        else:
            status = "runtime_only"

        counts[status] += 1
        items.append(
            {
                "key": key,
                "file_value": _json_safe_value(file_values.get(key)) if in_file else None,
                "runtime_value": _json_safe_value(runtime_values.get(key)) if in_runtime else None,
                "status": status,
            }
        )
    return {"items": items, "counts": counts}


def _read_file_settings() -> Mapping[str, Any]:
    path = Path(settings.pal_settings_ini)
    if not path.exists():
        raise IniNotFoundError(f"INI 設定ファイルが見つかりません: {path}")
    if not path.is_file():
        raise IniReadError(f"INI 設定パスがファイルではありません: {path}")
    try:
        data = ini_editor.read_ini()
    except OSError as exc:
        raise IniReadError(f"INI 設定ファイルを読み込めません: {exc}") from exc
    except Exception as exc:
        raise IniReadError(f"INI 設定の解析に失敗しました: {exc}") from exc
    if not isinstance(data, Mapping) or not data:
        raise IniReadError("INI 設定に OptionSettings が見つからないか、値が空です")
    return data


def _runtime_error_message(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        return f"Palworld REST API /settings が HTTP {status_code} を返しました"
    detail = str(exc).strip()
    if detail:
        return f"Palworld REST API /settings に接続できません: {detail}"
    return "Palworld REST API /settings から稼働値を取得できません"


async def get_config_diff() -> dict[str, Any]:
    """INI と REST API を読み、正規化済みの差分判定を返す。"""
    file_settings = _read_file_settings()
    try:
        runtime_settings = await pal.get_settings_data()
    except Exception as exc:
        raise RuntimeSettingsError(_runtime_error_message(exc)) from exc
    if not isinstance(runtime_settings, Mapping):
        raise RuntimeSettingsError(
            "Palworld REST API /settings の応答が設定オブジェクトではありません"
        )
    return compare_settings(file_settings, runtime_settings)
