#!/usr/bin/env python3
"""
StarFallX AGY Background Daemon
Listens for /prompt tasks dispatched from the Telegram bot container,
executes tasks using Antigravity CLI (agy) on the VPS host,
handles git commit/push, container sync & restart,
and streams rich Telegram status reports directly to the group.
"""

import asyncio
import html
import json
import logging
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import urllib.request
import urllib.parse

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("StarFallX-AGY-Daemon")

REPO_DIR = os.environ.get("STARFALL_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BRANCH = os.environ.get("STARFALL_BRANCH", "starfallx-v1.2-max-speed")
CONTAINER_NAME = "starfall-app-1"
HOST = "0.0.0.0"
PORT = 18888

def get_bot_token() -> str:
    token = os.environ.get("BOT_TOKEN", "")
    if token:
        return token
    config_path = os.path.join(REPO_DIR, "config.py")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("BOT_TOKEN"):
                    match = re.search(r'["\']([^"\']+)["\']', line)
                    if match:
                        return match.group(1)
    return ""

BOT_TOKEN = get_bot_token()

def tg_api_call(method: str, data: dict, files: dict = None):
    if not BOT_TOKEN:
        LOGGER.error("No BOT_TOKEN found. Cannot send Telegram update.")
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        if files:
            boundary = f"----WebKitFormBoundary{int(time.time()*1000)}"
            body = bytearray()
            for key, val in data.items():
                body.extend(f"--{boundary}\r\n".encode())
                body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
                body.extend(f"{val}\r\n".encode())
            for field, (fname, fcontent) in files.items():
                body.extend(f"--{boundary}\r\n".encode())
                body.extend(f'Content-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'.encode())
                body.extend(b"Content-Type: text/plain\r\n\r\n")
                body.extend(fcontent if isinstance(fcontent, bytes) else fcontent.encode())
                body.extend(b"\r\n")
            body.extend(f"--{boundary}--\r\n".encode())
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
        else:
            req_data = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        LOGGER.error(f"Telegram API {method} error: {e}")
        return None

def update_status(chat_id, message_id, text, reply_to_message_id=None):
    if message_id:
        res = tg_api_call("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        if res and res.get("ok"):
            return message_id
    res = tg_api_call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_to_message_id": reply_to_message_id,
        "disable_web_page_preview": True,
    })
    if res and res.get("ok"):
        return res["result"]["message_id"]
    return None

def send_report_document(chat_id, filename, content, caption, reply_to_message_id=None):
    return tg_api_call("sendDocument", {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
        "reply_to_message_id": reply_to_message_id,
    }, files={"document": (filename, content)})

def run_cmd(cmd: str, cwd: str = REPO_DIR, timeout: int = 600) -> tuple:
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout} seconds."
    except Exception as e:
        return -1, "", str(e)

def execute_task_worker(task: dict):
    prompt = task.get("prompt", "").strip()
    chat_id = task.get("chat_id")
    msg_id = task.get("message_id")
    reply_to = task.get("reply_to_message_id")
    user_name = task.get("user_name", "User")

    LOGGER.info(f"Starting AGY Task: {prompt} (Requested by {user_name})")
    start_time = time.time()

    safe_prompt = html.escape(prompt)
    update_status(
        chat_id,
        msg_id,
        f"<blockquote><b>⚡ STARFALLX AGY • RUNNING</b>\n"
        f"<b>Task:</b> <code>{safe_prompt[:200]}</code>\n"
        f"<b>Requested By:</b> {html.escape(user_name)}\n"
        f"<b>Engine:</b> Antigravity CLI (Host VPS)\n"
        f"<b>Status:</b> 🤖 <i>Agent analyzing codebase and executing...</i></blockquote>",
        reply_to,
    )

    # 1. Execute agy non-interactively with auto-approved permissions
    agy_cmd = f"agy -p {subprocess.list2cmdline([prompt])} --dangerously-skip-permissions"
    code, stdout, stderr = run_cmd(agy_cmd, cwd=REPO_DIR, timeout=900)
    elapsed = int(time.time() - start_time)

    combined_output = stdout
    if stderr:
        combined_output += f"\n[Errors / Warnings]\n{stderr}"

    # 2. Check for git modifications in REPO_DIR
    _, git_status, _ = run_cmd("git status --porcelain", cwd=REPO_DIR)
    modified_files = [line.strip() for line in git_status.strip().splitlines() if line.strip()]

    deploy_status = "ℹ️ No code changes detected."
    compile_ok = True

    if modified_files:
        LOGGER.info(f"Modified files detected: {modified_files}")
        # Validate Python syntax
        c_code, c_out, c_err = run_cmd("python3 -m py_compile bot/**/*.py", cwd=REPO_DIR)
        if c_code != 0:
            compile_ok = False
            deploy_status = f"❌ Syntax check failed! Skipping deployment.\n<code>{html.escape(c_err[:200])}</code>"
        else:
            # Commit and push
            commit_title = prompt.splitlines()[0][:60].replace('"', "'")
            run_cmd("git add bot/ tests/ configs/ scripts/", cwd=REPO_DIR)
            run_cmd(f'git commit -m "feat(agy): {commit_title}"', cwd=REPO_DIR)
            run_cmd(f"git push origin {BRANCH}", cwd=REPO_DIR)

            # Copy to container and restart
            run_cmd(f"docker cp {REPO_DIR}/bot {CONTAINER_NAME}:/usr/src/app/", cwd=REPO_DIR)
            r_code, _, _ = run_cmd(f"docker restart {CONTAINER_NAME}", cwd=REPO_DIR)
            if r_code == 0:
                deploy_status = f"✅ <b>Auto-Deployed & Container Restarted!</b> ({len(modified_files)} files committed & pushed)"
            else:
                deploy_status = f"⚠️ Changes committed, but docker restart returned code {r_code}."

    # 3. Format Telegram Report
    summary_preview = combined_output.strip()
    if len(summary_preview) > 1500:
        summary_preview = summary_preview[:1500] + "\n... [Full log attached as document below]"

    report_text = (
        f"<blockquote><b>✦ STARFALLX AGY • TASK COMPLETE ✦</b>\n"
        f"<b>Task:</b> <code>{safe_prompt[:150]}</code>\n"
        f"<b>Elapsed:</b> <code>{elapsed}s</code> | <b>Exit Code:</b> <code>{code}</code>\n"
        f"<b>Deploy:</b> {deploy_status}\n\n"
        f"<b>Output:</b>\n"
        f"<code>{html.escape(summary_preview)}</code></blockquote>"
    )

    update_status(chat_id, msg_id, report_text, reply_to)

    # 4. If log is substantial, send full log as a document
    if len(combined_output) > 1500 or not compile_ok:
        log_content = (
            f"StarFallX AGY Task Log\n"
            f"Prompt: {prompt}\n"
            f"Requested By: {user_name}\n"
            f"Elapsed: {elapsed}s\n"
            f"Exit Code: {code}\n"
            f"Git Status:\n{git_status}\n"
            f"{'='*60}\n"
            f"{combined_output}"
        )
        fname = f"agy_task_{int(start_time)}.txt"
        send_report_document(chat_id, fname, log_content, f"📄 Full AGY execution log for: <code>{safe_prompt[:60]}</code>", reply_to)

class DaemonRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        LOGGER.info("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), format % args))

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy", "engine": "agy"}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/execute":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            try:
                task = json.loads(post_data.decode("utf-8"))
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
                return

            if not task.get("prompt"):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "Missing prompt"}')
                return

            # Dispatch worker in background thread
            t = threading.Thread(target=execute_task_worker, args=(task,), daemon=True)
            t.start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "queued", "message": "Task dispatched to AGY"}).encode())
        else:
            self.send_response(404)
            self.end_headers()

def main():
    LOGGER.info(f"Starting StarFallX AGY Daemon on {HOST}:{PORT}...")
    server = HTTPServer((HOST, PORT), DaemonRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Daemon shutting down...")
        server.server_close()

if __name__ == "__main__":
    main()
