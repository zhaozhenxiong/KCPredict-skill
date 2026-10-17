"""Small loopback-only web interface; no original service or external CDN."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from .cli import execute, parser, state_home, status

MAX_BODY = 20 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _local(self):
        try:
            host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            if host not in ("localhost", "127.0.0.1", "::1"):
                return False
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host')}":
                return False
            return True
        except ValueError:
            return False

    def _send(self, code, value, content_type="application/json; charset=utf-8"):
        payload = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if not self._local():
            return self._send(403, {"error": "Local origin required"})
        try:
            if self.path == "/":
                return self._send(200, (Path(__file__).parent / "web/index.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/status":
                return self._send(200, status())
            return self._send(404, {"error": "Not found"})
        except Exception as exc:
            return self._send(500, {"error": str(exc)})

    def do_POST(self):
        if not self._local():
            return self._send(403, {"error": "Local origin required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                return self._send(413, {"error": "CSV / request exceeds 20 MB"})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Request JSON must be an object")
            if self.path == "/api/research":
                command = data.get("command")
                if command not in ("analyze", "predict", "run"):
                    raise ValueError("Unsupported command")
                args = [command, "--symbol", str(data.get("symbol", ""))]
                for name in ("end", "start", "lookback", "horizon", "samples", "seed", "device", "temperature", "top_p"):
                    if name in data and str(data[name]):
                        args += ["--" + name.replace("_", "-"), str(data[name])]
                return self._send(200, execute(parser().parse_args(args)))
            if self.path == "/api/import":
                from .data import Store
                kind = data.get("kind")
                if kind not in ("bars", "calendar"):
                    raise ValueError("Unknown CSV kind")
                folder = state_home() / "imports"
                folder.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", encoding="utf-8", dir=folder, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(str(data.get("csv", "")))
                try:
                    if kind == "calendar":
                        result = Store().import_calendar(temporary, verified=data.get("verified") is True)
                    else:
                        result = Store().import_csv(temporary, symbol=data.get("symbol"),
                            verified_unadjusted=data.get("verified") is True,
                            volume_unit="shares", amount_unit="CNY")
                finally:
                    temporary.unlink(missing_ok=True)
                return self._send(200, result)
            return self._send(404, {"error": "Not found"})
        except (ValueError, KeyError, TypeError, OSError, ImportError, RuntimeError, SystemExit) as exc:
            return self._send(400, {"error": str(exc)})


def serve(host="127.0.0.1", port=8130):
    if host != "localhost" and (not ipaddress.ip_address(host).is_loopback or ":" in host):
        raise ValueError("界面仅允许绑定 localhost / IPv4 回环地址")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"KCPredict http://{host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
