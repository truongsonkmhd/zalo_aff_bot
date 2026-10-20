import json
import secrets
import time

from app.config import Settings
from app.security import hash_password, zalo_expected_mac

APP_ID = "1234567890"
OA_ID = "999000111"
OA_SECRET = "oa-secret-for-tests"
PATH_SECRET = "p" * 32
ADMIN_PASSWORD = "mat-khau-quan-tri-dai"
_PW_HASH = hash_password(ADMIN_PASSWORD)


def make_settings(tmpdir, **overrides) -> Settings:
    base = dict(
        zalo_app_id=APP_ID, zalo_oa_id=OA_ID, zalo_oa_secret=OA_SECRET, zalo_app_secret="app-secret",
        zalo_initial_refresh_token="refresh-0", webhook_path_secret=PATH_SECRET,
        shopee_app_id="shp-app", shopee_app_secret="shp-secret", shopee_web_fallback=False,
        user_hash_key="u" * 40, data_key=secrets.token_hex(32), admin_password_hash=_PW_HASH,
        data_dir=str(tmpdir), admin_allowed_cidrs=("127.0.0.1/32",), cookie_secure=False,
        sub_id_channel="zalo", sub_id_campaign="c1",
    )
    base.update(overrides)
    return Settings(**base)


def zalo_event(text="", user="u-1", msg_id=None, event_name="user_send_text", ts=None, attachments=None):
    msg = {"text": text, "msg_id": msg_id or secrets.token_hex(8)}
    if attachments:
        msg["attachments"] = attachments
    return {
        "app_id": APP_ID, "event_name": event_name,
        "timestamp": str(int((ts or time.time()) * 1000)),
        "sender": {"id": user}, "recipient": {"id": OA_ID}, "message": msg,
    }


def signed(event, secret=OA_SECRET):
    body = json.dumps(event)
    mac = zalo_expected_mac(APP_ID, body, event["timestamp"], secret)
    return body, {"X-ZEvent-Signature": f"mac={mac}", "Content-Type": "application/json"}


async def public_resolver(host):
    return ["203.0.113.10"] if host == "never" else ["104.18.0.1"]
