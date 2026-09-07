# Palworld Server Manager

Palworld 専用のブラウザベース管理 UI です。各サーバーに配置して利用します。暗色と緑を基調にしたワークスペースで、監視、設定、コンソール、バックアップをタブで開けます。画面右上からライトテーマにも切り替えられます。

画面刷新に伴う追加機能、バックアップの仕様、検証の範囲は [[WORKSPACE-UPGRADE]] を参照してください。

## 機能

- **ワークスペース UI** — 左側のアイコンナビとファイル一覧、上部の画面タブ、サーバー操作ボタンを配置。設定ファイルの複数タブ、未保存表示、キーボード保存、機能検索、環境表示、ライト/ダーク切替に対応。新しいCSSとJavaScriptは `frontend/ui/` から配信する
- **バックアップ管理** — 設定とセーブデータを選んで作成し、一覧・詳細・収録ファイルを確認。cron式による定期実行と停止中のファイル復元に対応する。復元前退避、チェックサム検証、更新・再起動との排他制御付き。稼働中の作成は保存要求後のコピーであり、全ファイルが同じ時点とは限らない
- **ファイルエクスプローラー** — 設定領域とセーブ領域を表示。`Engine.ini`、`Game.ini`、`GameUserSettings.ini` はテキスト編集でき、変更前の退避と同時編集の検出を行う。`PalWorldSettings.ini` はパスワードを表示しない既存の専用画面で編集する
- **スマホ表示** — 画面幅に合わせてカードや入力欄を自動調整。ハンバーガーボタンでメニューを開閉でき、項目選択・背景タップ・Escape で閉じられる
- **ダッシュボード** — 稼働状況・プレイヤー数・CPU / メモリ / ディスク使用率を KPI カードで表示（リソース 3 枚には推移のスパークライン付き）。プレイヤー数の推移グラフ（期間切替可）、サーバー情報（起動日時・アドレス・プロセス ID・バージョンなど）、イベントログ、クイックアクション、ホスト情報を 1 画面に集約。1 秒自動更新（履歴グラフは 30 秒間隔）
- **イベントログ** — サーバーログを日時・レベル・カテゴリ・メッセージの表形式で表示し、レベル/カテゴリの絞り込みとメッセージ検索ができる。ログ行の内容から推定して分類するため、時刻情報を持たない行は受信時刻で代用する（その旨をツールチップで補足）
- **クイックアクション** — ダッシュボードからサーバーの起動・再起動・停止、ワールド保存、アナウンス送信（サーバー操作タブへ誘導）をワンクリックで実行
- **プレイヤー一覧** — 接続中プレイヤーの Lv / Ping / 位置 / 建築物数 表示、キック・バン操作、自動更新
- **ランキング** — 接続中プレイヤーを 10 秒間隔でサンプリングし、累計プレイ時間・最高レベル・ログイン日数を集計して順位表示（指標切替可、10 秒自動更新）。選択中の指標順でCSV出力でき、出力処理は蓄積データへ書き込まない。データは `ranking.json` へ毎サンプル atomic write で永続化され、Manager を再起動しても続きから計測。10 分ごとの `.bak` バックアップと破損時の自動復旧（`.corrupt-*` 退避 → `.bak` フォールバック）付き。プレイ時間はサンプリングによる近似値（誤差上限 ≒ 10 秒、Manager 停止中・Palworld 停止中は非計上）
- **ワールド状況** — `/game-data` からプレイヤー・パル・NPC・PalBox・ギルドを集計し、Actor の XY 座標を座標マップに表示。`frontend/assets/` にワールドマップ画像を置くと地形画像の上に固定座標系で配置し、画像がない場合は従来どおり座標分布の相対表示になる（詳細は「座標マップのマップ画像」）。低 HP、HP 0、非アクティブも一覧化する。接続情報やユーザー ID などの識別情報は画面用 API から除外し、取得結果は 15 秒キャッシュする
- **稼働履歴** — FPS、フレーム時間、接続人数、CPU、メモリを 30 秒間隔で SQLite に記録し、期間別グラフと集計を表示。グラフにカーソルを合わせると CloudWatch メトリクス風に縦ガイド線と時刻・全系列値のツールチップが出る（タッチ操作にも対応）。プレイヤーの参加・退出時刻と滞在時間もセッション履歴として確認できる。REST API で 90 秒以上観測できないセッションは最終観測付近で区切り、障害時間をプレイ時間へ加算しない。メトリクスと終了済みセッションは 30 日で自動削除する
- **コマンドパネル** — `/Info`、`/ShowPlayers`、`/Broadcast`、`/KickPlayer`、`/BanPlayer`、`/UnBanPlayer`、`/Save`、`/Shutdown`、`/DoExit` を UI から直接実行
- **設定ファイル編集・差分確認** — `PalWorldSettings.ini` の閲覧・編集（変更前に自動バックアップ生成）。保存済み INI と REST API の稼働値を正規化して比較し、再起動待ちの変更を一覧表示する。パスワード類は差分 API に含めない。INI から読み取れない場合は REST API の現在値を読み取り専用で表示
- **サーバー操作** — アナウンス送信・ワールド保存・シャットダウン・強制停止・アンバン・Discord 通知テスト
- **サーバー終了日時** — 指定した日時に自動でサーバーを停止（クイックアクションの「停止」と同じ `systemctl stop`。再起動はしない）。設定は1件のみで上書き・解除が可能。終了日時の3日前からダッシュボードにカウントダウンバナーを表示し、日時経過後は「終了日を迎えました」バナーに切り替わる（自動停止の成否に関わらず、手動で解除するまで表示し続ける）
- **安全なメンテナンス** — 再起動または Palworld 更新の前に接続人数を確認し、既定では接続者がいる操作を拒否する。必要に応じて予告アナウンスと最大 300 秒のキャンセル可能な待機を行い、ワールド保存後に処理を開始して REST API の復旧まで確認する。更新時は専用モーダルでsudoパスワードを受け取り、設定ファイルやジョブ状態には保存しない
- **再起動スケジューラ** — 1 時間刻みの時刻ボタンで毎日の再起動を登録（複数選択可）、カレンダーから単発予約、cron 式指定も可能。指定時刻からゲーム内アナウンス（5 分前 / 1 分前 / 30 秒前）+ 自動保存後に `systemctl restart`
- **Discord 通知** — ① Palworld サーバーの起動・停止を 10 秒ポーリングで検知して通知（🟢 起動 / 🔴 停止、別 Webhook 設定可）② メモリ使用率を 60 秒間隔で監視し、80% で 🟡 Warning、90% で 🔴 Critical を embed 形式で通知。回復時は ✅ 通知。同一状態が続く場合は 30 分間隔で再通知
- **リアルタイムログ** — WebSocket + journalctl によるリアルタイムストリーム。画面を開くと自動接続し、ログタブのターミナル表示とダッシュボードのイベントログへ同時に流す（接続は 1 本を共有）
- **環境バナー** — 検証環境（オレンジ）/ 本番環境（赤）を視覚的に識別

> [!note] 今回の対象外
> デスクトップアプリ、拡張機能の導入、複数サーバーの一括管理、Minecraft専用機能は対象外です。プレイヤーの参加・退出のDiscord通知や、設定だけを再読み込みする機能もありません。参加・退出はセッション履歴で確認でき、INIの変更はサーバー再起動で反映します。

> [!note] ワールド状況の対応条件
> Palworld サーバー側が REST API の `/game-data` に対応している必要があります。未対応で 404 が返る場合は、管理画面に「利用できません」と表示し、ほかの機能はそのまま利用できます。座標マップの地形表示には `frontend/assets/` へのマップ画像配置が必要です（「座標マップのマップ画像」参照。未配置でも座標分布の相対表示で動作します）。

> [!warning] インターネットへ直接公開しない
> このアプリ自体にはログイン機能がなく、再起動・更新・停止を行う管理 API を持っています。`8080/tcp` をインターネットへ直接開放せず、ファイアウォールで接続元を管理用 LAN または VPN に限定してください。外部から利用する場合は、認証と HTTPS を設定したリバースプロキシの内側へ置いてください。
>
> 「安全にアップデート」では `palworld-user` ユーザーのsudoパスワードを送信します（sudoersで `rootpw` または `targetpw` を設定している場合は、その設定に従います）。この機能を使う場合は、管理用 LAN や VPN 内でも認証付きHTTPSを使用してください。HTTPの画面へsudoパスワードを入力しないでください。`NOPASSWD` など、パスワード認証を免除する構成では入力値を検証できないため、この方式を使用しないでください。

## 新規セットアップ（初回のみ）

Manager は `/home/palworld-user/palworld-manager` に配置する前提です。すでに稼働している環境では、この手順を繰り返さず、次の「既存環境のアップデート」を使ってください。

```bash
# 1. 仮想環境を作成して依存インストール
cd /home/palworld-user/palworld-manager/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 環境変数ファイルを準備（秘密情報を含むため root 所有・0600 で配置）
sudo install -o root -g root -m 600 ../palworld-manager.env.example /etc/palworld-manager.env
sudo nano /etc/palworld-manager.env   # 値を編集

# 3. systemd に登録
sudo install -o root -g root -m 644 ../palworld-manager.service /etc/systemd/system/palworld-manager.service
sudo systemctl daemon-reload
sudo systemctl enable --now palworld-manager
```

## 既存環境のアップデート

新しい版を別フォルダへ展開し、実行中のフォルダへ必要なファイルだけを上書きします。以下では、新しい版を `/home/palworld-user/palworld-manager-update`、稼働中の版を `/home/palworld-user/palworld-manager` としています。このフォルダは Git 管理を前提としていないため、`git pull` は使いません。

> [!warning] フォルダを削除して丸ごと置き換えない
> `backend/ranking.json*` にはランキング、`backend/schedules.json` には再起動予約、`backend/manager_history.db*` にはメトリクスとプレイヤーセッション履歴が保存されています。稼働中のフォルダを削除したり、新しい版に含まれる空ファイルで上書きしたりするとデータが消えます。更新用の `rsync` に `--delete` は付けないでください。

> [!note] Web UI の「アップデート実行」とは別の手順
> Web UI のボタンは `/home/palworld-user/scripts/update.sh` を実行します。ここで説明する Palworld Manager 本体の更新には使いません。

### 更新時に残すファイル

| 対象 | 内容 |
| --- | --- |
| `backend/ranking.json*` | ランキング本体、バックアップ、破損時の退避ファイル |
| `backend/schedules.json` | 定期・単発の再起動予約 |
| `backend/shutdown_schedule.json` | サーバー終了日時の予約（1件のみ） |
| `backend/manager_history.db*` | メトリクスとプレイヤーセッション履歴の SQLite 本体、WAL、SHM |
| `backend/.venv/` | systemd サービスが使用する Python 仮想環境 |
| `backend/backups/` または `PAL_BACKUP_DIR` | ZIPバックアップ、定期予約の `schedule.json`、削除済みバックアップの `trash/` |
| `frontend/assets/` | 座標マップ用のワールドマップ画像（ユーザー配置。新版には含まれない） |
| `log/` | 起動ごとのログ |
| `/etc/palworld-manager.env` | 接続先、管理パスワード、Webhook などの設定 |
| `PAL_SETTINGS_INI` の参照先 | PalWorldSettings.ini 本体と同じ場所に作られるバックアップ |

`/etc/palworld-manager.env` はアプリフォルダの外にあります。更新時に `palworld-manager.env.example` を再インストールして上書きしないでください。新しい環境変数が追加された場合だけ、サンプルを見ながら不足分を手動で追記します。

### 1. 更新元とバックアップ先を確認する

`rsync` が必要です。新版は稼働中のフォルダへ直接展開せず、`palworld-manager-update` に置いてください。以下のコマンドは同じシェルで、手順 1〜5 の順に実行します。

```bash
release_dir=/home/palworld-user/palworld-manager-update
app_dir=/home/palworld-user/palworld-manager
backup_dir="/home/palworld-user/palworld-manager-backup/$(date +%Y%m%d-%H%M%S)"

# パスの取り違えを防ぐ
test -f "$release_dir/backend/main.py"
test -f "$app_dir/backend/main.py"
command -v rsync
```

### 2. Manager を停止してバックアップする

Palworld サーバー本体は停止しません。Manager だけを止め、JSON への書き込みが終わってからバックアップします。仮想環境とログは稼働中のフォルダに残るため、容量を抑えるためバックアップ対象から外します。

```bash
sudo systemctl stop palworld-manager

sudo install -d -o root -g root -m 700 "$backup_dir/app"
sudo rsync -a \
  --exclude='/backend/.venv/' \
  --exclude='/log/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  "$app_dir/" "$backup_dir/app/"

# 秘密情報を含むため、環境変数のバックアップは root のみ読み取り可能にする
sudo install -o root -g root -m 600 \
  /etc/palworld-manager.env "$backup_dir/palworld-manager.env"
```

### 3. 実行時データを除外して上書きする

最初に dry-run の結果を確認します。`ranking.json*`、`schedules.json`、`manager_history.db*`、`.venv/`、`log/` がコピー対象に出ていないことを確認してから、同じ条件で更新します。コピー元とコピー先の末尾の `/` は省略しないでください。

```bash
# dry-run
sudo rsync -ain \
  --exclude='/backend/backups/' \
  --exclude='/.preview-data/' \
  --exclude='/.dev-deps/' \
  --exclude='/backend/.venv/' \
  --exclude='/backend/schedules.json' \
  --exclude='/backend/shutdown_schedule.json' \
  --exclude='/backend/ranking.json*' \
  --exclude='/backend/manager_history.db*' \
  --exclude='/log/' \
  --exclude='.env' \
  --exclude='palworld-manager.env' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  "$release_dir/" "$app_dir/"

# dry-run に問題がなければ更新
sudo rsync -a \
  --exclude='/backend/backups/' \
  --exclude='/.preview-data/' \
  --exclude='/.dev-deps/' \
  --exclude='/backend/.venv/' \
  --exclude='/backend/schedules.json' \
  --exclude='/backend/shutdown_schedule.json' \
  --exclude='/backend/ranking.json*' \
  --exclude='/backend/manager_history.db*' \
  --exclude='/log/' \
  --exclude='.env' \
  --exclude='palworld-manager.env' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  "$release_dir/" "$app_dir/"
```

### 4. 依存関係と設定を更新する

仮想環境は作り直さず、既存の `.venv` に必要な依存関係を反映します。`palworld-manager.service` も更新し、`User=root` の設定を維持します。

```bash
sudo "$app_dir/backend/.venv/bin/python" -m pip install \
  -r "$app_dir/backend/requirements.txt"

sudo install -o root -g root -m 644 \
  "$app_dir/palworld-manager.service" \
  /etc/systemd/system/palworld-manager.service
sudo systemctl daemon-reload

# サンプルと現在の設定について、値を出さず変数名だけを表示する
echo 'sample:'
sed -nE 's/^#?[[:space:]]*([A-Z_][A-Z0-9_]*)=.*/\1/p' \
  "$app_dir/palworld-manager.env.example"
echo 'current:'
sudo sed -nE 's/^#?[[:space:]]*([A-Z_][A-Z0-9_]*)=.*/\1/p' \
  /etc/palworld-manager.env
```

### 5. テスト後に起動する

pytest は必須依存に含まれていないため、直接実行できるテストを使います。1 つでも失敗した場合はサービスを起動せず、`$backup_dir` の内容から復旧してください。

```bash
cd "$app_dir/backend"
sudo "$app_dir/backend/.venv/bin/python" tests/test_logging_config.py
sudo "$app_dir/backend/.venv/bin/python" tests/test_frontend_responsive.py
sudo "$app_dir/backend/.venv/bin/python" tests/test_workspace_files.py
sudo "$app_dir/backend/.venv/bin/python" -m unittest discover -s tests -p test_workspace_api.py -v
sudo "$app_dir/backend/.venv/bin/python" tests/test_system_metrics.py
sudo "$app_dir/backend/.venv/bin/python" tests/test_regressions.py
sudo "$app_dir/backend/.venv/bin/python" tests/test_shutdown_schedule.py
sudo "$app_dir/backend/.venv/bin/python" tests/test_ranking.py
sudo "$app_dir/backend/.venv/bin/python" tests/test_ranking_csv.py
sudo "$app_dir/backend/.venv/bin/python" tests/test_world_snapshot.py
sudo "$app_dir/backend/.venv/bin/python" tests/test_history_store.py
sudo "$app_dir/backend/.venv/bin/python" tests/test_config_diff.py
sudo "$app_dir/backend/.venv/bin/python" tests/test_maintenance.py
sudo "$app_dir/backend/.venv/bin/python" tests/test_maintenance_api.py

# 状態ファイルが残っていることを確認
for state_file in \
  "$app_dir/backend/schedules.json" \
  "$app_dir/backend/shutdown_schedule.json" \
  "$app_dir"/backend/ranking.json* \
  "$app_dir"/backend/manager_history.db*; do
  if sudo test -f "$state_file"; then
    sudo stat -c '%n  %s bytes' "$state_file"
  fi
done

sudo systemctl start palworld-manager
sudo systemctl --no-pager --full status palworld-manager
```

起動後、ダッシュボードのホストサーバー情報に `Ubuntu 26.04 LTS` と表示され、ランキング、再起動スケジュール、稼働履歴が残っていることを確認します。起動に失敗した場合は、最新のファイルログか `sudo journalctl -u palworld-manager -n 100 --no-pager` を確認してください。

## Web UI の「安全にアップデート」に必要な外部スクリプト

Web UI の「安全にアップデート」は、`backend/maintenance.py` の `UPDATE_SCRIPT = "/home/palworld-user/scripts/update.sh"` を直接実行します。このパスはコード内の定数で、環境変数では変更できません。**このスクリプトをサーバー機の該当パスに用意しないと、アップデート機能は動作しません**（再起動・その他の機能には影響しません）。実行ユーザーとsudo認証の要件は「[systemctl 実行権限](#systemctl-実行権限)」を参照してください。

### サンプル実装（`/home/palworld-user/scripts/update.sh`）

SteamCMD で PalServer 本体を更新する最小構成の例です。実際の環境に合わせて調整し、実行権限を付与してください。

```bash
#!/bin/bash

echo "[$(date)] パルワールドサーバーのアップデートチェックを開始します..."

# SteamCMDを実行してアップデート
/usr/games/steamcmd +force_install_dir /home/palworld-user/PalServer +login anonymous +app_update 2394010 validate +quit

echo "[$(date)] アップデート処理が完了しました。"
```

```bash
sudo install -o palworld-user -g palworld-user -m 755 update.sh /home/palworld-user/scripts/update.sh
```

### 関連スクリプト（任意・Web UIからは呼ばれません）

Web UIのバックアップ画面から作成・予約・復元できます。以下は、別途 `cron` で運用する場合の既存スクリプト例です。Web UIの定期バックアップと併用する場合は、実行時刻と保存先の重複を避けてください。

```bash
#!/bin/bash

# 設定
BACKUP_DIR="/home/palworld-user/backups"
SAVE_DIR="/home/palworld-user/PalServer/Pal/Saved"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_PATH="$BACKUP_DIR/palserver_backup_$TIMESTAMP.tar.gz"

# 30日以上前のバックアップを削除（ディスク容量の節約）
find $BACKUP_DIR -type f -name "*.tar.gz" -mtime +30 -delete

# バックアップ処理
if [ -d "$SAVE_DIR" ]; then
    tar -czf "$BACKUP_PATH" -C "$SAVE_DIR" .
    echo "[$(date)] バックアップが正常に完了しました: $BACKUP_PATH"
else
    echo "[$(date)] エラー: セーブデータフォルダが見つかりません。"
fi
```

## 起動確認

Manager を起動するたびに、`log/YYYYMMDD-HHMMSS-palworld-manager.log` を作成します。`log/` は初回起動時に自動作成されます。ファイルは最新 7 世代だけを残し、8 世代目を作るときに最古のログを削除します。systemd journal への出力も続くため、ファイルログを確認できない場合の予備として使えます。

画面の定期取得で発生する正常な GET アクセスと、HTTPX・HTTPCore・APScheduler の通常の INFO ログは省略します。警告とエラーは記録し、Discord Webhook URL はファイルと journal の両方で伏字にします。

```bash
# ファイルログの一覧を確認
ls -lt /home/palworld-user/palworld-manager/log/

# 最新のファイルログを追跡
tail -f "$(ls -1t /home/palworld-user/palworld-manager/log/*-palworld-manager.log | head -n 1)"

# journal を予備の確認手段として使う
sudo journalctl -u palworld-manager -f

# ブラウザでアクセス
http://<サーバーIP>:8080
```

画面幅が 700px 以下になると、左メニューは自動でドロワー表示へ切り替わります。左上のハンバーガーボタンで開き、メニュー項目か半透明の背景を押すと閉じます。PCでは同じボタンで左メニューを折りたためます。ノッチ付き端末の安全領域を考慮し、横向きのタッチ端末でもボタンのタップ領域を保ちます。更新後も古い表示が残る場合は、ブラウザを再読み込みしてください。

## 座標マップのマップ画像

ワールド状況タブの座標マップは、`frontend/assets/worldmap.png` または `frontend/assets/worldmap.webp` があれば地形画像の上に Actor を固定座標系で表示します（`.png` を優先）。画像は `/static/` パスで配信され、`frontend/assets/` が無くても Manager は起動します。

- **画像の要件** — Palworld 1.0 の全域ワールドマップ画像（正方形）で、画像の四辺がゲーム内マップ座標 ±1000 の範囲に一致していること。ゲーム内マップテクスチャ（T_WorldMap）由来の画像はこの条件を満たします
- **入手方法** — ゲームデータからの抽出画像（例: [PalworldSaveTools](https://github.com/deafdudecomputers/PalworldSaveTools) の `resources/assets/maps/T_WorldMap.webp`）か、ゲーム内マップの全体スクリーンショット（UI アイコン非表示・マップ全域が正方形に収まるよう撮影）を使います
- **著作権上の注意** — マップ画像はポケットペア社の著作物です。個人利用のサーバー管理目的に留め、`frontend/assets/` は `.gitignore` 済みのためリポジトリへコミットされません。公開リポジトリや再配布物に含めないでください
- **範囲外の点** — ダンジョン・世界樹内部など画像範囲外の座標の Actor は描画せず、「マップ範囲外 N 件」として件数表示します
- **位置がずれる場合** — 将来のアップデートでマップ範囲が変わった場合は、`frontend/index.html` の `WORLD_MAP_CONFIG`（座標範囲・反転フラグ）だけを調整します。DevTools コンソールで `window.worldMapDebug = true` にするとマップクリック位置のワールド座標とゲーム内マップ座標が出力され、ゲーム内マップと突き合わせて確認できます

座標変換は Palworld 1.0 の系（`map_x = (world_y + 18) / 725`、`map_y = (world_x + 375247) / 725`、画像はマップ座標 ±1000）を使用し、ファストトラベル 157 地点の投影検証で位置一致を確認済みです。

## 環境変数（/etc/palworld-manager.env）

| 変数 | 説明 | デフォルト |
| --- | --- | --- |
| PAL_ENV | staging / production | staging |
| PAL_HOST | Palworld サーバーのホスト | localhost |
| PAL_PORT | REST API ポート | 8212 |
| PAL_ADMIN_PASSWORD | AdminPassword の値 | (空) |
| APP_PORT | Web UI ポート | 8080 |
| PAL_SERVICE_NAME | Palworld の systemd サービス名（実機に合わせる） | palserver |
| PAL_SETTINGS_INI | PalWorldSettings.ini のフルパス | /home/palworld-user/PalServer/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini |
| SCHEDULE_TIMEZONE | スケジュールの解釈・表示に使う TZ | Asia/Tokyo |
| DISCORD_WEBHOOK_URL | Discord Webhook URL（メモリ監視通知用。未設定なら無効） | (空) |
| DISCORD_LIFECYCLE_WEBHOOK_URL | Discord Webhook URL（Manager 起動/停止通知用。未設定なら無効） | (空) |

> [!warning] Manager は単一ワーカーで運用する
> メモリ監視、履歴サンプラー、再起動スケジューラ、安全メンテナンスの状態と排他制御は単一プロセスを前提にしています。uvicorn を複数ワーカーで動かすと、通知・収集・操作が重複するため、同梱の systemd サービスと同じく単一ワーカーで運用してください。

> [!warning] 更新中は通常のサービス操作を重ねない
> Palworld 更新ではManagerのrootプロセスから `palworld-user` へ権限を落とし、そのユーザーとして `sudo /home/palworld-user/scripts/update.sh` 相当の固定コマンドを実行します。実行ディレクトリは `/home/palworld-user` です。パスワードはsudoへ1行だけ送り、送信後にメモリ上で上書きして標準入力も閉じます。sudoがパスワードを読まない構成でも秘密値が更新スクリプトへ渡らないよう、スクリプトの標準入力は `/dev/null` に固定します。更新終了時には同じ実行コンテキストでsudo認証キャッシュも破棄します。更新スクリプトが30分以内に終わらない場合は失敗として終了させます。同梱のsystemd unitは `KillMode=mixed` と40分の停止猶予を設定し、Manager停止時も先に更新と復旧確認の完了を待ちます。更新中はManagerを停止せず、Web UIの通常の起動・停止・再起動も重ねないでください。Managerを再起動すると実行中の更新状態を引き継げません。Web UIの「シャットダウン」と「強制停止」は緊急操作として排他の対象外なので、更新中には使用しないでください。

## systemctl 実行権限

`palworld-manager.service` は `User=root` で実行します。通常のサービス操作には `sudo -n systemctl` を使います。Palworld更新時だけ `runuser` で `palworld-user` へ権限を落とし、そのユーザーのsudo認証で固定パスのスクリプトを実行します。OSに `runuser` と `sudo` コマンドが必要です。

```bash
command -v sudo
command -v runuser
id palworld-user
systemctl show palworld-manager -p User
```

sudoが通常求めるのはrootパスワードではなく、呼び出し元である `palworld-user` ユーザーのパスワードです。sudoersに `rootpw` または `targetpw` がある環境だけは、その設定に従います。`update.sh` は対話入力やバックグラウンド化を行わず、実行ディレクトリ `/home/palworld-user` から固定パスまたはスクリプト自身を基準にファイルを参照してください。秘密値をスクリプトの標準入力へ渡さないため、sudoが実際に起動するのは固定引数の `sh -c` ラッパーです。`palworld-user` のsudoersをコマンド単位で制限している場合、`update.sh` の直接実行だけを許可するルールとは互換性がないため、実際の固定ラッパーを含めて権限設計を確認してください。この実装はパスワード認証を前提にしているため、`NOPASSWD`、`!authenticate`、`exempt_group` などの認証免除を設定しないでください。将来パスワードレス運用へ切り替える場合は、画面とAPIからパスワード項目をなくす専用モードとして実装してください。

> [!warning] サービス名は完全一致で統一する
> アプリは `PAL_SERVICE_NAME`（既定 `palserver`）をそのまま `systemctl` へ渡します。環境変数と実際のサービス名を接尾辞なしの `palserver` に揃えてください。片方だけに `.service` を付けると、start、stop、restart が失敗する場合があります。

## Discord 通知の設定

Webhook を用途別に 2 つ設定できます。同じ URL にしても構いません。

### メモリ監視通知（`DISCORD_WEBHOOK_URL`）

1. Discord のサーバー設定 → 連携サービス → Webhook で Webhook URL を作成
2. `/etc/palworld-manager.env` に `DISCORD_WEBHOOK_URL=...` を追記
3. `sudo systemctl restart palworld-manager`
4. Web UI の「サーバー操作」→「Discord 通知テスト」で送信確認

メモリ使用率が 80% を超えると 🟡 Warning、90% を超えると 🔴 Critical が通知されます。しきい値を下回ると ✅ 回復通知が届きます。

### Palworld サーバー起動/停止通知（`DISCORD_LIFECYCLE_WEBHOOK_URL`）

1. Webhook URL を作成（メモリ監視と同じ URL でも可）
2. `/etc/palworld-manager.env` に `DISCORD_LIFECYCLE_WEBHOOK_URL=...` を追記
3. `sudo systemctl restart palworld-manager`

10 秒ごとに `systemctl is-active <PAL_SERVICE_NAME>` をポーリングし、状態が変化したときだけ通知します。palserver の起動時に 🟢、停止時に 🔴 の通知が届きます。

## ファイル構成

```
palworld-manager/
├── backend/
│   ├── main.py               # FastAPI エントリーポイント
│   ├── config.py             # 環境変数・設定クラス
│   ├── logging_config.py     # ファイルログと journal の設定・世代管理
│   ├── palworld_client.py    # Palworld REST API クライアント（全 12 エンドポイント）
│   ├── system_metrics.py     # psutil によるシステム・プロセス監視
│   ├── ini_editor.py         # PalWorldSettings.ini 読み書き（バックアップ付き）
│   ├── scheduler.py          # APScheduler による再起動スケジューラ（cron / 単発）
│   ├── shutdown_scheduler.py # サーバー終了日時の予約（単発 stop、再起動しない）
│   ├── ranking_tracker.py    # プレイヤーランキング収集（10 秒サンプリング・積算・JSON 永続化）
│   ├── world_snapshot.py     # /game-data の安全な集計、簡易マップ用データ、15 秒キャッシュ
│   ├── history_store.py      # メトリクス・プレイヤーセッション履歴（SQLite、30 秒サンプリング）
│   ├── config_diff.py        # INI 保存値と REST API 稼働値の正規化・差分判定
│   ├── maintenance.py        # 接続者確認・予告・保存・復旧確認付きメンテナンスジョブ
│   ├── sensitive_requests.py # sudoパスワードを含む入力エラーの秘匿・キャッシュ防止
│   ├── discord_notify.py     # Discord Webhook 通知（起動/停止通知・メモリ監視）
│   ├── websocket_log.py      # journalctl WebSocket ストリーム
│   ├── schedules.json        # スケジュール永続化（自動生成、状態機付き）
│   ├── shutdown_schedule.json # サーバー終了日時の予約永続化（自動生成、1件のみ）
│   ├── ranking.json          # ランキング永続化（自動生成、.bak バックアップ付き）
│   ├── manager_history.db    # 稼働履歴（自動生成、WAL モード）
│   ├── requirements.txt      # Python 依存
│   ├── routers/
│   │   ├── server.py         # /api/server/* （REST API プロキシ）
│   │   ├── system.py         # /api/system/* （サービス制御・リソース監視・Discord テスト）
│   │   ├── ini.py            # /api/ini/* （INI 読み書き、秘密キーはマスク）
│   │   ├── schedule.py       # /api/schedule/* （スケジュール管理）
│   │   ├── shutdown_schedule.py # /api/shutdown-schedule/* （サーバー終了日時の設定・解除）
│   │   ├── ranking.py        # /api/ranking/* （ランキング参照・CSV出力）
│   │   ├── world.py          # GET /api/world/snapshot
│   │   ├── history.py        # /api/history/* （メトリクス・セッション履歴）
│   │   ├── maintenance.py    # /api/maintenance/* （設定差分・安全な再起動/更新）
│   │   └── config.py         # GET /api/config/env
│   └── tests/
│       ├── test_frontend_responsive.py  # スマホ表示とメニュー構造のテスト
│       ├── test_system_metrics.py   # ホスト OS 情報とシステムメトリクスのテスト
│       ├── test_logging_config.py  # ログ生成・世代管理・フォールバックのテスト
│       ├── test_regressions.py     # 回帰テスト（INI・スケジューラ。pytest / 直接実行の両対応）
│       ├── test_shutdown_schedule.py # サーバー終了日時（設定・解除・自動停止・復元）のテスト
│       ├── test_ranking.py         # ランキング収集のテスト（積算・破損復旧・atomic write）
│       ├── test_ranking_csv.py     # ランキングCSVの順序・形式・蓄積データ不変テスト
│       ├── test_world_snapshot.py  # ワールド集計・秘匿・キャッシュのテスト
│       ├── test_history_store.py   # SQLite 履歴・セッション・保持期間のテスト
│       ├── test_config_diff.py     # 設定値の正規化・秘密値除外のテスト
│       ├── test_maintenance.py     # メンテナンス状態遷移・拒否・キャンセルのテスト
│       └── test_maintenance_api.py # sudoパスワードAPIの秘匿・キャッシュ防止テスト
├── frontend/
│   ├── index.html            # シングル HTML（Vanilla JS、DOM は createElement で構築、CSS 変数でライト/ダーク切替）
│   └── assets/               # 座標マップ用ワールドマップ画像（ユーザー配置、/static/ で配信、Git 管理外）
├── log/                      # 起動ごとの Manager ログ（自動生成、最新 7 世代）
├── palworld-manager.service  # systemd ユニットファイル
├── palworld-manager.env.example
├── .gitignore
├── LICENSE                   # MIT License
└── README.md
```

## テスト

```bash
cd backend
# pytest を追加インストールしなくても実行可能
.venv/bin/python tests/test_frontend_responsive.py
.venv/bin/python tests/test_system_metrics.py
.venv/bin/python tests/test_logging_config.py
.venv/bin/python tests/test_regressions.py
.venv/bin/python tests/test_shutdown_schedule.py
.venv/bin/python tests/test_ranking.py
.venv/bin/python tests/test_ranking_csv.py
.venv/bin/python tests/test_world_snapshot.py
.venv/bin/python tests/test_history_store.py
.venv/bin/python tests/test_config_diff.py
.venv/bin/python tests/test_maintenance.py
.venv/bin/python tests/test_maintenance_api.py

# pytest を別途インストールしている場合
.venv/bin/python -m pytest tests/ -v
```

- `test_frontend_responsive.py` — viewport、安全領域、タップ領域、メニューの ARIA 属性、ナビ項目と画面の対応、スマホ用ドロワーのCSSと開閉イベント、ダッシュボードの KPI・グラフ・クイックアクションの DOM、イベントログのフロント側構造化（時刻推定・リングバッファ上限）、`innerHTML` 不使用を検証
- `test_system_metrics.py` — OS 情報の取得・サニタイズ・フォールバックと、メトリクス応答への安全な追加を検証
- `test_logging_config.py` — 起動時のログファイル作成、最新 7 世代の保持、Webhook URL の伏字、初期化失敗時の journal フォールバックを検証
- `test_regressions.py` — INI の保存（二重引用・CRLF/BOM 維持・空値キー保持）、再起動スケジューラ（保存失敗時の中止・キャンセル・デバウンス・cron 曜日拒否）、Palworld 停止中の接続失敗を 502/504 で返す（500 とトレースバックにしない）ことの回帰を固定
- `test_shutdown_schedule.py` — サーバー終了日時の設定・上書き・解除、過去日時の拒否、自動停止ジョブの成功/失敗時の状態遷移、Manager 再起動時の復元（未来日時は再登録、過去日時は自動実行せず failed、running は failed、completed/failed は保持）、ダッシュボード用の状態判定（none/countdown/reached）を検証
- `test_ranking.py` — ランキング収集の積算ロジック（差分積算・異常ギャップ・重複 userId）、API 失敗・不正応答時のベースラインリセット、破損ファイルからの復旧（不正 UTF-8・不正スキーマ・非有限数）、atomic write とバックアップの耐久性、タイムゾーン日付境界を検証
- `test_ranking_csv.py` — 指標ごとの順位、BOM・改行・ヘッダー、数式注入対策、出力前後でランキングのメモリ・永続ファイルが変わらないことを検証
- `test_world_snapshot.py` — Actor とギルドの集計、警告候補・ギルド・文字列の返却上限、識別情報の除外、型崩れ、座標・件数制限、15 秒キャッシュ、404・接続失敗の扱いを検証
- `test_history_store.py` — メトリクスの保存・間引き・30 日保持、セッション開始/継続/終了・観測期限・30 日保持、集計、壊れた応答と部分的な取得失敗の扱いを検証
- `test_config_diff.py` — 真偽値・数値・配列・引用符の正規化、保存値と稼働値の分類、秘密キーの除外、上流エラーの扱いを検証
- `test_maintenance.py` — 接続者がいる場合の安全側拒否、予告・保存・再起動/更新・復旧確認の順序、sudo認証と固定スクリプト、有限タイムアウト、二重実行防止、カウントダウンのキャンセルを検証
- `test_maintenance_api.py` — sudoパスワードを応答やジョブ状態へ返さないこと、入力エラーを含む応答のキャッシュ防止を検証

## License

MIT License。詳細は [LICENSE](LICENSE) を参照してください。

> [!note] マップ画像は対象外
> `frontend/assets/` に配置するワールドマップ画像はポケットペア社の著作物であり、本ライセンスの対象外です。詳細は「[座標マップのマップ画像](#座標マップのマップ画像)」を参照してください。
