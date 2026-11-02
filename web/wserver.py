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


# --- STARFALL VIDEO TOOLS: MERGE & SYNC STUDIO ENDPOINTS ---
import base64
import os
import mimetypes
from bot.helper.video_utils.vps_audio_bank import (
    verify_vt_studio_token,
    load_web_session,
    list_audio_bank,
    get_audio_bank_dir,
    clear_audio_bank,
    write_web_cmd,
    write_web_submission,
)


def stream_local_file(file_path: str, request: Request, default_mime: str = "application/octet-stream"):
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    file_size = os.path.getsize(file_path)
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in [".mkv"]:
            mime_type = "video/x-matroska"
        elif ext in [".mka"]:
            mime_type = "audio/x-matroska"
        elif ext in [".mp4", ".m4v"]:
            mime_type = "video/mp4"
        elif ext in [".m4a"]:
            mime_type = "audio/mp4"
        elif ext in [".aac"]:
            mime_type = "audio/aac"
        elif ext in [".mp3"]:
            mime_type = "audio/mpeg"
        elif ext in [".opus", ".ogg"]:
            mime_type = "audio/ogg"
        elif ext in [".flac"]:
            mime_type = "audio/flac"
        elif ext in [".webm"]:
            mime_type = "video/webm"
        else:
            mime_type = default_mime

    range_header = request.headers.get("range")
    safe_name = os.path.basename(file_path)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{safe_name}"',
        "Cache-Control": "no-cache",
    }

    if not range_header:
        headers["Content-Length"] = str(file_size)
        if request.method == "HEAD":
            return Response(status_code=200, media_type=mime_type, headers=headers)

        async def full_file_sender():
            def read_chunk(f, size):
                return f.read(size)
            with open(file_path, "rb") as f:
                chunk_size = 256 * 1024
                while True:
                    data = await asyncio.to_thread(read_chunk, f, chunk_size)
                    if not data:
                        break
                    yield data

        return StreamingResponse(full_file_sender(), status_code=200, media_type=mime_type, headers=headers)

    try:
        byte_range = parse_byte_range(range_header, file_size)
    except ValueError:
        headers["Content-Range"] = f"bytes */{file_size}"
        return Response(status_code=416, headers=headers)

    start, end = byte_range
    content_length = end - start + 1
    headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    headers["Content-Length"] = str(content_length)

    if request.method == "HEAD":
        return Response(status_code=206, media_type=mime_type, headers=headers)

    async def range_file_sender():
        def read_chunk(f, size):
            return f.read(size)
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = content_length
            chunk_size = 256 * 1024
            while remaining > 0:
                to_read = min(chunk_size, remaining)
                data = await asyncio.to_thread(read_chunk, f, to_read)
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(range_file_sender(), status_code=206, media_type=mime_type, headers=headers)


@app.get("/app/video-studio", response_class=HTMLResponse)
async def video_studio_page(request: Request, task_id: str = "", token: str = ""):
    if not task_id or not token:
        raise HTTPException(status_code=403, detail="Task ID and authentication token required")
    if not verify_vt_studio_token(task_id, token):
        raise HTTPException(status_code=403, detail="Invalid or expired video studio token")
    response = templates.TemplateResponse(request, "video_studio.html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/api/video/probe")
async def video_probe_api(task_id: str = "", token: str = ""):
    if not task_id or not token:
        raise HTTPException(status_code=403, detail="Missing task_id or token")
    if not verify_vt_studio_token(task_id, token):
        raise HTTPException(status_code=403, detail="Invalid studio token")
    session = await load_web_session(task_id)
    if not session:
        raise HTTPException(status_code=404, detail="Task session not found or completed")
    user_id = session.get("user_id", 0)
    bank_data = await list_audio_bank(user_id)
    return JSONResponse(
        {
            "task_id": session.get("task_id", task_id),
            "file_name": session.get("file_name", "Video.mkv"),
            "is_folder": session.get("is_folder", False),
            "video": session.get("video_stream", {}),
            "audio_tracks": session.get("audio_tracks", []),
            "subtitle_tracks": session.get("sub_tracks", []),
            "external_audio": session.get("external_audio", []),
            "bank_data": bank_data,
            "matched_bank": session.get("matched_bank") or {},
            "total_episodes": session.get("total_episodes", 1),
            "video_files": session.get("video_files", []),
            "audio_delay_ms": session.get("audio_delay_ms", 0),
            "single_sample_for_folder": True,
        }
    )


@app.api_route("/api/video/stream", methods=["GET", "HEAD"])
async def video_stream_api(
    request: Request,
    task_id: str = "",
    token: str = "",
    source: str = "video",
    file_idx: int = 0,
    bank_path: str = "",
):
    if not task_id or not token:
        raise HTTPException(status_code=403, detail="Missing credentials")
    if not verify_vt_studio_token(task_id, token):
        raise HTTPException(status_code=403, detail="Invalid token")
    session = await load_web_session(task_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    target_path = ""
    if source == "video":
        base_path = session.get("file_path", "")
        if os.path.isdir(base_path):
            videos = session.get("video_files", [])
            if 0 <= file_idx < len(videos):
                target_path = os.path.join(base_path, videos[file_idx])
            else:
                for v in videos:
                    p = os.path.join(base_path, v)
                    if os.path.isfile(p):
                        target_path = p
                        break
        else:
            target_path = base_path
    elif source == "audio":
        ext_audio = session.get("external_audio", [])
        if 0 <= file_idx < len(ext_audio):
            target_path = ext_audio[file_idx].get("path", "")
    elif source == "bank":
        user_id = session.get("user_id", 0)
        bank_dir = get_audio_bank_dir(user_id)
        if bank_path:
            clean_bank_path = os.path.abspath(bank_path)
            clean_bank_dir = os.path.abspath(bank_dir)
            if clean_bank_path.startswith(clean_bank_dir) and os.path.isfile(clean_bank_path):
                target_path = clean_bank_path
            else:
                candidate = os.path.join(bank_dir, os.path.basename(bank_path))
                if os.path.isfile(candidate):
                    target_path = candidate

    if not target_path or not os.path.isfile(target_path):
        raise HTTPException(status_code=404, detail="Media stream file not found on disk")

    return stream_local_file(target_path, request)


@app.get("/api/video/frame")
async def video_frame_api(task_id: str = "", token: str = "", ss: str = "00:04:50"):
    if not task_id or not token:
        raise HTTPException(status_code=403, detail="Missing credentials")
    if not verify_vt_studio_token(task_id, token):
        raise HTTPException(status_code=403, detail="Invalid token")
    session = await load_web_session(task_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    file_path = session.get("file_path", "")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Media file not found on disk")
    if os.path.isdir(file_path):
        videos = session.get("video_files", [])
        found = False
        for v in videos:
            p = os.path.join(file_path, v)
            if os.path.isfile(p):
                file_path = p
                found = True
                break
        if not found:
            raise HTTPException(status_code=404, detail="No video file found in folder")

    # Strictly lightweight single-frame grab to keep RAM under 4GB safe limits
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        ss,
        "-i",
        file_path,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-f",
        "image2",
        "-",
    ]
    try:
        import asyncio

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        if stdout and len(stdout) > 100:
            return Response(content=stdout, media_type="image/jpeg")
        raise HTTPException(status_code=500, detail="Failed to extract frame")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Frame grab error: {e}")


@app.post("/api/video/thumbnail")
async def video_thumbnail_api(request: Request, token: str = ""):
    data = await request.json()
    task_id = data.get("task_id", "")
    if not task_id or not token:
        raise HTTPException(status_code=403, detail="Missing task_id or token")
    if not verify_vt_studio_token(task_id, token):
        raise HTTPException(status_code=403, detail="Invalid token")
    b64_img = data.get("image_base64", "")
    if "," in b64_img:
        b64_img = b64_img.split(",", 1)[1]
    if b64_img:
        img_bytes = base64.b64decode(b64_img)
        thumb_dir = "downloads/thumbnails"
        os.makedirs(thumb_dir, exist_ok=True)
        thumb_path = os.path.join(thumb_dir, f"{task_id}.jpg")
        with open(thumb_path, "wb") as f:
            f.write(img_bytes)
    return JSONResponse({"status": "ok"})


@app.post("/api/video/preview")
async def video_preview_api(request: Request, token: str = ""):
    data = await request.json()
    task_id = data.get("task_id", "")
    if not task_id or not token:
        raise HTTPException(status_code=403, detail="Missing task_id or token")
    if not verify_vt_studio_token(task_id, token):
        raise HTTPException(status_code=403, detail="Invalid token")
    delay_ms = int(data.get("delay_ms") or 0)
    await write_web_cmd(task_id, "preview", {"delay_ms": delay_ms})
    return JSONResponse({"status": "preview_queued"})


@app.post("/api/video/extracted/clear")
async def video_clear_extracted_api(request: Request, token: str = ""):
    data = await request.json()
    task_id = data.get("task_id", "")
    if not task_id or not token:
        raise HTTPException(status_code=403, detail="Missing task_id or token")
    if not verify_vt_studio_token(task_id, token):
        raise HTTPException(status_code=403, detail="Invalid token")
    session = await load_web_session(task_id)
    user_id = session.get("user_id", 0) if session else 0
    del_count, freed_bytes = await clear_audio_bank(user_id)
    await write_web_cmd(task_id, "clear_bank")
    return JSONResponse(
        {"status": "ok", "deleted_files": del_count, "freed_bytes": freed_bytes}
    )


@app.post("/api/video/mux")
async def video_mux_api(request: Request, token: str = ""):
    data = await request.json()
    task_id = data.get("task_id", "")
    if not task_id or not token:
        raise HTTPException(status_code=403, detail="Missing task_id or token")
    if not verify_vt_studio_token(task_id, token):
        raise HTTPException(status_code=403, detail="Invalid token")
    await write_web_submission(task_id, data)
    return JSONResponse({"status": "mux_queued"})


@app.exception_handler(Exception)
async def page_not_found(_, exc):
    return HTMLResponse(
        f"<h1>404: Task not found! Mostly wrong input. <br><br>Error: {escape(str(exc))}</h1>",
        status_code=404,
    )
