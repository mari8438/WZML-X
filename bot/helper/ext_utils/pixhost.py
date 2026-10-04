from os import path as ospath
from urllib.parse import urlsplit

from httpx import AsyncClient


PIXHOST_API = "https://api.pixhost.cc"
PIXHOST_HOSTS = {"pixhost.to", "pixhost.cc", "pixho.st"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class PixhostError(RuntimeError):
    pass


def validate_pixhost_url(value):
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not any(host == item or host.endswith(f".{item}") for item in PIXHOST_HOSTS)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not ospath.splitext(parsed.path)[1].lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
    ):
        raise PixhostError("Pixhost returned an invalid direct image URL")
    return parsed.geturl()


async def upload_image(path, content_type=1, max_th_size=500, client=None):
    size = ospath.getsize(path)
    if size > MAX_IMAGE_BYTES:
        raise PixhostError(f"Image exceeds the Pixhost 10 MB limit: {ospath.basename(path)}")
    owns_client = client is None
    client = client or AsyncClient(timeout=120)
    try:
        with open(path, "rb") as image:
            response = await client.post(
                f"{PIXHOST_API}/images",
                headers={"Accept": "application/json"},
                files={"img": (ospath.basename(path), image, "image/jpeg")},
                data={
                    "content_type": str(int(content_type)),
                    "max_th_size": str(max(150, min(int(max_th_size), 500))),
                    "optimize_for_web": "1",
                },
            )
        response.raise_for_status()
        payload = response.json()
        return validate_pixhost_url(payload.get("th_url"))
    except PixhostError:
        raise
    except Exception as exc:
        raise PixhostError(f"Pixhost upload failed for {ospath.basename(path)}: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()
