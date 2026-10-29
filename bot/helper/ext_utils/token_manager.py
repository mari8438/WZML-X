# ruff: noqa: E402
"""
Anime Starfall • User Magic Token System (`bot/helper/ext_utils/token_manager.py`)
Provides cryptographically secure, URL-safe HMAC-SHA256 tokens shared across
the Pyrogram Bot process and the FastAPI (`web/wserver.py`) process.
"""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
from hmac import compare_digest, new as hmac_new
from importlib import import_module
from json import dumps, loads
from os import environ
from time import time
from typing import Any, Dict, Optional

_TOKEN_VERSION = 1
_TOKEN_NAMESPACE = "starfall-web-studio-magic-token-v1"


def _resolve_signing_secret() -> bytes:
    """
    Derive a deterministic 256-bit HMAC key from BOT_TOKEN (consistent with
    `file_stream_utils.py`), ensuring the Pyrogram bot process and the gunicorn
    `web.wserver:app` worker always compute the exact same signing key even when
    `WEB_ACCESS_PASSWORD` is ephemerally generated at runtime.
    """
    bot_token = environ.get("BOT_TOKEN", "").strip()
    if not bot_token:
        try:
            from bot.core.config_manager import Config

            bot_token = str(getattr(Config, "BOT_TOKEN", "") or "").strip()
        except Exception:
            pass
    if not bot_token:
        try:
            cfg = import_module("config")
            bot_token = str(getattr(cfg, "BOT_TOKEN", "") or "").strip()
        except Exception:
            pass

    _secret_file = "/tmp/starfall_token_secret.bin"
    if bot_token:
        seed = f"{bot_token}|{_TOKEN_NAMESPACE}"
        derived = sha256(seed.encode("utf-8")).digest()
        try:
            with open(_secret_file, "wb") as fp:
                fp.write(derived)
        except Exception:
            pass
        return derived

    if os.path.exists(_secret_file):
        try:
            with open(_secret_file, "rb") as fp:
                data = fp.read()
                if len(data) == 32:
                    return data
        except Exception:
            pass

    seed = f"{bot_token}|{_TOKEN_NAMESPACE}"
    return sha256(seed.encode("utf-8")).digest()


def _b64url_encode(data: bytes) -> str:
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return urlsafe_b64decode((text + padding).encode("ascii"))


def generate_user_token(user_id: int, expiry_days: int = 7) -> str:
    """
    Generate a cryptographically signed, URL-safe magic token for `user_id`.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("user_id must be a valid integer") from exc
    if uid <= 0:
        raise ValueError("user_id must be a positive integer")

    days = max(1, int(expiry_days or 7))
    now_ts = int(time())
    exp_ts = now_ts + (days * 86400)

    payload_dict = {
        "u": uid,
        "iat": now_ts,
        "exp": exp_ts,
        "v": _TOKEN_VERSION,
    }
    payload_bytes = dumps(payload_dict, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    payload_b64 = _b64url_encode(payload_bytes)
    signature = hmac_new(
        _resolve_signing_secret(), payload_b64.encode("ascii"), sha256
    ).hexdigest()
    return f"{payload_b64}.{signature}"


def verify_user_token(token: str) -> Optional[int]:
    """
    Validate a magic token's HMAC-SHA256 signature and expiration timestamp.
    Returns the authenticated `user_id` (int) if valid; returns `None` if
    tampered with, malformed, or expired.
    """
    info = inspect_user_token(token)
    if not info:
        return None
    return info["user_id"]


def inspect_user_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Verify token and return decoded metadata (`user_id`, `issued_at`, `expires_at`)
    or `None` if invalid or expired.
    """
    if not token or not isinstance(token, str) or "." not in token:
        return None

    token = token.strip()
    try:
        payload_b64, provided_sig = token.rsplit(".", 1)
        if not payload_b64 or not provided_sig:
            return None

        expected_sig = hmac_new(
            _resolve_signing_secret(), payload_b64.encode("ascii"), sha256
        ).hexdigest()
        if not compare_digest(provided_sig.lower(), expected_sig.lower()):
            return None

        payload = loads(_b64url_decode(payload_b64).decode("utf-8"))
        if not isinstance(payload, dict):
            return None

        uid = int(payload["u"])
        exp_ts = int(payload["exp"])
        iat_ts = int(payload.get("iat", 0))
        if uid <= 0 or exp_ts < int(time()):
            return None

        return {
            "user_id": uid,
            "issued_at": iat_ts,
            "expires_at": exp_ts,
            "version": int(payload.get("v", 1)),
        }
    except Exception:
        return None


def resolve_base_url(fallback: str = "http://localhost:8080") -> str:
    """Resolve the public BASE_URL from Config, environment, or config.py."""
    base = environ.get("BASE_URL", "").strip()
    if not base:
        try:
            import sys
            cfg_mod = sys.modules.get("bot.core.config_manager")
            if cfg_mod and hasattr(cfg_mod, "Config"):
                base = str(getattr(cfg_mod.Config, "BASE_URL", "") or "").strip()
        except Exception:
            pass
    if not base:
        try:
            cfg = import_module("config")
            base = str(getattr(cfg, "BASE_URL", "") or "").strip()
        except Exception:
            pass
    return (base or fallback).rstrip("/")


def build_studio_url(
    user_id: int,
    route: str = "/app/user",
    expiry_days: int = 7,
    base_url: Optional[str] = None,
) -> str:
    """Construct a complete one-click Starfall Studio URL for `user_id`."""
    token = generate_user_token(user_id=user_id, expiry_days=expiry_days)
    root = (base_url or resolve_base_url()).rstrip("/")
    clean_route = "/" + route.lstrip("/")
    return f"{root}{clean_route}?token={token}"
