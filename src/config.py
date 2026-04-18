import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    meet_url: str
    bot_display_name: str
    google_email: str
    google_password: str
    chrome_user_data_dir: str
    music_dir: str
    output_device_substring: str
    crossfade_seconds: float
    default_volume: float
    chat_command_prefix: str
    allow_chat_control: bool
    shuffle_on_start: bool
    sample_rate: int
    youtube_enabled: bool
    youtube_cache_dir: str
    youtube_cookies_from_browser: str

    @classmethod
    def load(cls, path: str = "config.json") -> "Config":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"{path} not found. Copy config.example.json to config.json and edit it."
            )
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(**data)
