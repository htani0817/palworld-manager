"""
プレイヤーランキング収集（累計プレイ時間・最高レベル・ログイン日数）。

Palworld REST API はプレイ時間や接続履歴を返さないため、/players を
定期サンプリングし、前回と今回の両方に存在した userId へ単調時計の
実経過秒を加算して累計プレイ時間を導出する（誤差上限 ≒ サンプル間隔）。

耐久性の設計（イベント期間中のデータを失わないため）:
- 毎サンプル、一時ファイル + os.replace のアトミック書き込みで JSON 永続化
  （schedules.json と同じパターン。強制終了でも半端なファイルにならない）
- 一定間隔で .bak へコピーし、万一の破損時のフォールバックにする
- 起動時に JSON を読み込み、続きから積算を再開する（リセットしない）
- 壊れたファイルは .corrupt-日時 に退避してから新規開始（黙って捨てない）

計上ルール:
- API 取得失敗（Palworld 停止中など）はベースラインをリセットし、その間は計上しない
- 応答の形式が不正な場合（JSON でない・players がリストでない等）も失敗と同扱いにする
- サンプル間隔の 3 倍を超える異常ギャップも計上しない（イベントループ停滞対策）
- ログイン日数は SCHEDULE_TIMEZONE 基準の日付で「オンラインを観測した日のユニーク数」
- 最高レベルは観測した level の最大値（下がっても保持する）
"""
import asyncio
import json
import logging
import math
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 以下のフォールバック
    ZoneInfo = None  # type: ignore

import palworld_client as pal
from config import settings

logger = logging.getLogger("palworld_manager.ranking")

RANKING_FILE = Path(__file__).parent / "ranking.json"
BACKUP_FILE = Path(__file__).parent / "ranking.json.bak"

SAMPLE_INTERVAL_SECONDS = 10
# サンプル間隔の何倍を超えたら異常ギャップとして計上しないか
MAX_GAP_FACTOR = 3
# .bak へコピーする間隔（秒）
BACKUP_INTERVAL_SECONDS = 600

# ランキングデータ本体（load_data() で読み込み、sample() が更新する）
_data: dict = {}
# 前回サンプルでオンラインだった userId（プロセス内のみ。再起動で一旦空になる）
_prev_ids: set[str] = set()
# 前回サンプルの単調時計時刻（壁時計のズレに影響されない経過計測用）
_prev_mono: Optional[float] = None
_last_backup_mono: Optional[float] = None
_lock = asyncio.Lock()
# タイムゾーン解釈失敗の警告を1回だけ出すためのフラグ
_tz_warned = False


def _tz():
    global _tz_warned
    if ZoneInfo is None:
        if not _tz_warned:
            logger.warning("zoneinfo が利用できないため OS ローカル時刻で日付判定します")
            _tz_warned = True
        return None
    try:
        return ZoneInfo(settings.schedule_timezone)
    except Exception:
        if not _tz_warned:
            logger.warning(
                "SCHEDULE_TIMEZONE '%s' を解釈できないため OS ローカル時刻で日付判定します",
                settings.schedule_timezone,
            )
            _tz_warned = True
        return None


def _now() -> datetime:
    tz = _tz()
    return datetime.now(tz) if tz else datetime.now()


def _today(now: Optional[datetime] = None) -> str:
    """SCHEDULE_TIMEZONE 基準の日付文字列（ログイン日数の境界判定用）"""
    tz = _tz()
    if now is None:
        return (_now()).strftime("%Y-%m-%d")
    if tz is not None and now.tzinfo is not None:
        now = now.astimezone(tz)
    return now.strftime("%Y-%m-%d")


def _as_float(v, default: float) -> float:
    """0 以上の有限な float へ正規化する。変換不能・非有限（NaN/inf）・負数は既定値"""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(f) or f < 0:
        return default
    return f


def _as_int(v, default: int) -> int:
    """0 以上の有限な int へ正規化する。変換不能・非有限（NaN/inf）・負数は既定値"""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(f) or f < 0:
        return default
    return int(f)


def _empty_data() -> dict:
    return {
        "version": 1,
        "started_at": _now().isoformat(),
        "last_updated": None,
        "players": {},
    }


def _read_json(path: Path) -> Optional[dict]:
    """JSON を読み、スキーマとして最低限妥当なら返す。壊れていれば None。

    UnicodeError も破損扱いにする（不正な文字コードで起動不能にならないため）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("players"), dict):
        return None
    return data


def _sanitize(data: dict) -> dict:
    """読み込んだデータの型を正規化する。

    手編集や部分破損で型が壊れたレコードが混ざっていても、積算や API 返却で
    例外にならないよう、数値は変換できなければ既定値に倒し、dict でない
    プレイヤーレコードは警告を出して破棄する。
    """
    cleaned = {}
    for user_id, e in data.get("players", {}).items():
        if not isinstance(e, dict):
            logger.warning("ranking.json 内の不正なレコードを破棄しました: %s", user_id)
            continue
        last_login = e.get("last_login_date")
        cleaned[str(user_id)] = {
            "name": str(e.get("name") or ""),
            "accountName": str(e.get("accountName") or ""),
            "total_seconds": _as_float(e.get("total_seconds"), 0.0),
            "max_level": _as_int(e.get("max_level"), 0),
            "login_days": _as_int(e.get("login_days"), 0),
            "last_login_date": last_login if isinstance(last_login, str) else None,
            "first_seen": str(e.get("first_seen") or ""),
            "last_seen": str(e.get("last_seen") or ""),
        }
    data["players"] = cleaned
    return data


def load_data() -> None:
    """起動時に保存済みランキングを読み込み、続きから積算できる状態にする"""
    global _data, _prev_ids, _prev_mono
    _prev_ids = set()
    _prev_mono = None
    data = None
    if RANKING_FILE.exists():
        data = _read_json(RANKING_FILE)
        if data is None:
            # 壊れたファイルは退避してから .bak にフォールバック（黙って捨てない）
            quarantine = RANKING_FILE.with_name(
                f"ranking.json.corrupt-{datetime.now():%Y%m%d_%H%M%S}"
            )
            try:
                os.replace(RANKING_FILE, quarantine)
                logger.error("ranking.json が壊れていたため %s に退避しました", quarantine.name)
            except OSError:
                pass
    if data is None and BACKUP_FILE.exists():
        data = _read_json(BACKUP_FILE)
        if data is not None:
            logger.warning("ranking.json.bak からランキングデータを復元しました")
    if data is None:
        data = _empty_data()
    _data = _sanitize(data)


def _save() -> None:
    """一時ファイルに書いてから os.replace で置換（書き込み中断による破損を防ぐ）"""
    payload = json.dumps(_data, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(RANKING_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, RANKING_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _backup_if_due() -> None:
    """前回バックアップから一定時間経過していたら .bak を更新する。

    既存の正常な .bak を更新途中の強制終了で壊さないよう、本体と同じく
    一時ファイルへ書いてから os.replace で置換する。失敗しても収集は止めない。
    """
    global _last_backup_mono
    now = time.monotonic()
    if _last_backup_mono is not None and (now - _last_backup_mono) < BACKUP_INTERVAL_SECONDS:
        return
    try:
        payload = RANKING_FILE.read_bytes()
        fd, tmp = tempfile.mkstemp(dir=str(BACKUP_FILE.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, BACKUP_FILE)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        _last_backup_mono = now
    except OSError:
        pass


def _apply_sample(
    data: dict, players: list, elapsed: float, today: str, prev_ids: set
) -> tuple:
    """1 サンプル分をランキングデータへ反映する（I/O なし。テスト対象の中核）。

    - elapsed 秒は「前回もオンラインだった userId」だけに加算する
      （接続直後のプレイヤーは次サンプルから計上される）
    - 戻り値: (今回オンラインの userId 集合, データに変化があったか)
    """
    entries = data.setdefault("players", {})
    current_ids: set = set()
    changed = False
    now_iso = _now().isoformat()
    for p in players:
        user_id = str(p.get("userId") or "").strip()
        if not user_id:
            continue  # 識別子がないエントリは積算できないためスキップ
        if user_id in current_ids:
            continue  # 同一応答内の重複 userId は二重加算になるため最初の1件だけ使う
        current_ids.add(user_id)
        e = entries.get(user_id)
        if e is None:
            e = {
                "name": "",
                "accountName": "",
                "total_seconds": 0.0,
                "max_level": 0,
                "login_days": 0,
                "last_login_date": None,
                "first_seen": now_iso,
                "last_seen": now_iso,
            }
            entries[user_id] = e
        # 表示名は最新の観測値で上書き（name は変わりうるため）
        name = p.get("name")
        if name:
            e["name"] = str(name)
        account = p.get("accountName")
        if account:
            e["accountName"] = str(account)
        # level も API 由来のため正規化して比較する（inf 等での例外を防ぐ）
        level = _as_int(p.get("level"), 0)
        if level > _as_int(e.get("max_level"), 0):
            e["max_level"] = level
        if e.get("last_login_date") != today:
            e["last_login_date"] = today
            e["login_days"] = int(e.get("login_days", 0)) + 1
        e["last_seen"] = now_iso
        if elapsed > 0 and user_id in prev_ids:
            e["total_seconds"] = _as_float(e.get("total_seconds"), 0.0) + elapsed
        changed = True
    if changed:
        data["last_updated"] = now_iso
    return current_ids, changed


def _validate_players(resp) -> list:
    """/players 応答の形を検証し、プレイヤー dict のリストを返す。不正なら ValueError"""
    if not isinstance(resp, dict):
        raise ValueError("応答がオブジェクトではありません")
    players = resp.get("players")
    if players is None:
        return []
    if not isinstance(players, list):
        raise ValueError("players がリストではありません")
    for p in players:
        if not isinstance(p, dict):
            raise ValueError("players の要素がオブジェクトではありません")
    return players


async def sample() -> None:
    """定期実行の本体（APScheduler の interval ジョブから呼ばれる）"""
    global _prev_ids, _prev_mono
    async with _lock:
        try:
            resp = await pal.get_players()
            players = _validate_players(resp)
        except ValueError:
            # 通信は成功したが応答の形式が不正。計上すると誤加算になるため失敗と同扱いにする
            logger.warning("players 応答の形式が不正のため、このサンプルは計上しません")
            _prev_ids = set()
            _prev_mono = None
            return
        except Exception:
            # Palworld 停止中などは計上しない。ベースラインを倒して復帰後に再検出する
            _prev_ids = set()
            _prev_mono = None
            return
        now_mono = time.monotonic()
        elapsed = 0.0
        if _prev_mono is not None:
            gap = now_mono - _prev_mono
            # 異常ギャップ（イベントループ停滞など）は計上せずベースラインだけ進める
            if 0 < gap <= SAMPLE_INTERVAL_SECONDS * MAX_GAP_FACTOR:
                elapsed = gap
        today = _today()
        current_ids, changed = _apply_sample(_data, players, elapsed, today, _prev_ids)
        _prev_ids = current_ids
        _prev_mono = now_mono
        if changed:
            try:
                _save()
                _backup_if_due()
            except Exception:
                logger.exception("ranking.json の保存に失敗しました")


def get_ranking_view() -> dict:
    """API 返却用のランキング一覧（既定は累計プレイ時間の降順）"""
    players = []
    for user_id, e in _data.get("players", {}).items():
        players.append({
            "userId": user_id,
            "name": e.get("name") or "",
            "accountName": e.get("accountName") or "",
            "total_seconds": round(_as_float(e.get("total_seconds"), 0.0), 1),
            "max_level": _as_int(e.get("max_level"), 0),
            "login_days": _as_int(e.get("login_days"), 0),
            "last_seen": e.get("last_seen"),
            "online": user_id in _prev_ids,
        })
    players.sort(key=lambda p: p["total_seconds"], reverse=True)
    return {
        "started_at": _data.get("started_at"),
        "updated": _data.get("last_updated"),
        "sample_interval": SAMPLE_INTERVAL_SECONDS,
        "players": players,
    }
