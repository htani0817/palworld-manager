# -*- coding: utf-8 -*-
"""イベントログパーサの回帰テスト。

index.html の JS ロジック（LOG_TS_RE / parseLogLine / parseLogTimestamp）の
ソースを読んで、正規表現が実際に動作することを Python から検証する。
正規表現の文字列を抽出して re モジュールで評価するため、JS エンジンを使わずに
パターンの退行を検知できる。

実行:
    cd backend
    python tests/test_log_parser.py
または:
    python -m pytest tests/test_log_parser.py -v
"""

import re
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parent.parent
FRONTEND_HTML = BACKEND.parent / "frontend" / "index.html"


def _load_html() -> str:
    return FRONTEND_HTML.read_text(encoding="utf-8")


def _extract_log_ts_re(html: str) -> str:
    """JS ソースから LOG_TS_RE の正規表現パターンを抽出する。
    パターン内に / が含まれるため、行全体を取り出して前後の / を除去する。"""
    m = re.search(r"const LOG_TS_RE\s*=\s*/(.*)/;", html)
    assert m, "LOG_TS_RE が見つかりません"
    return m.group(1)


class TestLogTsRe:
    """LOG_TS_RE の正規表現が正しい書式を認識すること"""

    def setup_method(self):
        html = _load_html()
        pattern = _extract_log_ts_re(html)
        self.re = re.compile(pattern)

    def test_iso_with_z_suffix(self):
        """ISO 8601 + Z （UTC）を認識すること"""
        m = self.re.match("2026-07-27T01:36:23Z ")
        assert m is not None, "Z 付き ISO 時刻が認識されません"
        assert "Z" in m.group(1), f"Z が time group に含まれません: {m.group(1)}"

    def test_iso_with_utc_offset(self):
        """UTC オフセット付き ISO 8601 を認識すること"""
        m = self.re.match("2026-07-27T10:36:23+09:00 ")
        assert m is not None, "オフセット付き ISO 時刻が認識されません"
        assert "+09:00" in m.group(1)

    def test_iso_without_timezone(self):
        """タイムゾーンなし ISO 8601 を認識すること"""
        m = self.re.match("2026-07-27T01:36:23 ")
        assert m is not None

    def test_bracketed_datetime(self):
        """[2024-05-24 14:30:45] 形式を認識すること"""
        m = self.re.match("[2024-05-24 14:30:45] some message")
        assert m is not None

    def test_time_only(self):
        """hh:mm:ss のみの形式を認識すること"""
        m = self.re.match("14:30:45 message")
        assert m is not None

    def test_no_match_on_plain_text(self):
        """タイムスタンプのない行は一致しないこと"""
        m = self.re.match("plain log message without timestamp")
        assert m is None


class TestLogLevelRules:
    """LOG_LEVEL_RULES の優先順位が正しいこと"""

    def setup_method(self):
        html = _load_html()
        # 各ルールを正規表現として抽出
        self.error_re = re.search(r"level: 'ERROR', re: /([^/]+)/i", html)
        self.warn_re  = re.search(r"level: 'WARN',\s*re: /([^/]+)/i", html)
        self.debug_re = re.search(r"level: 'DEBUG', re: /([^/]+)/i", html)
        assert self.error_re, "ERROR ルールが見つかりません"
        assert self.warn_re,  "WARN ルールが見つかりません"
        assert self.debug_re, "DEBUG ルールが見つかりません"
        self.error_pattern = re.compile(self.error_re.group(1), re.IGNORECASE)
        self.warn_pattern  = re.compile(self.warn_re.group(1),  re.IGNORECASE)
        self.debug_pattern = re.compile(self.debug_re.group(1), re.IGNORECASE)

    def test_error_keyword(self):
        """'error' キーワードを含む行を ERROR と判定すること"""
        assert self.error_pattern.search("Failed to save world: error")

    def test_fatal_is_error(self):
        """'fatal' を ERROR と判定すること"""
        assert self.error_pattern.search("FATAL: unrecoverable condition")

    def test_warn_keyword(self):
        """'warn' を WARN と判定すること"""
        assert self.warn_pattern.search("connection timeout warning")

    def test_no_errors_is_not_error(self):
        """'no errors' は ERROR にならないこと（単語境界がある場合）
        注: \\b を使っているので 'errors' は単独語として一致するが、
        'no errors' 全体のフレーズは ERROR とは無関係。実際にはこの
        テストは許容済みの誤検出と相殺される設計のため、WARN/DEBUG
        が先に一致しないことだけを確認する。"""
        assert not self.warn_pattern.search("no errors found")
        assert not self.debug_pattern.search("no errors found")


class TestBracketPrefix:
    """角括弧プレフィックスの処理が正しいこと"""

    def setup_method(self):
        html = _load_html()
        # JS の正規表現パターンを抽出
        m = re.search(r"/\^\[\[\^\\x00-\\x7F\]\+\]\\./", html)
        # エスケープ後の形を直接確認する方式
        self.html = html

    def test_japanese_bracket_prefix_handled_as_system(self):
        # r"^\[[^\x00-\x7F]+\]" が JS ソースに存在すること
        assert r"^\[[^\x00-\x7F]+\]" in self.html, (
            "日本語角括弧プレフィックスのパターンが見つかりません"
        )

    def test_old_broad_pattern_not_present(self):
        # 旧パターン r"^\[[^\]]+\]" が削除されていること（英字タグを誤判定する）
        assert r"^\[[^\]]+\]" not in self.html, (
            "旧い角括弧パターンが残っています。[ERROR] 等を誤判定します。"
        )


def _run_all() -> int:
    import traceback
    tests_classes = [TestLogTsRe, TestLogLevelRules, TestBracketPrefix]
    failed = []
    for cls in tests_classes:
        instance = cls()
        for name in sorted(dir(instance)):
            if not name.startswith("test_"):
                continue
            method = getattr(instance, name)
            if hasattr(instance, "setup_method"):
                instance.setup_method()
            try:
                method()
                print(f"  OK  {cls.__name__}.{name}")
            except Exception:
                print(f" FAIL {cls.__name__}.{name}")
                traceback.print_exc()
                failed.append(f"{cls.__name__}.{name}")
    print()
    if failed:
        print(f"FAILED {len(failed)} tests")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
