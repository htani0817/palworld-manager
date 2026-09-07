# -*- coding: utf-8 -*-
"""実サーバーに接続しない画面検証用サーバー（標準ライブラリのみ）。"""

import base64
import hashlib
import json
import math
import os
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".preview-data" / "server"
CONFIG = DATA / "Saved" / "Config" / "LinuxServer"
CONFIG.mkdir(parents=True, exist_ok=True)
SAVES = DATA / "Saved" / "SaveGames" / "0" / "DEMO-WORLD"
SAVES.mkdir(parents=True, exist_ok=True)
for name, content in {
    "Engine.ini": "[OnlineSubsystemUtils.IpNetDriver]\nNetServerMaxTickRate=60\nMaxClientRate=100000\n\n[/Script/Engine.Engine]\nbSmoothFrameRate=False\n",
    "Game.ini": "[/Script/Engine.GameSession]\nMaxPlayers=32\n",
    "GameUserSettings.ini": "[/Script/Engine.GameUserSettings]\nFrameRateLimit=60.000000\n",
    "PalWorldSettings.ini": '[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(ServerName="Palworld preview",ExpRate=1.000000,DayTimeSpeedRate=1.000000)\n',
}.items():
    path = CONFIG / name
    if not path.exists():
        path.write_text(content, encoding="utf-8", newline="\n")
if not (SAVES / "Level.sav").exists():
    (SAVES / "Level.sav").write_bytes(b"preview-only-not-a-real-world")
os.environ["PAL_SETTINGS_INI"] = str(CONFIG / "PalWorldSettings.ini")
os.environ["PAL_BACKUP_DIR"] = str(DATA / "backups")
sys.path.insert(0, str(ROOT / "backend"))
import backup_store
import workspace_files

if not backup_store.summaries():
    backup_store.create("daily-world", "画面確認用のサンプルバックアップ", ["saves"])
    backup_store.create("before-update", "更新前の設定・セーブデータ（プレビュー）", ["config", "saves"])

STATE = {"running": True, "job": None, "schedule": {"enabled": False, "cron": "0 3 * * *", "paths": ["config", "saves"], "timezone": "Asia/Tokyo", "next_run": None}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def respond(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        url = urlsplit(self.path)
        path, q = url.path, parse_qs(url.query)
        if path == "/ws/logs":
            key = self.headers.get("Sec-WebSocket-Key", "")
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode())
            self.end_headers()
            try:
                for text in ["[INFO] プレビュー環境です。実サーバーには接続していません。", "[INFO] Palworld Server Manager の画面確認を開始しました", "[INFO] サンプルワールドと設定ファイルを読み込みました"]:
                    data = text.encode(); self.wfile.write(bytes([0x81, 126]) + struct.pack("!H", len(data)) + data); self.wfile.flush()
                while True:
                    time.sleep(15); self.wfile.write(b"\x89\x00"); self.wfile.flush()
            except (OSError, ConnectionError):
                pass
            return
        if not path.startswith("/api/"):
            relative = "index.html" if path == "/" else path.lstrip("/")
            target = (ROOT / "frontend" / relative).resolve()
            if not target.is_relative_to((ROOT / "frontend").resolve()) or not target.is_file():
                self.send_error(404); return
            data = target.read_bytes(); self.send_response(200)
            self.send_header("Content-Type", {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}.get(target.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        try:
            result = self.api_get(path, q)
            self.respond(result)
        except Exception as error:
            self.respond({"detail": str(error)}, 400)

    def api_get(self, path, q):
        if path == "/api/config/env":
            return {"env": "preview", "label": "[プレビュー・サンプルデータ]", "color": "#537b64", "is_production": False, "pal_host": "preview.local", "pal_port": 8212}
        if path == "/api/system/service-status":
            return {"running": STATE["running"], "query_ok": True, "status": "active" if STATE["running"] else "inactive"}
        if path == "/api/server/info":
            return {"servername": "Palworld / Preview", "version": "sample", "description": "画面確認用サーバー（実サーバー未接続）", "worldguid": "DEMO-WORLD"}
        if path == "/api/server/metrics":
            return {"currentplayernum": 3, "maxplayernum": 32, "serverfps": 59.9, "serverframetime": 16.69, "uptime": 125376, "days": 264, "basecampnum": 8}
        if path == "/api/system/metrics":
            return {"host": {"os_pretty_name": "Linux · サンプル"}, "system": {"cpu_percent": 13.2, "cpu_count": 8, "mem_percent": 36.8, "mem_total_gb": 32, "mem_used_gb": 11.8, "disk_percent": 24.3, "disk_used_gb": 58.4, "disk_total_gb": 240}, "palworld": {"running": STATE["running"], "pid": 12345, "cpu_percent": 9.2, "mem_mb": 8420}}
        if path == "/api/system/notify-status": return {"discord_enabled": False}
        if path == "/api/shutdown-schedule/": return {"state": "unset"}
        if path == "/api/maintenance/jobs/current": return {"job": None}
        if path == "/api/maintenance/config-diff": return {"summary": {}, "diffs": []}
        if path == "/api/server/players":
            return {"players": [{"name": name, "accountName": name, "userId": "steam_preview_" + str(i), "level": level, "ping": ping, "location_x": 1000, "location_y": 2500} for i, (name, level, ping) in enumerate([("Yuki", 55, 21), ("Haru", 43, 18), ("Aoi", 48, 32)])]}
        if path == "/api/history/metrics":
            return {"metrics": [{"sampled_at": time.time() - (119 - i) * 30, "system_cpu_percent": round(12 + math.sin(i / 8) * 4, 1), "system_memory_percent": round(35 + math.sin(i / 18) * 3, 1), "current_players": 2 + int(i > 45), "server_fps": 59 + math.sin(i / 9), "server_frame_time_ms": 16.7} for i in range(120)]}
        if path == "/api/history/sessions": return {"sessions": []}
        if path == "/api/history/summary": return {}
        if path == "/api/schedule/": return []
        if path == "/api/ranking/": return {"players": []}
        if path == "/api/world/snapshot": return {"supported": False, "summary": {}, "warnings": [], "guilds": [], "points": []}
        if path == "/api/files": return workspace_files.listing(q.get("path", ["config"])[0])
        if path == "/api/files/content": return workspace_files.read_text(q["path"][0])
        if path == "/api/backups": return {"backups": backup_store.summaries()}
        if path == "/api/backups/schedule": return STATE["schedule"]
        if path == "/api/backups/jobs/current": return {"job": STATE["job"]}
        if path.startswith("/api/backups/"): return backup_store.detail(path.rsplit("/", 1)[1])
        if path == "/api/ini/settings": return {"values": {"ServerName": "Palworld / Preview", "ExpRate": "1.000000", "DayTimeSpeedRate": "1.000000", "PalCaptureRate": "1.000000", "PlayerDamageRateAttack": "1.000000", "ServerPlayerMaxNum": "32"}, "source": "ini", "readonly": True, "secret_keys": []}
        raise ValueError("このプレビューでは未対応のAPIです")

    def do_POST(self):
        self.mutate()

    def do_PUT(self):
        self.mutate()

    def do_DELETE(self):
        self.mutate()

    def do_PATCH(self):
        self.respond({"detail": "プレビューのゲーム設定は読み取り専用です"}, 409)

    def mutate(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            path = urlsplit(self.path).path
            if path == "/api/files/content":
                return self.respond(workspace_files.write_text(data["path"], data["content"], data["revision"]))
            if path == "/api/backups/schedule":
                STATE["schedule"].update(data); return self.respond(STATE["schedule"])
            if path == "/api/backups":
                result = backup_store.create(data["label"], data.get("description", ""), data["paths"], "live" if STATE["running"] else "stopped")
                STATE["job"] = {"id": result["id"], "state": "completed", "message": "プレビュー用バックアップを作成しました", "result": {"backup_id": result["id"]}}
                return self.respond({"job": STATE["job"]}, 202)
            if path.endswith("/restore"):
                if STATE["running"]: return self.respond({"detail": "復元前にプレビューのサーバーを停止してください"}, 409)
                result = backup_store.restore(path.split("/")[-2]); STATE["job"] = {"id": str(time.time()), "state": "completed", "message": "プレビューのファイルを復元しました", "result": result}
                return self.respond({"job": STATE["job"]}, 202)
            if self.command == "DELETE" and path.startswith("/api/backups/"):
                return self.respond(backup_store.move_to_trash(path.rsplit("/", 1)[1]))
            if path.startswith("/api/system/"):
                action = path.rsplit("/", 1)[1]
                if action in {"start", "stop"}: STATE["running"] = action == "start"
                return self.respond({"result": "preview"})
            return self.respond({"result": "preview", "message": "実サーバーには操作していません"})
        except Exception as error:
            self.respond({"detail": str(error)}, 409)


if __name__ == "__main__":
    print("画面プレビュー: http://127.0.0.1:18765 （実サーバー未接続）", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 18765), Handler).serve_forever()
