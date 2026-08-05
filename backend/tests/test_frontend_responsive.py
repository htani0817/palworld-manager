# -*- coding: utf-8 -*-
"""フロントエンドのレイアウト・ナビゲーション・ダッシュボード構造の回帰テスト。

実行:
    cd backend
    python tests/test_frontend_responsive.py
または:
    python -m pytest tests/test_frontend_responsive.py -v
"""

import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


BACKEND = Path(__file__).resolve().parent.parent
FRONTEND_HTML = BACKEND.parent / "frontend" / "index.html"


class _StructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.ids: list[str] = []
        self.nav_tabs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            self.ids.append(element_id)
            self.elements[element_id] = (tag, values)
        classes = set((values.get("class") or "").split())
        if "nav-item" in classes and values.get("data-tab"):
            self.nav_tabs.append(values["data-tab"] or "")


def _load_frontend() -> tuple[str, _StructureParser]:
    html = FRONTEND_HTML.read_text(encoding="utf-8")
    parser = _StructureParser()
    parser.feed(html)
    return html, parser


def test_document_has_no_duplicate_ids():
    """開閉処理が参照するIDを一意に保つこと"""
    _, parser = _load_frontend()
    duplicates = [element_id for element_id, count in Counter(parser.ids).items() if count > 1]
    assert duplicates == []


def test_menu_button_and_drawer_have_accessible_controls():
    """ハンバーガーとドロワーに必要なARIA属性があること"""
    _, parser = _load_frontend()
    menu_tag, menu = parser.elements["menu-toggle"]
    sidebar_tag, sidebar = parser.elements["sidebar"]
    backdrop_tag, backdrop = parser.elements["sidebar-backdrop"]

    assert menu_tag == "button"
    assert menu.get("type") == "button"
    assert menu.get("aria-controls") == "sidebar"
    assert menu.get("aria-expanded") in {"true", "false"}
    assert menu.get("aria-label")
    assert sidebar_tag == "nav"
    assert sidebar.get("aria-label") == "メインメニュー"
    assert backdrop_tag == "button"
    assert backdrop.get("aria-label") == "メニューを閉じる"
    assert backdrop.get("aria-hidden") == "true"


def test_every_navigation_item_has_a_matching_panel():
    """メニュー項目から存在しない画面へ遷移しないこと"""
    _, parser = _load_frontend()
    assert parser.nav_tabs
    for tab in parser.nav_tabs:
        assert f"tab-{tab}" in parser.elements


def test_mobile_drawer_css_replaces_hidden_sidebar():
    """スマホでサイドバーを消さず、ドロワーとして開閉すること"""
    html, _ = _load_frontend()
    assert '<meta name="viewport"' in html
    assert "width=device-width" in html
    assert "viewport-fit=cover" in html
    assert "@media (max-width: 700px)" in html
    assert "#layout.sidebar-open #sidebar" in html
    assert "transform: translateX(-100%);" in html
    assert "transform: translateX(0);" in html
    assert "#layout.sidebar-open #sidebar-backdrop" in html
    assert "#sidebar { display: none; }" not in html
    # デスクトップ側の折りたたみも維持すること
    assert "#layout.sidebar-collapsed #sidebar" in html
    # ハンバーガーは #content の外に置く（ドロワーを開くと #content が inert になり、
    # 中にあるとハンバーガー自身が操作不能になるため）
    assert "#main-col" in html


def test_responsive_rules_cover_mobile_viewport_and_fixed_width_content():
    """動的viewportと主要な固定幅要素のスマホ調整があること"""
    html, _ = _load_frontend()
    assert "height: 100dvh;" in html
    assert ".resource-grid { grid-template-columns: minmax(0, 1fr); }" in html
    assert "#sch-label, #sch-once-dt, #sch-once-label, #sch-cron, #sch-cron-label" in html
    assert "min-width: 0;" in html
    assert "#toast-wrap" in html and "max-width: none;" in html
    # ダッシュボードの新しい行がスマホで畳まれること
    # （具体的な列数や余白は変わりうるので、正規表現で緩く固定する）
    assert re.search(r"\.kpi-row \{[^}]*grid-template-columns: repeat\(2,", html)
    assert re.search(
        r"\.dash-2col, \.dash-2col-wide \{[^}]*grid-template-columns: minmax\(0, 1fr\)", html
    )


def test_dashboard_has_host_and_palworld_information_cards():
    """ホストとPalworldの情報を安全な2カード構成で表示すること"""
    html, parser = _load_frontend()

    assert {"host-os", "host-cpu", "host-memory", "host-disk"}.issubset(parser.elements)
    assert '<div class="card-title">ホストサーバー情報</div>' in html
    assert '<div class="card-title">Palworld サーバー情報</div>' in html
    # 2枚が同じ resource-grid 内に並ぶことだけを固定する。
    # 余白などのインラインstyleはレイアウト実装の内部詳細なので固定しない。
    assert re.search(
        r'<div class="resource-grid"[^>]*>\s*'
        r'<div class="card"[^>]*>\s*'
        r'<div class="card-title">ホストサーバー情報</div>',
        html,
    )

    assert re.search(r"const host = sys\.host \|\| \{\};", html)
    required_updates = (
        r"setText\('host-os',\s*host\.os_pretty_name\)",
        r"setText\('host-cpu',\s*s\.cpu_count\b",
        r"setText\('host-memory',\s*s\.mem_total_gb\b",
        r"setText\('host-disk',\s*s\.disk_total_gb\b",
    )
    for pattern in required_updates:
        assert re.search(pattern, html), pattern


def test_safe_areas_and_touch_targets_are_not_limited_to_portrait_width():
    """横向き端末でもノッチ回避と十分なタップ領域を維持すること"""
    html, _ = _load_frontend()
    base_css = html.split("@media (max-width: 960px)", maxsplit=1)[0]

    for inset in ("top", "right", "bottom", "left"):
        assert f"env(safe-area-inset-{inset})" in base_css
    assert "@media (hover: none) and (pointer: coarse)" in html
    assert re.search(
        r"@media \(max-width: 960px\).*?\.page-head \{.*?flex-wrap: wrap;.*?"
        r"\.page-head \.right \{.*?flex-wrap: wrap;",
        html,
        re.DOTALL,
    )


def test_menu_javascript_handles_all_close_paths():
    """ボタン、背景、項目選択、Escape、幅変更を開閉処理へ接続すること"""
    html, _ = _load_frontend()
    required_patterns = (
        r"menuToggle\.addEventListener\('click',\s*toggleSidebar\)",
        r"sidebarBackdrop\.addEventListener\('click'",
        r"event\.key\s*!==\s*'Escape'",
        r"mobileSidebarQuery\.addEventListener\('change'",
        r"closeMobileSidebar\(true\)",
        r"setAttribute\('aria-current',\s*'page'\)",
        r"sidebarBackdrop\.setAttribute\('aria-hidden',\s*'true'\)",
    )
    for pattern in required_patterns:
        assert re.search(pattern, html), pattern


def test_new_feature_panels_have_required_controls_and_accessible_charts():
    """ワールド・履歴・設定差分・安全なメンテナンスの主要DOMを固定すること"""
    _, parser = _load_frontend()
    assert {"world", "history"}.issubset(parser.nav_tabs)

    required_ids = {
        "tab-world",
        "world-map",
        "world-warning-tbody",
        "world-guild-tbody",
        "tab-history",
        "chart-fps",
        "chart-frametime",
        "chart-players",
        "chart-system",
        "history-session-tbody",
        "config-diff-tbody",
        "maintenance-action",
        "maintenance-allow-players",
        "maintenance-start",
        "maintenance-cancel",
        "maintenance-status",
    }
    assert required_ids.issubset(parser.elements)

    for chart_id in ("world-map", "chart-fps", "chart-frametime", "chart-players", "chart-system"):
        tag, attrs = parser.elements[chart_id]
        assert tag == "svg"
        assert attrs.get("role") == "img"
        assert attrs.get("aria-label")


def test_new_feature_javascript_uses_integrated_api_routes():
    """追加画面が安全な統合APIへ接続され、旧更新ボタンへ戻らないこと"""
    html, _ = _load_frontend()
    required_routes = (
        "/api/world/snapshot?limit=3000",
        "/api/history/metrics?hours=",
        "/api/history/sessions?limit=100&active_only=false",
        "/api/history/summary?hours=",
        "/api/maintenance/config-diff",
        "/api/maintenance/jobs",
        "/api/maintenance/jobs/current",
    )
    for route in required_routes:
        assert route in html

    assert "id=\"op-update\"" not in html
    assert "function doUpdate" not in html
    assert "allow_players: allowPlayers" in html
    assert "document.createElementNS('http://www.w3.org/2000/svg'" in html
    assert "function finiteNumber(value)" in html
    assert "['sampled_at', 'captured_at', 'timestamp', 'time']" in html
    assert "if (maintenanceBusy) return;" in html
    assert "MAINTENANCE_REFRESH_MS" in html
    assert "world-warning-note" in html
    assert "guilds_truncated" in html
    assert "skipped: '省略'" in html
    # メンテナンス実行中に起動/停止を無効化する処理（renderMaintenanceStatus）
    assert "[data-svc=\"start\"], [data-svc=\"stop\"]" in html
    # サービス制御ボタンが3種類そろっていること（クイックアクションに配置）
    for action in ("start", "stop", "restart"):
        assert f'data-svc="{action}"' in html, action


def test_dashboard_kpi_and_quick_actions_are_present():
    """ダッシュボードのKPI・推移グラフ・クイックアクションを固定すること"""
    html, parser = _load_frontend()

    required_ids = {
        # KPI カード（稼働状況・プレイヤー数・CPU・メモリ・ディスク）
        "svc-dot",
        "svc-label",
        "kpi-uptime-sub",
        "m-players",
        "m-maxplayers",
        "kpi-cpu-value",
        "kpi-mem-value",
        "kpi-disk-value",
        # スパークラインと推移グラフ
        "spark-cpu",
        "spark-mem",
        "spark-disk",
        "dash-chart-players",
        "dash-chart-hours",
        # サーバー情報とクイックアクション
        "i-booted",
        "i-address",
        "i-pid",
        "quick-save",
        "quick-announce",
        "dashboard-reload",
    }
    assert required_ids.issubset(parser.elements)

    # グラフは支援技術向けに role と aria-label を持つ SVG であること
    for chart_id in ("spark-cpu", "spark-mem", "spark-disk", "dash-chart-players"):
        tag, attrs = parser.elements[chart_id]
        assert tag == "svg"
        assert attrs.get("role") == "img"
        assert attrs.get("aria-label")

    # 履歴タブのグラフとIDが衝突していないこと
    assert "dash-chart-players" != "chart-players"
    assert "chart-players" in parser.elements

    # warming（cpu_percent が null）を 0% と誤表示しないこと
    assert "function setKpiPercent(" in html
    assert "pct != null ? pct.toFixed(1) + '%' : '–'" in html
    # running の 3値（true/false/null）を区別すること
    assert "svc.running === null" in html
    assert "'状態不明'" in html
    # 起動日時はクライアント側で uptime から算出すること
    assert "Date.now() - metrics.uptime * 1000" in html
    # 履歴APIを1秒ポーリングで叩かないこと
    assert "DASH_CHART_REFRESH_MS" in html


def test_event_log_is_structured_on_the_frontend():
    """イベントログを /ws/logs の生テキストからフロント側で構造化すること"""
    html, parser = _load_frontend()

    required_ids = {
        "evt-level",
        "evt-category",
        "evt-search",
        "evt-clear",
        "evt-toggle-all",
        "evt-tbody",
        "evt-dot",
        "evt-status-txt",
    }
    assert required_ids.issubset(parser.elements)

    # パーサ本体（時刻・レベル・カテゴリの推定）
    assert "function parseLogLine(" in html
    assert "function parseLogTimestamp(" in html
    assert "LOG_LEVEL_RULES" in html
    assert "LOG_CATEGORY_RULES" in html
    # 行に時刻が無い場合は受信時刻で代用し、推定であることを示すこと
    assert "tsSource" in html
    assert "受信時刻（ログ行に時刻情報がありません）" in html
    # リングバッファで際限なく溜め込まないこと
    assert "EVENT_LOG_MAX" in html
    assert "eventLogEntries.length = EVENT_LOG_MAX" in html
    # 高頻度ログでも1行ごとに再描画しないこと
    assert "requestAnimationFrame" in html
    # WebSocket は1本を共有し、ターミナルと表の両方へ流すこと
    assert "appendLog(e.data); pushEventLog(e.data);" in html
    # バックエンドは変更しないので、専用のイベントログAPIは呼ばないこと
    assert "/api/logs/events" not in html


def test_frontend_never_uses_inner_html():
    """XSS対策として innerHTML を使わず textContent のみで組み立てること"""
    html, _ = _load_frontend()
    assert "innerHTML" not in html


def test_sidebar_close_button_is_present():
    """モバイルドロワー内に閉じるボタンがあり、クリックで閉じること"""
    html, parser = _load_frontend()
    assert "sidebar-close" in parser.elements
    tag, attrs = parser.elements["sidebar-close"]
    assert tag == "button"
    assert attrs.get("aria-label") == "メニューを閉じる"
    # クリックハンドラが登録されていること
    assert re.search(r"sidebarCloseBtn\.addEventListener\('click'", html)


def test_mobile_drawer_inert_covers_content_topbar():
    """ドロワーを開いたとき #content-topbar も inert になりフォーカスが逃げないこと"""
    html, _ = _load_frontend()
    assert re.search(r"contentTopbar\.toggleAttribute\('inert'", html)
    assert re.search(r"const sidebarCloseBtn\s*=\s*document\.getElementById\('sidebar-close'\)", html)


def test_event_log_max_used_as_ringbuffer_limit():
    """EVENT_LOG_MAX がリングバッファの上限チェックに直接使われていること"""
    html, _ = _load_frontend()
    # 定数宣言
    assert re.search(r"const EVENT_LOG_MAX\s*=\s*[1-9]\d+", html)
    # 配列の切り詰めに使われていること（変異で上限を無効化すると消える）
    assert re.search(r"eventLogEntries\.length\s*=\s*EVENT_LOG_MAX", html)


def test_dash_chart_refresh_ms_is_positive():
    """履歴チャートのスロットル間隔が 1 秒を超える正の値であること"""
    html, _ = _load_frontend()
    m = re.search(r"const DASH_CHART_REFRESH_MS\s*=\s*(\d+)", html)
    assert m, "DASH_CHART_REFRESH_MS が見つかりません"
    assert int(m.group(1)) > 1000, f"スロットル間隔が短すぎます: {m.group(1)}ms"


def test_palworld_info_card_is_inside_resource_grid():
    """Palworld サーバー情報カードが .resource-grid の2枚目に配置されていること"""
    html, _ = _load_frontend()
    # ホスト情報カードが resource-grid 内にあることに加え、
    # 同じ resource-grid ブロック内に Palworld 情報カードも含まれること
    m = re.search(
        r'<div class="resource-grid"[^>]*>(.*?)</div>\s*</div>\s*<!-- (?:.*?)?',
        html, re.DOTALL
    )
    # DOM パーサで resource-grid を探し、その中に両カードのタイトルがあることを確認する
    grid_start = html.find('<div class="resource-grid"')
    assert grid_start != -1, ".resource-grid が見つかりません"
    # 開始タグから終了タグまでのブロックを抽出する（ネスト深度を追跡）
    depth = 0
    i = grid_start
    grid_end = -1
    while i < len(html):
        if html[i:i+4] == "<div":
            depth += 1
            i += 4
        elif html[i:i+6] == "</div>":
            depth -= 1
            if depth == 0:
                grid_end = i + 6
                break
            i += 6
        else:
            i += 1
    assert grid_end != -1, ".resource-grid の終了タグが見つかりません"
    grid_html = html[grid_start:grid_end]
    assert "ホストサーバー情報" in grid_html, "ホストサーバー情報が .resource-grid 内にありません"
    assert "Palworld サーバー情報" in grid_html, "Palworld サーバー情報が .resource-grid 内にありません"


def test_close_paths_include_sidebar_close_button():
    """ドロワーを閉じる4経路（ハンバーガー・背景・sidebar-close・Escape）が揃っていること"""
    html, _ = _load_frontend()
    # sidebar-close のクリックで閉じること（新規追加された閉じる経路）
    assert re.search(r"sidebarCloseBtn\.addEventListener\('click'.*closeMobileSidebar", html, re.DOTALL)
    # 背景タップで閉じること
    assert re.search(r"sidebarBackdrop\.addEventListener\('click'.*closeMobileSidebar", html, re.DOTALL)
    # Escape で閉じること
    assert re.search(r"event\.key\s*!==\s*'Escape'", html)
    # ナビ項目クリックで閉じること
    assert re.search(r"closeMobileSidebar\(true\)", html)


def test_ranking_csv_export_uses_selected_metric_and_releases_blob_url():
    """選択中の3指標を読み取り専用CSV APIへ渡し、Blob URLを後始末すること"""
    html, parser = _load_frontend()
    button_tag, button = parser.elements["ranking-csv-export"]

    assert button_tag == "button"
    assert button.get("type") == "button"
    assert "/api/ranking/export.csv?metric=" in html
    assert "encodeURIComponent(metric)" in html
    assert "response.blob()" in html
    assert "URL.createObjectURL(blob)" in html
    assert "URL.revokeObjectURL(objectUrl)" in html
    assert re.search(
        r"ranking-csv-export'\)\.addEventListener\('click',\s*exportRankingCsv\)",
        html,
    )
    assert "function compareRankingPlayers(a, b, metric)" in html
    assert "players.sort((a, b) => compareRankingPlayers(a, b, metric))" in html
    assert "['userId', 'name', 'accountName']" in html


def test_update_password_modal_keeps_secret_out_of_dom_and_storage():
    """更新時だけsudoパスワードを求め、送信後にDOM参照を消すこと"""
    html, parser = _load_frontend()
    input_tag, password_input = parser.elements["sudo-password-input"]
    form_tag, _ = parser.elements["sudo-password-form"]

    assert input_tag == "input"
    assert password_input.get("type") == "password"
    assert password_input.get("autocomplete") == "new-password"
    assert password_input.get("value") is None
    assert password_input.get("maxlength") == "1024"
    assert form_tag == "form"
    assert "if (action === 'update')" in html
    assert "openSudoPasswordModal(payload, label)" in html
    assert "sudo_password: sudoPassword" in html
    assert "delete payload.sudo_password" in html
    assert "sudoPassword = ''" in html
    assert "document.getElementById('sudo-password-input').value = ''" in html
    assert "cache: 'no-store'" in html
    assert not re.search(
        r"(?:localStorage|sessionStorage)\.(?:setItem|getItem)\([^)]*sudo",
        html,
        re.IGNORECASE,
    )


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    failed = []
    for test in tests:
        try:
            test()
            print(f"  OK  {test.__name__}")
        except Exception as exc:
            print(f" FAIL {test.__name__}: {exc}")
            failed.append(test.__name__)
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(tests)}")
        return 1
    print(f"ALL {len(tests)} TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
