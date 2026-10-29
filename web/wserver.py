# ruff: noqa: E402
try:
    from uvloop import install

    install()
except ImportError:
    pass


from asyncio import new_event_loop, set_event_loop

bot_loop = new_event_loop()
set_event_loop(bot_loop)

from asyncio import sleep
from html import escape
from importlib import import_module
from os import environ
from re import compile as re_compile
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from logging import INFO, WARNING, FileHandler, StreamHandler, basicConfig, getLogger

from aioaria2 import Aria2HttpClient
from aiohttp.client_exceptions import ClientError
from aioqbt.client import create_client
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pyrogram import Client
from time import time
from file_stream_utils import (
    STREAM_CHUNK_SIZE,
    content_disposition,
    decode_file_token,
    parse_byte_range,
)
from fastapi.templating import Jinja2Templates
from sabnzbdapi import SabnzbdClient
from aioqbt.exc import AQError

from web.nodes import extract_file_ids, make_tree
from aiohttp import ClientSession

getLogger("httpx").setLevel(WARNING)
getLogger("aiohttp").setLevel(WARNING)
getLogger("uvicorn").setLevel(WARNING)
getLogger("uvicorn.access").setLevel(WARNING)

basicConfig(
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
    datefmt="%d-%b-%y %I:%M:%S %p",
    handlers=[FileHandler("log.txt"), StreamHandler()],
    level=INFO,
)

LOGGER = getLogger(__name__)

_SAFE_PATH = re_compile(r"^[A-Za-z0-9_./-]+$")
_SAFE_GID = re_compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_PIN = re_compile(r"^\d{4}$")
_SERVICE_PWD_SALT = b"wzmlx_v3_service_pwd_salt"
_PIN_SALT = b"wzmlx_v3_pin_salt"
_PIN_LEN = 4
_PIN_RATE_LIMIT = 5
_PIN_RATE_WINDOW = 60
_pin_attempts: dict = {}

_cached_secret_bytes = None


def _load_config():
    try:
        cfg = import_module("config")
    except ModuleNotFoundError:
        cfg = None
    bot_token = environ.get("BOT_TOKEN", "") or (
        getattr(cfg, "BOT_TOKEN", "") if cfg else ""
    )
    access_pwd = environ.get("WEB_ACCESS_PASSWORD", "") or (
        getattr(cfg, "WEB_ACCESS_PASSWORD", "") if cfg else ""
    )
    api_id = int(environ.get("TELEGRAM_API", 0) or (getattr(cfg, "TELEGRAM_API", 0) if cfg else 0))
    api_hash = environ.get("TELEGRAM_HASH", "") or (getattr(cfg, "TELEGRAM_HASH", "") if cfg else "")
    return bot_token, access_pwd, api_id, api_hash


def _resolve_bot_id(token):
    if not token or not isinstance(token, str):
        return "0"
    token = token.strip()
    if not token:
        return "0"
    return (token.split(":", 1)[0] or "0").strip()


_BOT_TOKEN, _ACCESS_PASSWORD, _API_ID, _API_HASH = _load_config()
_BOT_ID = _resolve_bot_id(_BOT_TOKEN)
_stream_client = None


def _service_pwd(service):
    from hashlib import sha256
    from hmac import new as hmac_new
    from secrets import token_bytes

    global _cached_secret_bytes
    if not _ACCESS_PASSWORD:
        if _cached_secret_bytes is None:
            _cached_secret_bytes = token_bytes(32)
        secret = _cached_secret_bytes
    elif isinstance(_ACCESS_PASSWORD, str):
        secret = _ACCESS_PASSWORD.encode("utf-8")
    else:
        secret = _ACCESS_PASSWORD
    msg = f"{_BOT_ID}:{service}".encode("utf-8")
    digest = hmac_new(_SERVICE_PWD_SALT, msg, sha256)
    digest.update(secret)
    raw = digest.hexdigest()
    return raw[:20] + raw[-4:]


def _derive_pin(gid):
    from hashlib import sha256
    from hmac import new as hmac_new

    sig = hmac_new(
        _PIN_SALT,
        f"{gid}|{_BOT_ID}".encode("utf-8"),
        sha256,
    ).hexdigest()
    digits = "".join(c for c in sig if c.isdigit())[:_PIN_LEN]
    if len(digits) < _PIN_LEN:
        digits = (digits + sig).ljust(_PIN_LEN, "0")[:_PIN_LEN]
    return digits


def _pin_rate_limited(gid):
    from time import time

    now = time()
    cutoff = now - _PIN_RATE_WINDOW
    attempts = _pin_attempts.get(gid, [])
    attempts = [t for t in attempts if t > cutoff]
    if attempts:
        _pin_attempts[gid] = attempts
    else:
        _pin_attempts.pop(gid, None)
    if len(_pin_attempts) > 10000:
        stale = [
            g for g, ts in _pin_attempts.items() if not ts or (ts and ts[-1] < cutoff)
        ]
        for g in stale:
            _pin_attempts.pop(g, None)
    return len(attempts) >= _PIN_RATE_LIMIT


def _record_pin_attempt(gid):
    from time import time

    _pin_attempts.setdefault(gid, []).append(time())


def _verify_pin(gid, pin):
    from hashlib import sha256
    from hmac import new as hmac_new

    if not gid or not pin:
        return False
    if not _SAFE_PIN.match(pin):
        return False
    expected = _derive_pin(gid)
    if not expected:
        return False
    return (
        hmac_new(_PIN_SALT, expected.encode(), sha256).hexdigest()
        == hmac_new(_PIN_SALT, pin.encode(), sha256).hexdigest()
    )


aria2 = None
qbittorrent = None
sabnzbd_client = SabnzbdClient(
    host="http://localhost",
    api_key=_service_pwd("sabnzbd"),
    port="8070",
)
SERVICES = {
    "nzb": {"url": "http://localhost:8070/", "password": _service_pwd("sabnzbd")},
    "qbit": {"url": "http://localhost:8090", "password": _service_pwd("qbit")},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global aria2, qbittorrent, _stream_client
    aria2 = Aria2HttpClient("http://localhost:6800/jsonrpc")
    qbittorrent = await create_client("http://localhost:8090/api/v2/")
    try:
        yield
    finally:
        if _stream_client is not None:
            await _stream_client.stop()
            _stream_client = None
        await aria2.close()
        await qbittorrent.close()


app = FastAPI(lifespan=lifespan)


templates = Jinja2Templates(directory="web/templates/")


async def _get_stream_client():
    global _stream_client
    if _stream_client is None:
        if not (_BOT_TOKEN and _API_ID and _API_HASH):
            raise HTTPException(status_code=503, detail="Telegram streaming is not configured")
        _stream_client = Client(
            "starfall_file_stream",
            api_id=_API_ID,
            api_hash=_API_HASH,
            bot_token=_BOT_TOKEN,
            in_memory=True,
            max_concurrent_transmissions=8,
            no_updates=True,
        )
        await _stream_client.start()
    return _stream_client


@app.api_route("/dl/{token}/{filename:path}", methods=["GET", "HEAD"])
async def telegram_file_stream(token: str, filename: str, request: Request):
    payload = decode_file_token(token, time(), _BOT_TOKEN, _ACCESS_PASSWORD)
    if not payload:
        raise HTTPException(status_code=403, detail="This download link is invalid or expired")
    chat_id, message_id, _ = payload
    client = await _get_stream_client()
    message = await client.get_messages(chat_id, message_id)
    media = next((getattr(message, key, None) for key in ("document", "video", "audio", "animation", "voice") if getattr(message, key, None)), None)
    if not media:
        raise HTTPException(status_code=404, detail="Telegram file not found")

    safe_name = getattr(media, "file_name", None) or filename or "telegram-file"
    file_size = int(getattr(media, "file_size", 0) or 0)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition(safe_name),
        "Cache-Control": "private, no-store",
    }
    byte_range = None
    range_header = request.headers.get("range")
    if range_header:
        try:
            byte_range = parse_byte_range(range_header, file_size)
        except ValueError:
            headers["Content-Range"] = f"bytes */{file_size}"
            return Response(status_code=416, headers=headers)

    status_code = 200
    if byte_range:
        start, end = byte_range
        content_length = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        status_code = 206
    else:
        start, end = 0, file_size - 1
        content_length = file_size
    if content_length > 0:
        headers["Content-Length"] = str(content_length)

    media_type = getattr(media, "mime_type", None) or "application/octet-stream"
    if request.method == "HEAD":
        return Response(status_code=status_code, media_type=media_type, headers=headers)

    async def body():
        if not byte_range:
            async for chunk in client.stream_media(message):
                yield chunk
            return

        offset = start // STREAM_CHUNK_SIZE
        skip = start % STREAM_CHUNK_SIZE
        remaining = content_length
        limit = (skip + remaining + STREAM_CHUNK_SIZE - 1) // STREAM_CHUNK_SIZE
        async for chunk in client.stream_media(message, offset=offset, limit=limit):
            if skip:
                chunk = chunk[skip:]
                skip = 0
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            if chunk:
                yield chunk
                remaining -= len(chunk)
            if remaining <= 0:
                break

    return StreamingResponse(
        body(),
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


async def re_verify(paused, resumed, hash_id):
    k = 0
    while True:
        res = await qbittorrent.torrents.files(hash_id)
        verify = True
        for i in res:
            if i.index in paused and i.priority != 0:
                verify = False
                break
            if i.index in resumed and i.priority == 0:
                verify = False
                break
        if verify:
            break
        LOGGER.info("Reverification Failed! Correcting stuff...")
        await sleep(0.5)
        if paused:
            try:
                await qbittorrent.torrents.file_prio(
                    hash=hash_id, id=paused, priority=0
                )
            except (ClientError, TimeoutError, Exception, AQError) as e:
                LOGGER.error(f"{e} Errored in reverification paused!")
        if resumed:
            try:
                await qbittorrent.torrents.file_prio(
                    hash=hash_id, id=resumed, priority=1
                )
            except (ClientError, TimeoutError, Exception, AQError) as e:
                LOGGER.error(f"{e} Errored in reverification resumed!")
        k += 1
        if k > 5:
            return False
    LOGGER.info(f"Verified! Hash: {hash_id}")
    return True


@app.get("/app/files", response_class=HTMLResponse)
async def files(request: Request):
    response = templates.TemplateResponse(request, "page.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/mxplayer")
async def mxplayer_api(url: str):
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    try:
        from bot.helper.ext_utils.site_resolvers import resolve_mx

        data = await resolve_mx(url, {"MX_PLAYER_API_BASE": "internal"})
        return JSONResponse(
            {
                "status": True,
                "source_url": data.get("source_url", url),
                "download_url": data.get("download_url", url),
                "m3u8_url": data.get("download_url", url),
                "full_title": data.get("title") or "MX Player Video",
                "description": data.get("description") or "",
                "thumbnail": data.get("thumbnail") or "",
                "videos": data.get("videos") or [],
                "audios": data.get("audios") or [],
            }
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/hanime-api")
async def hanime_api(url: str):
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    try:
        from bot.helper.ext_utils.site_resolvers import resolve_hanime

        data = await resolve_hanime(url, {"HANIME_API_BASE": "internal"})
        streams = data.get("streams") or []
        return JSONResponse(
            {
                "slug": data.get("slug") or url.rstrip("/").split("/")[-1],
                "title": data.get("title") or "Hanime Video",
                "episode": data.get("episode") or "",
                "views": data.get("views") or "",
                "downloads": data.get("downloads") or "",
                "rank": data.get("rank") or "",
                "upload_date": data.get("upload_date") or "",
                "studio": data.get("studio") or "",
                "genres": data.get("genres") or "",
                "synopsis": data.get("synopsis") or "",
                "poster_url": data.get("poster_url") or "",
                "cover_url": data.get("cover_url") or "",
                "thumbnail": data.get("thumbnail") or "",
                "best": streams[0]["url"] if streams else "",
                "streams": streams,
            }
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.api_route(
    "/app/files/torrent", methods=["GET", "POST"], response_class=HTMLResponse
)
async def handle_torrent(request: Request):
    params = request.query_params

    if not (gid := params.get("gid")):
        return JSONResponse(
            {
                "files": [],
                "engine": "",
                "error": "GID is missing",
                "message": "GID not specified",
            }
        )

    if not _SAFE_GID.match(gid):
        return JSONResponse(
            {
                "files": [],
                "engine": "",
                "error": "Invalid GID",
                "message": "Invalid GID",
            }
        )

    if not (pin := params.get("pin")):
        return JSONResponse(
            {
                "files": [],
                "engine": "",
                "error": "Pin is missing",
                "message": "PIN not specified",
            }
        )

    if _pin_rate_limited(gid):
        return JSONResponse(
            {
                "files": [],
                "engine": "",
                "error": "Too many attempts",
                "message": f"Too many PIN attempts. Try again in {_PIN_RATE_WINDOW}s.",
            },
            status_code=429,
        )

    if not _verify_pin(gid, pin):
        _record_pin_attempt(gid)
        return JSONResponse(
            {
                "files": [],
                "engine": "",
                "error": "Invalid pin",
                "message": "The PIN you entered is incorrect. Try Again!",
            }
        )
    _pin_attempts.pop(gid, None)

    if request.method == "POST":
        if not (mode := params.get("mode")):
            return JSONResponse(
                {
                    "files": [],
                    "engine": "",
                    "error": "Mode is not specified",
                    "message": "Mode is not specified",
                }
            )
        data = await request.json()
        if mode == "rename":
            if len(gid) > 20:
                await handle_rename(gid, data)
                content = {
                    "files": [],
                    "engine": "",
                    "error": "",
                    "message": "Rename successfully.",
                }
            else:
                content = {
                    "files": [],
                    "engine": "",
                    "error": "Rename failed.",
                    "message": "Cannot rename aria2c torrent file",
                }
        else:
            selected_files, unselected_files = extract_file_ids(data)
            if gid.startswith("SABnzbd_nzo"):
                await set_sabnzbd(gid, unselected_files)
            elif len(gid) > 20:
                await set_qbittorrent(gid, selected_files, unselected_files)
            else:
                selected_files = ",".join(selected_files)
                await set_aria2(gid, selected_files)
            content = {
                "files": [],
                "engine": "",
                "error": "",
                "message": "Your selection has been submitted successfully.",
            }
    else:
        try:
            if gid.startswith("SABnzbd_nzo"):
                res = await sabnzbd_client.get_files(gid)
                content = make_tree(res, "sabnzbd")
            elif len(gid) > 20:
                res = await qbittorrent.torrents.files(gid)
                content = make_tree(res, "qbittorrent")
            else:
                res = await aria2.getFiles(gid)
                op = await aria2.getOption(gid)
                fpath = f"{op['dir']}/"
                content = make_tree(res, "aria2", fpath)
        except (ClientError, TimeoutError, Exception, AQError) as e:
            LOGGER.error(str(e))
            content = {
                "files": [],
                "engine": "",
                "error": "Error getting files",
                "message": str(e),
            }
    return JSONResponse(content)


async def handle_rename(gid, data):
    try:
        _type = data["type"]
        del data["type"]
        if _type == "file":
            await qbittorrent.torrents.rename_file(hash=gid, **data)
        else:
            await qbittorrent.torrents.rename_folder(hash=gid, **data)
    except (ClientError, TimeoutError, Exception, AQError) as e:
        LOGGER.error(f"{e} Errored in renaming")


async def set_sabnzbd(gid, unselected_files):
    await sabnzbd_client.remove_file(gid, unselected_files)
    LOGGER.info(f"Verified! nzo_id: {gid}")


async def set_qbittorrent(gid, selected_files, unselected_files):
    if unselected_files:
        try:
            await qbittorrent.torrents.file_prio(
                hash=gid, id=unselected_files, priority=0
            )
        except (ClientError, TimeoutError, Exception, AQError) as e:
            LOGGER.error(f"{e} Errored in paused")
    if selected_files:
        try:
            await qbittorrent.torrents.file_prio(
                hash=gid, id=selected_files, priority=1
            )
        except (ClientError, TimeoutError, Exception, AQError) as e:
            LOGGER.error(f"{e} Errored in resumed")
    await sleep(0.5)
    if not await re_verify(unselected_files, selected_files, gid):
        LOGGER.error(f"Verification Failed! Hash: {gid}")


async def set_aria2(gid, selected_files):
    res = await aria2.changeOption(gid, {"select-file": selected_files})
    if res == "OK":
        LOGGER.info(f"Verified! Gid: {gid}")
    else:
        LOGGER.info(f"Verification Failed! Report! Gid: {gid}")


@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    response = templates.TemplateResponse(request, "landing.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def rewrite_location(location: str, proxy_prefix: str) -> str:
    parsed = urlparse(location)
    if not parsed.netloc:
        return proxy_prefix + location
    if parsed.hostname in ["localhost", "127.0.0.1"]:
        return proxy_prefix + parsed.path
    return location


async def proxy_fetch(
    method: str, url: str, headers: dict, params: dict, body: bytes, proxy_prefix: str
):
    async with ClientSession(auto_decompress=True) as session:
        async with session.request(
            method,
            url,
            headers=headers,
            params=params,
            data=body,
            allow_redirects=False,
        ) as upstream:
            raw = [
                (k.lower().encode("latin-1"), v.encode("latin-1"))
                for k, v in upstream.headers.items()
                if k.lower() not in ("content-length", "content-encoding")
            ]
            if upstream.status in (301, 302, 303, 307, 308):
                loc = upstream.headers.get("Location")
                if loc:
                    new_loc = rewrite_location(loc, proxy_prefix)
                    raw = [
                        (k, new_loc.encode("latin-1") if k == b"location" else v)
                        for k, v in raw
                    ]
            body = (
                await upstream.read()
                if upstream.status not in (301, 302, 303, 307, 308)
                else b""
            )
            response = Response(content=body, status_code=upstream.status)
            response.raw_headers = raw
            return response


async def protected_proxy(
    service: str, path: str, request: Request, password: str = None
):
    from hmac import compare_digest

    service_info = SERVICES.get(service)
    if not service_info:
        raise HTTPException(status_code=404, detail="Service not found")
    if "password" in service_info:
        if password is None:
            password = request.query_params.get("pass") or request.cookies.get(
                f"{service}_pass"
            )
        if not password or not compare_digest(password, service_info["password"]):
            raise HTTPException(status_code=403, detail="Unauthorized access")
    if path:
        if not _SAFE_PATH.match(path):
            raise HTTPException(status_code=400, detail="Invalid path")
        if ".." in path.split("/"):
            raise HTTPException(status_code=400, detail="Invalid path")
    base = service_info["url"].rstrip("/")
    url = f"{base}/{path.lstrip('/')}" if path else f"{base}/"
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    body = await request.body()
    params = {k: v for k, v in request.query_params.items() if k != "pass"}
    if "password" in service_info:
        params["apikey"] = service_info["password"]
    response = await proxy_fetch(
        request.method, url, headers, params, body, f"/{service}"
    )
    if "pass" in request.query_params:
        is_https = request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(
            f"{service}_pass",
            password,
            httponly=True,
            samesite="strict",
            secure=is_https,
        )
    return response


@app.api_route("/nzb/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def sabnzbd_proxy(path: str = "", request: Request = None):
    return await protected_proxy("nzb", path, request)


@app.api_route("/qbit/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def qbittorrent_proxy(path: str = "", request: Request = None):
    return await protected_proxy("qbit", path, request)


@app.exception_handler(Exception)
async def page_not_found(_, exc):
    return HTMLResponse(
        f"<h1>404: Task not found! Mostly wrong input. <br><br>Error: {escape(str(exc))}</h1>",
        status_code=404,
    )


# ============================================================================
# STAGE 1: ANIME STARFALL SECURITY TOKENS & DUAL-MODE STUDIO API ENDPOINTS
# ============================================================================

from typing import Optional
from pydantic import BaseModel
from pymongo import AsyncMongoClient
from pymongo.server_api import ServerApi

import sys as _sys
from importlib.util import module_from_spec as _module_from_spec, spec_from_file_location as _spec_from_file_location
from os import path as _ospath


def _load_ext_module(mod_name: str, rel_path: str):
    if mod_name in _sys.modules:
        return _sys.modules[mod_name]
    full_path = _ospath.abspath(_ospath.join(_ospath.dirname(__file__), "..", rel_path))
    spec = _spec_from_file_location(mod_name, full_path)
    mod = _module_from_spec(spec)
    _sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_tm_mod = _load_ext_module("starfall_token_manager", "bot/helper/ext_utils/token_manager.py")
generate_user_token = _tm_mod.generate_user_token
verify_user_token = _tm_mod.verify_user_token
inspect_user_token = _tm_mod.inspect_user_token
resolve_base_url = _tm_mod.resolve_base_url

_wb_mod = _load_ext_module("starfall_web_bridge", "bot/helper/ext_utils/web_bridge.py")
enqueue_ipc_event = _wb_mod.enqueue_ipc_event
read_active_tasks_for_user = _wb_mod.read_active_tasks_for_user
read_user_snapshot = _wb_mod.read_user_snapshot
update_user_snapshot_mode = _wb_mod.update_user_snapshot_mode

_mongo_client = None


def _get_database_url() -> str:
    db_url = environ.get("DATABASE_URL", "").strip()
    if not db_url:
        try:
            cfg = import_module("config")
            db_url = str(getattr(cfg, "DATABASE_URL", "") or "").strip()
        except Exception:
            pass
    return db_url


def _get_mongo_db():
    global _mongo_client
    db_url = _get_database_url()
    if not db_url:
        return None
    if _mongo_client is None:
        _mongo_client = AsyncMongoClient(db_url, server_api=ServerApi("1"))
    return _mongo_client.wzmlx


def _get_partition_key() -> str:
    try:
        from hashlib import blake2b
        return blake2b(str(_BOT_ID or '0').encode(), digest_size=8).hexdigest()
    except Exception:
        return str(_BOT_ID or "0")


def _require_valid_user_token(token: Optional[str]) -> int:
    user_id = verify_user_token(token or "")
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired Starfall Studio magic token.",
        )
    return user_id


class ModeUpdateRequest(BaseModel):
    mode: str


class LeechStartRequest(BaseModel):
    url: str
    mode: Optional[str] = "leech"
    engine: Optional[str] = "auto"
    name: Optional[str] = ""
    select: Optional[bool] = False
    extract: Optional[bool] = False
    compress: Optional[bool] = False
    up_dest: Optional[str] = ""


@app.get("/api/user/session")
async def api_user_session(token: str):
    """
    Validate magic token and return:
    { "user_id": ..., "mode": "web"|"telegram", "dump_channel": ..., "username": ... }
    """
    user_id = _require_valid_user_token(token)
    snap = read_user_snapshot(user_id)
    mode = str(snap.get("mode", "telegram") or "telegram").lower()
    dump_channel = snap.get("dump_channel")
    username = snap.get("username") or f"user_{user_id}"

    # Check in-memory user_data if running in shared process
    try:
        bot_user_data = _sys.modules['bot'].user_data if 'bot' in _sys.modules else {}

        u_dict = bot_user_data.get(user_id, {})
        if u_dict:
            if "TASK_MODE" in u_dict:
                mode = str(u_dict["TASK_MODE"] or "telegram").lower()
            if u_dict.get("LEECH_DUMP_CHAT"):
                dump_channel = u_dict["LEECH_DUMP_CHAT"]
            if u_dict.get("USERNAME"):
                username = u_dict["USERNAME"]
    except Exception:
        pass

    # Query MongoDB users collection
    db = _get_mongo_db()
    if db is not None:
        try:
            doc = await db.users[_get_partition_key()].find_one({"_id": user_id})
            if doc:
                if "TASK_MODE" in doc and doc["TASK_MODE"]:
                    mode = str(doc["TASK_MODE"]).lower()
                if doc.get("LEECH_DUMP_CHAT"):
                    dump_channel = doc["LEECH_DUMP_CHAT"]
                if doc.get("USERNAME"):
                    username = doc["USERNAME"]
        except Exception as exc:
            LOGGER.warning(f"MongoDB session lookup warning for {user_id}: {exc}")

    if not dump_channel:
        dump_channel = environ.get("LEECH_DUMP_CHAT") or None
        if not dump_channel:
            try:
                cfg = import_module("config")
                dump_channel = getattr(cfg, "LEECH_DUMP_CHAT", None)
            except Exception:
                pass

    mode = "web" if mode == "web" else "telegram"
    return JSONResponse(
        {
            "user_id": user_id,
            "mode": mode,
            "dump_channel": dump_channel,
            "username": username,
        }
    )


@app.post("/api/user/mode")
async def api_update_user_mode(token: str, body: ModeUpdateRequest):
    """
    Update user's TASK_MODE ("web" | "telegram") in MongoDB, shared IPC snapshot, and in-memory user_data.
    """
    user_id = _require_valid_user_token(token)
    requested_mode = (body.mode or "").strip().lower()
    if requested_mode not in {"web", "telegram"}:
        raise HTTPException(
            status_code=400,
            detail="mode must be 'web' or 'telegram'",
        )

    # 1. Update in-memory user_data if shared
    try:
        bot_user_data = _sys.modules['bot'].user_data if 'bot' in _sys.modules else {}

        bot_user_data.setdefault(user_id, {})["TASK_MODE"] = requested_mode
    except Exception:
        pass

    # 2. Update shared snapshot & enqueue IPC sync to the live bot process
    update_user_snapshot_mode(user_id, requested_mode)

    # 3. Persist in MongoDB
    db = _get_mongo_db()
    if db is not None:
        try:
            await db.users[_get_partition_key()].update_one(
                {"_id": user_id},
                {"$set": {"TASK_MODE": requested_mode}},
                upsert=True,
            )
        except Exception as exc:
            LOGGER.warning(f"MongoDB mode update warning for {user_id}: {exc}")

    return JSONResponse(
        {
            "status": "ok",
            "user_id": user_id,
            "mode": requested_mode,
        }
    )


@app.get("/api/user/tasks")
async def api_get_user_tasks(token: str):
    """
    Return active tasks for the authenticated user (speeds, progress, size, ETA, engine).
    Combines live snapshots from the bot process's `task_dict` and MongoDB `web_tasks`.
    """
    user_id = _require_valid_user_token(token)
    tasks_list = []
    seen_gids = set()

    # 1. Read live task_dict snapshot exported by the bot process
    for t in read_active_tasks_for_user(user_id):
        gid = str(t.get("gid") or t.get("task_id"))
        seen_gids.add(gid)
        tasks_list.append(t)

    # 2. Also inspect in-process task_dict if running in the same process
    try:
        bot_task_dict = _sys.modules['bot'].task_dict if 'bot' in _sys.modules else {}

        for mid, status_obj in list(bot_task_dict.items()):
            listener = getattr(status_obj, "listener", None)
            if listener and getattr(listener, "user_id", None) == user_id:
                gid = str(status_obj.gid()) if callable(getattr(status_obj, "gid", None)) else str(mid)
                if gid in seen_gids:
                    continue
                seen_gids.add(gid)
                tasks_list.append(
                    {
                        "task_id": mid,
                        "gid": gid,
                        "name": str(status_obj.name()) if callable(getattr(status_obj, "name", None)) else getattr(listener, "name", "Task"),
                        "status": str(status_obj.status()) if callable(getattr(status_obj, "status", None)) else "Processing",
                        "progress": str(status_obj.progress()) if callable(getattr(status_obj, "progress", None)) else "0%",
                        "processed_bytes": str(status_obj.processed_bytes()) if callable(getattr(status_obj, "processed_bytes", None)) else "0 B",
                        "size": str(status_obj.size()) if callable(getattr(status_obj, "size", None)) else "0 B",
                        "speed": str(status_obj.speed()) if callable(getattr(status_obj, "speed", None)) else "0 B/s",
                        "eta": str(status_obj.eta()) if callable(getattr(status_obj, "eta", None)) else "-",
                        "engine": str(getattr(status_obj, "engine", "StarfallEngine")),
                    }
                )
    except Exception:
        pass

    return JSONResponse(
        {
            "user_id": user_id,
            "count": len(tasks_list),
            "tasks": tasks_list,
        }
    )


@app.post("/api/leech/start")
async def api_start_leech_task(token: str, body: LeechStartRequest):
    """
    Start a direct leech or mirror task from the Web Studio dashboard.
    Dispatches via in-process `start_web_leech_task` or the cross-process IPC bridge to the live bot.
    """
    user_id = _require_valid_user_token(token)
    source_url = (body.url or "").strip()
    if not source_url:
        raise HTTPException(status_code=400, detail="url is required")

    task_mode = "mirror" if (body.mode or "leech").strip().lower() == "mirror" else "leech"
    engine = (body.engine or "auto").strip().lower()
    task_name = (body.name or "").strip() or source_url.split("/")[-1][:64] or "Web Studio Task"
    dashboard_url = f"{resolve_base_url()}/app/dashboard?token={token}"

    # 1. Try direct in-process dispatch if TgClient.bot is running in this process
    try:
        if 'bot' not in _sys.modules:
            raise RuntimeError('bot runs in separate process')
        from bot.core.tg_client import TgClient
        from bot.helper.mirror_leech import start_web_leech_task

        if TgClient.bot is not None:
            res = await start_web_leech_task(
                user_id=user_id,
                url=source_url,
                mode=task_mode,
                engine=engine,
                custom_name=body.name or "",
                select=bool(body.select),
                extract=bool(body.extract),
                compress=bool(body.compress),
                up_dest=body.up_dest or "",
            )
            return JSONResponse(
                {
                    "status": "started",
                    "user_id": user_id,
                    "task_id": res["mid"],
                    "gid": f"web_{res['mid']}",
                    "name": task_name,
                    "mode": task_mode,
                    "engine": engine,
                    "dashboard_url": dashboard_url,
                }
            )
    except Exception:
        pass

    # 2. Enqueue via IPC bridge to the live Pyrogram bot process (`start_web_bridge_daemon`)
    synthetic_id = f"sf_{int(time() * 1000)}"
    enqueue_ipc_event(
        {
            "type": "start_leech",
            "task_id": synthetic_id,
            "user_id": user_id,
            "url": source_url,
            "mode": task_mode,
            "engine": engine,
            "name": body.name or "",
            "select": bool(body.select),
            "extract": bool(body.extract),
            "compress": bool(body.compress),
            "up_dest": body.up_dest or "",
        }
    )

    return JSONResponse(
        {
            "status": "started",
            "user_id": user_id,
            "task_id": synthetic_id,
            "gid": synthetic_id,
            "name": task_name,
            "mode": task_mode,
            "engine": engine,
            "dashboard_url": dashboard_url,
        }
    )


class CancelTaskRequest(BaseModel):
    gid: Optional[str] = ""
    task_id: Optional[str] = ""


@app.get("/app/user", response_class=HTMLResponse)
@app.get("/app/dashboard", response_class=HTMLResponse)
async def user_studio_dashboard(request: Request):
    """Serve the standalone Anime Starfall Web Studio Dashboard (user_studio.html) with no-cache headers."""
    response = templates.TemplateResponse(request, "user_studio.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.post("/api/leech/cancel")
async def api_cancel_leech_task(token: str, body: CancelTaskRequest):
    """Cancel an active task from the Web Studio Dashboard."""
    user_id = _require_valid_user_token(token)
    gid = (body.gid or body.task_id or "").strip()
    if not gid:
        raise HTTPException(status_code=400, detail="gid or task_id is required")

    enqueue_ipc_event(
        {
            "type": "cancel_task",
            "user_id": user_id,
            "gid": gid,
            "task_id": body.task_id or gid,
        }
    )
    return JSONResponse({"status": "canceling", "gid": gid, "user_id": user_id})

# ============================================================================
# STAGE 3A: VIDEO TOOLS, LOSSLESS AUDIO/SUB SYNC & MANUAL THUMBNAIL STUDIO API
# ============================================================================

from asyncio import create_subprocess_exec
from asyncio.subprocess import PIPE
from base64 import b64decode
from json import loads as json_loads
from os import walk as os_walk, makedirs as os_makedirs


class VideoMuxRequest(BaseModel):
    task_id: str
    file_name: Optional[str] = ""
    mode: Optional[str] = "stream_copy_only"
    transcode_video: Optional[bool] = False
    audio_tracks: list = []
    subtitle_tracks: list = []
    thumbnail_mode: Optional[str] = "frame_grabber"
    frame_timestamp: Optional[str] = "00:04:50"
    cli_preview: Optional[str] = ""


class VideoThumbnailRequest(BaseModel):
    task_id: str
    image_base64: str


def _locate_media_file_for_task(task_id: str) -> str:
    """Locate the primary video file inside `/usr/src/app/downloads/{task_id}`."""
    base_dir = f"/usr/src/app/downloads/{task_id}"
    video_exts = (".mkv", ".mp4", ".webm", ".avi", ".mov", ".ts", ".m4v")
    best_path = ""
    best_size = -1
    if _ospath.exists(base_dir):
        for root, _, files in os_walk(base_dir):
            for fn in files:
                if fn.lower().endswith(video_exts):
                    full_p = _ospath.join(root, fn)
                    try:
                        sz = _ospath.getsize(full_p)
                        if sz > best_size:
                            best_size = sz
                            best_path = full_p
                    except OSError:
                        pass
    return best_path


@app.get("/app/video-tools", response_class=HTMLResponse)
async def video_tools_studio_page(request: Request):
    """Serve `video_studio.html` with strict no-cache headers."""
    response = templates.TemplateResponse(request, "video_studio.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/api/video/probe")
async def api_video_probe(token: str, task_id: str):
    """
    Probe media container via `ffprobe` and return structured video, audio, and subtitle stream hierarchy.
    """
    user_id = _require_valid_user_token(token)
    media_path = _locate_media_file_for_task(task_id)

    if not media_path:
        # Return structured fallback metadata if the file is still staging
        return JSONResponse(
            {
                "user_id": user_id,
                "task_id": task_id,
                "file_name": f"Task_{task_id}.mkv",
                "video": {
                    "index": 0,
                    "codec": "HEVC / x265",
                    "resolution": "1920×1080 (1080p)",
                    "bitDepth": "10-bit HDR10",
                    "aspect": "16:9",
                    "durationSec": 1452,
                },
                "audio_tracks": [],
                "subtitle_tracks": [],
            }
        )

    proc = await create_subprocess_exec(
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        media_path,
        stdout=PIPE,
        stderr=PIPE,
    )
    stdout, _ = await proc.communicate()
    probe_data = json_loads(stdout.decode("utf-8", errors="replace") or "{}")

    streams = probe_data.get("streams", [])
    fmt = probe_data.get("format", {})
    duration_sec = int(float(fmt.get("duration", 1452) or 1452))

    video_info = {
        "index": 0,
        "codec": "HEVC",
        "resolution": "1920×1080",
        "bitDepth": "10-bit",
        "aspect": "16:9",
        "durationSec": duration_sec,
    }
    audio_tracks = []
    subtitle_tracks = []

    for st in streams:
        ctype = st.get("codec_type")
        idx = int(st.get("index", 0))
        codec_name = str(st.get("codec_name", "unknown")).upper()
        tags = st.get("tags", {}) or {}
        disp = st.get("disposition", {}) or {}
        lang = str(tags.get("language", "jpn")).lower()
        title = str(tags.get("title", f"Track #{idx}"))

        if ctype == "video":
            w = st.get("width", 1920)
            h = st.get("height", 1080)
            pix = str(st.get("pix_fmt", ""))
            video_info = {
                "index": idx,
                "codec": codec_name,
                "resolution": f"{w}×{h}",
                "bitDepth": "10-bit" if "10" in pix else "8-bit",
                "aspect": st.get("display_aspect_ratio", "16:9"),
                "durationSec": duration_sec,
            }
        elif ctype == "audio":
            channels = st.get("channels", 2)
            ch_str = "5.1" if channels >= 6 else "2.0"
            audio_tracks.append(
                {
                    "id": idx,
                    "codec": f"{codec_name} {ch_str}",
                    "lang": lang,
                    "title": title,
                    "delayMs": 0,
                    "keep": True,
                    "isDefault": bool(disp.get("default", 0)),
                    "isForced": bool(disp.get("forced", 0)),
                }
            )
        elif ctype == "subtitle":
            subtitle_tracks.append(
                {
                    "id": idx,
                    "codec": codec_name,
                    "lang": lang,
                    "title": title,
                    "keep": True,
                    "isDefault": bool(disp.get("default", 0)),
                    "isForced": bool(disp.get("forced", 0)),
                }
            )

    return JSONResponse(
        {
            "user_id": user_id,
            "task_id": task_id,
            "file_name": _ospath.basename(media_path),
            "video": video_info,
            "audio_tracks": audio_tracks,
            "subtitle_tracks": subtitle_tracks,
        }
    )


@app.get("/api/video/frame")
async def api_video_frame(token: str, task_id: str, ss: str = "00:04:50"):
    """
    Extract a single JPEG frame at timestamp `ss` using `ffmpeg -threads 1 -ss {ss} -i {file} -vframes 1 -q:v 2`.
    """
    _require_valid_user_token(token)
    media_path = _locate_media_file_for_task(task_id)
    if not media_path:
        raise HTTPException(status_code=404, detail="Media file not found for task_id")

    proc = await create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        "1",
        "-ss",
        str(ss),
        "-i",
        media_path,
        "-vframes",
        "1",
        "-q:v",
        "2",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
        stdout=PIPE,
        stderr=PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0 or not stdout:
        raise HTTPException(
            status_code=500,
            detail=f"Frame extraction failed: {stderr.decode('utf-8', errors='replace')[:200]}",
        )
    return Response(content=stdout, media_type="image/jpeg")


@app.post("/api/video/thumbnail")
async def api_video_thumbnail(token: str, body: VideoThumbnailRequest):
    """Save a custom 16:9 thumbnail image for `task_id`."""
    user_id = _require_valid_user_token(token)
    raw_b64 = (body.image_base64 or "").strip()
    if "," in raw_b64:
        raw_b64 = raw_b64.split(",", 1)[1]
    try:
        img_bytes = b64decode(raw_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 image payload") from exc

    os_makedirs("thumbnails", exist_ok=True)
    task_thumb_path = f"thumbnails/task_{body.task_id}.jpg"
    user_thumb_path = f"thumbnails/{user_id}.jpg"
    for p in (task_thumb_path, user_thumb_path):
        with open(p, "wb") as fp:
            fp.write(img_bytes)

    return JSONResponse(
        {
            "status": "saved",
            "user_id": user_id,
            "task_id": body.task_id,
            "thumbnail_path": task_thumb_path,
        }
    )


@app.post("/api/video/mux")
async def api_video_mux(token: str, body: VideoMuxRequest):
    """
    Enqueue a strictly lossless (`mkvmerge` / `ffmpeg -c copy`) remux & audio-sync job
    to `/tmp/starfall_web_ipc.jsonl` for background execution and silent dump upload.
    """
    user_id = _require_valid_user_token(token)
    if body.transcode_video:
        raise HTTPException(
            status_code=400,
            detail="Video transcoding is strictly forbidden on this VPS. Use stream_copy_only.",
        )

    enqueue_ipc_event(
        {
            "type": "mux_video",
            "user_id": user_id,
            "task_id": body.task_id,
            "file_name": body.file_name or "",
            "mode": "stream_copy_only",
            "audio_tracks": body.audio_tracks,
            "subtitle_tracks": body.subtitle_tracks,
            "thumbnail_mode": body.thumbnail_mode or "frame_grabber",
            "frame_timestamp": body.frame_timestamp or "00:04:50",
        }
    )

    return JSONResponse(
        {
            "status": "queued_lossless_mux",
            "user_id": user_id,
            "task_id": body.task_id,
            "engine": "mkvmerge-stream-copy",
        }
    )
