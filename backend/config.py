import os
from dataclasses import dataclass


@dataclass
class Settings:
    env: str
    pal_host: str
    pal_port: int
    pal_admin_password: str
    app_port: int
    pal_service_name: str
    pal_settings_ini: str
    discord_webhook_url: str
    discord_lifecycle_webhook_url: str
    schedule_timezone: str

    @property
    def pal_base_url(self) -> str:
        return f"http://{self.pal_host}:{self.pal_port}/v1/api"

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def env_label(self) -> str:
        return "[本番環境]" if self.is_production else "[検証環境]"

    @property
    def env_color(self) -> str:
        # 本番=赤、検証=オレンジ
        return "#c0392b" if self.is_production else "#e67e22"


def get_settings() -> Settings:
    return Settings(
        env=os.getenv("PAL_ENV", "staging"),
        pal_host=os.getenv("PAL_HOST", "localhost"),
        pal_port=int(os.getenv("PAL_PORT", "8212")),
        pal_admin_password=os.getenv("PAL_ADMIN_PASSWORD", ""),
        app_port=int(os.getenv("APP_PORT", "8080")),
        # 接尾辞なしで統一（systemctl は補完する。sudoers 例・env・README も接尾辞なし）
        pal_service_name=os.getenv("PAL_SERVICE_NAME", "palserver"),
        pal_settings_ini=os.getenv(
            "PAL_SETTINGS_INI",
            "/home/palworld-user/PalServer/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini",
        ),
        # Discord 通知用 Webhook URL（メモリ監視）
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
        # Discord 通知用 Webhook URL（起動/停止通知）
        discord_lifecycle_webhook_url=os.getenv("DISCORD_LIFECYCLE_WEBHOOK_URL", ""),
        # スケジュールの解釈・表示に使うタイムゾーン
        schedule_timezone=os.getenv("SCHEDULE_TIMEZONE", "Asia/Tokyo"),
    )


settings = get_settings()
