from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")

PIXIV_OAUTH_URL = "https://oauth.secure.pixiv.net/auth/token"
PIXIV_API_BASE = "https://app-api.pixiv.net/v1"
PIXIV_CLIENT_ID = os.getenv("PIXIV_CLIENT_ID") or "MOBrBDS8blbauoSck0ZfDbtuzpyT"
PIXIV_CLIENT_SECRET = os.getenv("PIXIV_CLIENT_SECRET") or "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
PIXIV_USER_AGENT = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"

TELEGRAM_API_BASE = "https://api.telegram.org"

MAX_ALBUM_SIZE = 10
MAX_IMAGE_EDGE = 1920
JPEG_QUALITY = 80
MAX_PIXIV_PAGES = 4
TELEGRAM_MAX_BYTES = 9_500_000
REQUEST_TIMEOUT = 20.0


def env_config(batch: dict) -> dict:
    """Config for one run: secrets from env, everything else (chat, artists, dry) from the batch."""
    from pixiv_lab.caption import set_cta, set_footer, set_layout, set_strip, set_templates
    from pixiv_lab.pipeline import set_dry_bot_link
    from pixiv_lab.tags import set_brand_tag
    from pixiv_lab.userbot_zip import set_storage, set_storage_chat

    refresh = (os.getenv("PIXIV_REFRESH_TOKEN") or "").strip()
    bot = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = str(batch.get("target_chat_id") or "").strip()
    dry_run = bool(batch.get("dry"))

    # all caption arrangement comes from the batch (material), never hardcoded
    footer = str(batch.get("footer_html") or batch.get("footer") or "").strip()
    brand_tag = str(batch.get("brand_tag") or "").strip()
    set_footer(footer)
    set_brand_tag(brand_tag)
    law_cap = (batch.get("channel_law") or {}).get("caption") or {}
    cap = batch.get("caption") if isinstance(batch.get("caption"), dict) else {}
    set_layout(cap.get("layout") or law_cap.get("layout"))
    set_strip(cap.get("strip") or law_cap.get("strip"))
    set_templates(cap.get("templates") or law_cap.get("templates"))
    set_cta(batch.get("cta") or cap.get("cta") or law_cap.get("cta"))
    set_dry_bot_link(batch.get("dry_bot_link") or "")
    set_storage(
        str(batch.get("storage_bot_username") or os.getenv("STORAGE_BOT_USERNAME", "") or "").strip(),
        str(batch.get("storage_bot_link_label") or os.getenv("STORAGE_BOT_LINK_LABEL", "") or "").strip(),
    )
    set_storage_chat(str(batch.get("storage_chat_id") or "").strip())

    missing = [n for n, v in [("PIXIV_REFRESH_TOKEN", refresh), ("target_chat_id (batch)", chat)] if not v]
    if missing:
        raise SystemExit("Missing required: " + ", ".join(missing))

    return {
        "pixiv_refresh_token": refresh,
        "telegram_bot_token": bot,
        "telegram_chat_id": chat,
        "dry_run": dry_run,
        "channel_law": batch.get("channel_law") or {},   # لایحه‌ی کانال از بات
        "artists": list(batch.get("artists") or []),
        "dictionary_path": DATA_DIR / "character_dictionary.csv",
        "content_tags_path": DATA_DIR / "content_tags.csv",
        "adult_tags_path": DATA_DIR / "adult_tags.csv",
        "drop_tags_path": DATA_DIR / "drop_tags.csv",
        "runtime_path": DATA_DIR / "runtime.json",
        "posted_path": DATA_DIR / "posted.json",
    }