import re
import shutil
from datetime import datetime
from pathlib import Path

from config import settings

# INI の OptionSettings 行を抽出するパターン。
# MULTILINE: 行頭一致（先頭にコメント行があっても検出できるように）
# 末尾は `)` の直後で止め、行末の \r や改行は消費しない（CRLF を壊さないため）。
_OPTION_RE = re.compile(r"^OptionSettings=\((.*)\)", re.MULTILINE)
# 値部分は3択:
#  - クォート文字列 "(?:[^"\\]|\\.)*"
#  - 括弧値 \([^)]*\)
#  - 素の値 [^,)]*（0文字以上。DenyTechnologyList= のような空値を拾うため + ではなく *）
_KEY_VAL_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\([^)]*\)|[^,)]*)')

# 秘密情報を含むキー。Mainte はもちろん Admin にも平文を返さず、
# 「設定済みかどうか」だけを伝えて書き込み専用に扱う。
_MASK_KEYS = frozenset({"AdminPassword", "ServerPassword"})
SECRET_PLACEHOLDER = "********"


def _ini_path() -> Path:
    return Path(settings.pal_settings_ini)


def _detect_encoding_and_newline(raw: bytes) -> tuple[str, str]:
    """元ファイルの BOM 有無と改行コードを判定して保存時に維持する"""
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    # CRLF が1つでもあれば CRLF ファイルとみなす
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return encoding, newline


def _unquote(val: str) -> tuple[str, bool]:
    """パーサーの生値からクォートを外し、(値, 元がクォート付きか) を返す。

    クォート付き文字列は Unreal の INI 記法に従い \\" と \\\\ をアンエスケープする。
    """
    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
        inner = val[1:-1]
        inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner, True
    return val, False


def _quote(val: str) -> str:
    """文字列値を INI 記法でクォートする（1回だけ）。\\ と " をエスケープ"""
    escaped = val.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def read_ini() -> dict:
    """INI を読み込み、OptionSettings のキーと値の辞書を返す（マスクなし・アンクォート済み生値）"""
    path = _ini_path()
    if not path.exists():
        return {}
    raw = path.read_bytes()
    encoding, _ = _detect_encoding_and_newline(raw)
    text = raw.decode(encoding, errors="replace")
    m = _OPTION_RE.search(text)
    if not m:
        return {}
    result = {}
    for key, val in _KEY_VAL_RE.findall(m.group(1)):
        unq, _quoted = _unquote(val)
        result[key] = unq
    return result


def mask_secrets(data: dict) -> dict:
    """秘密キーの値をプレースホルダに置換する。値が空でないものだけ設定済みとみなす"""
    masked = {}
    for k, v in data.items():
        if k in _MASK_KEYS:
            masked[k] = SECRET_PLACEHOLDER if str(v) else ""
        else:
            masked[k] = v
    return masked


def secret_key_names() -> list[str]:
    """マスク対象キー名の一覧（フロントで書込専用UIを出すため）"""
    return sorted(_MASK_KEYS)


def write_ini(new_values: dict) -> None:
    """指定したキーの値を INI に書き戻す（バックアップ付き、BOM・改行を維持）。

    内部表現は「クォートなしの値」に統一し、出力時に元がクォート付きだったキーだけを
    1回クォートする。これにより未変更キーの二重引用（AdminPassword=""x"" 等）を防ぐ。
    """
    path = _ini_path()
    if not path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {path}")

    raw = path.read_bytes()
    encoding, _ = _detect_encoding_and_newline(raw)
    text = raw.decode(encoding, errors="replace")
    m = _OPTION_RE.search(text)
    if not m:
        raise ValueError("OptionSettings 行が見つかりません")

    # 現在の全キーを「アンクォート済み値」と「元がクォート付きか」に分解する
    order: list[str] = []
    values: dict[str, str] = {}     # アンクォート済み
    quoted: dict[str, bool] = {}    # 元がクォート付きか
    for key, rawval in _KEY_VAL_RE.findall(m.group(1)):
        unq, is_q = _unquote(rawval)
        if key not in values:
            order.append(key)
        values[key] = unq
        quoted[key] = is_q

    # 変更を適用（入力値はクォートなしの生値として扱う）
    for key, val in new_values.items():
        if key not in values:
            raise KeyError(f"不明な設定キー: {key}")
        if key in _MASK_KEYS and val == SECRET_PLACEHOLDER:
            raise ValueError(f"{key} にプレースホルダ値は保存できません")
        # クォート付きキーの値に制御文字（改行）が混じるのは不正
        if quoted.get(key) and ("\n" in val or "\r" in val):
            raise ValueError(f"{key} の値に改行を含めることはできません")
        values[key] = val

    # OptionSettings 行を再構築（元の順序を維持。クォート付きキーだけ1回クォート）
    def _fmt(k: str) -> str:
        v = values[k]
        return f"{k}={_quote(v)}" if quoted.get(k) else f"{k}={v}"

    inner = ",".join(_fmt(k) for k in order)
    new_option_line = f"OptionSettings=({inner})"

    # バックアップ（元ファイルをそのままコピー）
    backup = path.with_suffix(f".ini.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(path, backup)

    # _OPTION_RE は `)` の直後で止まるため、m.end() 以降（\r や改行）はそのまま温存される。
    # text は read_bytes → decode で改行を正規化していないので CRLF/LF が保たれる。
    new_text = text[: m.start()] + new_option_line + text[m.end() :]
    path.write_bytes(new_text.encode(encoding))
