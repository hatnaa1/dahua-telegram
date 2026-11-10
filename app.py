"""Dahua/Akuvox SMTP/HTTP to Telegram bridge. Python 3.12+."""
import asyncio
import hmac
import json
import logging
import math
import os
import re
from email.utils import parseaddr
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import requests
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword

LOG = logging.getLogger("dahua")
MAX_BYTES = 20 * 1024 * 1024


def clean_label(value):
    return " ".join(str(value).split())[:180]


def mail_identity(message, bodies, fallback):
    """Prefer explicit body identity, then sender name/address, then subject.

    Subject is always included independently in the caption. Stable identity keeps
    cooldown useful when firmware adds timestamps to every subject.
    """
    for body in bodies:
        match = re.search(
            r"(?im)^\s*(?:camera(?:\s*name)?|device(?:\s*name)?|title|камер(?:ын нэр)?)\s*[:=]\s*([^\r\n]+)", body)
        if match:
            return clean_label(match.group(1))
    name, address = parseaddr(str(message.get("From", "")))
    if name:
        return clean_label(name)
    subject = clean_label(message.get("Subject", ""))
    # Bracketed camera identifiers are stable even with changing alarm timestamps.
    match = re.search(r"\[([^]\r\n]+)\]", subject)
    if match:
        return clean_label(match.group(1))
    if address:
        return clean_label(address)
    return subject or clean_label(fallback)


class DeliveryError(Exception):
    pass


class Bridge:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_TOKEN", "").strip()
        self.chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.device = os.environ.get("DEVICE_NAME", "Dahua DH-IPC-HDBW1230E-S5")
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASS", "")
        self.webhook_token = os.environ.get("WEBHOOK_TOKEN", "")
        self.cooldown = float(os.environ.get("COOLDOWN_S", "15"))
        if not self.token or not self.chat:
            raise ValueError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required")
        if self.user and not self.password:
            raise ValueError("SMTP_PASS is required when SMTP_USER is set")
        if not math.isfinite(self.cooldown) or self.cooldown < 0:
            raise ValueError("COOLDOWN_S must be finite and nonnegative")
        self.lock = threading.Lock()
        self.last = {}

    def authenticate(self, server, session, envelope, mechanism, auth_data):
        valid = isinstance(auth_data, LoginPassword) and (
            not self.user or (
                hmac.compare_digest(auth_data.login, self.user.encode())
                and hmac.compare_digest(auth_data.password, self.password.encode())
            )
        )
        return AuthResult(success=valid, handled=False)

    def api(self, method, data, files=None):
        try:
            with requests.post(
                f"https://api.telegram.org/bot{self.token}/{method}",
                data={"chat_id": self.chat, **data}, files=files,
                timeout=(5, 30),
            ) as response:
                if not response.ok or not response.json().get("ok"):
                    raise DeliveryError("Telegram rejected delivery")
        except (requests.RequestException, ValueError):
            # Never log request URLs: they contain the bot token.
            raise DeliveryError("Telegram connection/response error") from None

    def deliver(self, event, detail, images=(), test=False, text=None, device=None):
        event = event.strip().lower()[:128] or "motion"
        device = clean_label(device or self.device)
        key = (device.casefold(), event)
        # Serialize delivery and cooldown admission to avoid concurrent duplicates.
        with self.lock:
            now = time.monotonic()
            if not test and now - self.last.get(key, -float("inf")) < self.cooldown:
                return False
            caption = (f"🚨 Хөдөлгөөн / төхөөрөмжийн дохио\n📷 {device}\n"
                       f"🕒 {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}\n"
                       f"📝 {detail}")[:1024]
            if not images:
                self.api("sendMessage", {"text": (caption + ("\n" + text if text else ""))[:4096]})
            else:
                for start in range(0, len(images), 10):
                    batch = images[start:start + 10]
                    if len(batch) == 1:
                        self.api("sendPhoto", {"caption": caption}, {"photo": batch[0]})
                    else:
                        media = [{"type": "photo", "media": f"attach://p{i}",
                                  **({"caption": caption} if i == 0 else {})}
                                 for i in range(len(batch))]
                        self.api("sendMediaGroup", {"media": json.dumps(media)},
                                 {f"p{i}": photo for i, photo in enumerate(batch)})
            if not test:
                # Bounded keys even if an HTTP client supplies arbitrary event types.
                self.last = {k: v for k, v in self.last.items()
                             if time.monotonic() - v < self.cooldown}
                self.last[key] = time.monotonic()
            return True

    def mail(self, raw):
        message = BytesParser(policy=policy.default).parsebytes(raw)
        subject = str(message.get("Subject", ""))
        images, bodies = [], []
        for part in message.walk():
            if part.is_multipart():
                continue
            kind = part.get_content_type()
            name = part.get_filename() or ""
            if kind in ("image/jpeg", "image/png") or (
                kind == "application/octet-stream" and name.lower().endswith((".jpg", ".png"))
            ):
                data = part.get_payload(decode=True)
                if data:
                    mime = "image/png" if kind == "image/png" or name.lower().endswith(".png") else "image/jpeg"
                    images.append(("snapshot.png" if mime == "image/png" else "snapshot.jpg", data, mime))
            elif kind == "text/plain":
                data = part.get_payload(decode=True) or b""
                try:
                    bodies.append(data.decode(part.get_content_charset() or "utf-8", errors="replace"))
                except LookupError:
                    bodies.append(data.decode("utf-8", errors="replace"))
        device = mail_identity(message, bodies, self.device)
        return self.deliver("motion", subject or "motion", images,
                            test=bool(re.search(r"\btest\b", subject, re.I)),
                            text="\n".join(bodies)[:2500] if not images else None,
                            device=device)



class MailHandler:
    def __init__(self, bridge):
        self.bridge = bridge

    async def handle_DATA(self, server, session, envelope):
        try:
            sent = await asyncio.to_thread(self.bridge.mail, envelope.original_content)
            LOG.info("SMTP %s", "delivered" if sent else "cooldown")
            return "250 Message processed"
        except Exception:
            LOG.error("SMTP delivery failed")
            return "451 Temporary delivery failure; please retry"


def http_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            pass  # Request paths can contain secret query tokens.

        def reply(self, code, payload):
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True

        def do_GET(self):
            self.process()

        def do_POST(self):
            self.process()

        def process(self):
            self.connection.settimeout(15)
            path = urlsplit(self.path)
            if path.path == "/health" and self.command == "GET":
                return self.reply(200, {"status": "ok"})
            if path.path != "/event":
                return self.reply(404, {"error": "not found"})
            query = parse_qs(path.query)
            value = lambda key, default="": query.get(key, [default])[0]
            if bridge.webhook_token and not hmac.compare_digest(
                value("token").encode(), bridge.webhook_token.encode()
            ):
                return self.reply(403, {"error": "invalid token"})
            if self.headers.get("Transfer-Encoding"):
                return self.reply(400, {"error": "use Content-Length"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply(400, {"error": "invalid Content-Length"})
            if not 0 <= length <= MAX_BYTES:
                return self.reply(413, {"error": "body too large"})
            images = []
            try:
                if self.command == "POST" and length:
                    data = self.rfile.read(length)
                    if len(data) != length:
                        return self.reply(400, {"error": "incomplete body"})
                    if data.startswith(b"\xff\xd8\xff"):
                        images = [("snapshot.jpg", data, "image/jpeg")]
                    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
                        images = [("snapshot.png", data, "image/png")]
                    elif self.headers.get_content_type() not in ("text/plain", "application/json", "application/x-www-form-urlencoded"):
                        return self.reply(415, {"error": "expected raw JPEG or PNG"})
                sent = bridge.deliver(value("type", "motion"), value("subject", value("type", "motion")),
                                      images, test=value("test") == "1",
                                      device=value("device", value("camera", bridge.device)))
                return self.reply(200, {"status": "sent" if sent else "cooldown"})
            except (TimeoutError, OSError):
                return self.reply(408, {"error": "request timeout"})
            except Exception:
                LOG.error("HTTP delivery failed")
                return self.reply(502, {"error": "delivery failed; retry later"})
    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("mail.log").setLevel(logging.ERROR)
    bridge = Bridge()
    controller = Controller(MailHandler(bridge), hostname="0.0.0.0", port=2525,
                            data_size_limit=MAX_BYTES, auth_required=bool(bridge.user),
                            auth_require_tls=False, authenticator=bridge.authenticate)
    server = ThreadingHTTPServer(("0.0.0.0", 8090), http_handler(bridge))
    server.daemon_threads = True
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        controller.start()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        LOG.info("Listening SMTP :2525 and HTTP :8090; timestamps in Ulaanbaatar (UTC+8)")
        stopped.wait()
    finally:
        if 'worker' in locals():
            server.shutdown()
        server.server_close()
        controller.stop()


if __name__ == "__main__":
    main()
