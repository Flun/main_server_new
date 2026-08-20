"""Local OpenAI-compatible relay for a Cloudflare hostname blocked by local DNS."""

from __future__ import annotations

import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request as UrlRequest, urlopen

import httpx


CONFIG_PATH = Path(__file__).with_name("vast_qwen.env")
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18081
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def load_config() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def resolve_host(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except socket.gaierror:
        request = UrlRequest(
            "https://cloudflare-dns.com/dns-query"
            f"?name={quote(host)}&type=A",
            headers={"Accept": "application/dns-json"},
        )
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
        for answer in payload.get("Answer", []):
            if answer.get("type") == 1:
                return answer["data"]
        raise RuntimeError(f"No IPv4 address found for {host}")


class FixedDnsTransport(httpx.BaseTransport):
    def __init__(self, host: str, address: str) -> None:
        self.host = host
        self.address = address
        self.transport = httpx.HTTPTransport(retries=2)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != self.host:
            return self.transport.handle_request(request)
        headers = request.headers.copy()
        headers["Host"] = self.host
        routed = httpx.Request(
            request.method,
            request.url.copy_with(host=self.address),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": self.host},
        )
        return self.transport.handle_request(routed)

    def close(self) -> None:
        self.transport.close()


CONFIG = load_config()
LISTEN_HOST = CONFIG.get("PROXY_LISTEN_HOST", LISTEN_HOST)
LISTEN_PORT = int(CONFIG.get("PROXY_LISTEN_PORT", str(LISTEN_PORT)))
REQUEST_TIMEOUT = float(CONFIG.get("PROXY_REQUEST_TIMEOUT", "300"))
CONNECT_TIMEOUT = float(CONFIG.get("PROXY_CONNECT_TIMEOUT", "20"))
UPSTREAM = urlparse(CONFIG["OPENAI_BASE_URL"])
UPSTREAM_HOST = UPSTREAM.hostname or ""
UPSTREAM_ORIGIN = f"{UPSTREAM.scheme}://{UPSTREAM.netloc}"
CLIENT = httpx.Client(
    transport=FixedDnsTransport(UPSTREAM_HOST, resolve_host(UPSTREAM_HOST)),
    timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
    follow_redirects=False,
)


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self._forward()

    def _forward(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b""
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP | {"host", "content-length"}
        }
        headers["Accept-Encoding"] = "identity"
        url = f"{UPSTREAM_ORIGIN}{self.path}"

        try:
            with CLIENT.stream(self.command, url, headers=headers, content=body) as response:
                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    if key.lower() not in HOP_BY_HOP | {"content-length"}:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in response.iter_raw():
                    self.wfile.write(chunk)
                    self.wfile.flush()
                self.close_connection = True
        except Exception as exc:
            payload = json.dumps({"error": {"message": str(exc)}}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), RelayHandler)
    print(f"Vast relay listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    server.serve_forever()
