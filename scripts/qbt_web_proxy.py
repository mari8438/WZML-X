#!/usr/bin/env python3
"""
Secure qBittorrent WebUI Proxy with HTTP Basic Authentication
Protects public port 8090 with username and password authentication.
"""
import asyncio
import base64
import json
import os
import sys
import time

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8090
TARGET_HOST = "172.18.0.3"
TARGET_PORT = 8090

CONFIG_PATH = "/home/administrator/starfall/secrets/qbt_web_auth.json"

def get_credentials():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
                return data.get("username", "admin"), data.get("password", os.environ.get("QBT_PASSWORD", "adminadmin"))
        except Exception:
            pass
    return "admin", os.environ.get("QBT_PASSWORD", "adminadmin")

UNAUTHORIZED_RESPONSE = (
    b"HTTP/1.1 401 Unauthorized\r\n"
    b"WWW-Authenticate: Basic realm=\"qBittorrent WebUI Access\"\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"Content-Length: 125\r\n"
    b"Connection: close\r\n\r\n"
    b"<!DOCTYPE html><html><body><h2>401 Unauthorized</h2><p>Please enter your credentials to access WebUI.</p></body></html>"
)

def check_basic_auth(headers_raw):
    expected_user, expected_pass = get_credentials()
    lines = headers_raw.split(b"\r\n")
    for line in lines:
        if line.lower().startswith(b"authorization: basic "):
            b64_str = line.split(b" ", 2)[2].strip()
            try:
                decoded = base64.b64decode(b64_str).decode("utf-8", errors="ignore")
                if ":" in decoded:
                    user, pwd = decoded.split(":", 1)
                    if user == expected_user and pwd == expected_pass:
                        return True
            except Exception:
                pass
    return False

async def pipe(reader, writer):
    try:
        while not reader.at_eof():
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

async def handle_client(client_reader, client_writer):
    initial_data = bytearray()
    try:
        # Read initial HTTP request headers
        while True:
            chunk = await asyncio.wait_for(client_reader.read(4096), timeout=5.0)
            if not chunk:
                break
            initial_data.extend(chunk)
            if b"\r\n\r\n" in initial_data or len(initial_data) > 65536:
                break

        if not initial_data:
            client_writer.close()
            return

        # Check authentication
        if not check_basic_auth(bytes(initial_data)):
            client_writer.write(UNAUTHORIZED_RESPONSE)
            await client_writer.drain()
            client_writer.close()
            await client_writer.wait_closed()
            return

        # Connect to qBittorrent backend
        target_reader, target_writer = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)

        # Forward initial buffered bytes
        target_writer.write(initial_data)
        await target_writer.drain()

        # Pipe bi-directionally
        await asyncio.gather(
            pipe(client_reader, target_writer),
            pipe(target_reader, client_writer),
            return_exceptions=True
        )
    except Exception:
        try:
            client_writer.close()
        except Exception:
            pass

async def main():
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT, reuse_address=True)
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
