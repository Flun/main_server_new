"""Small OpenAI-compatible client for the Vast.ai Qwen llama.cpp server."""

from __future__ import annotations

import os
import json
import socket
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request as UrlRequest, urlopen

import httpx
from openai import OpenAI


CONFIG_PATH = Path(__file__).with_name("vast_qwen.env")


def load_config(path: Path = CONFIG_PATH) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


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
    """Route one HTTPS hostname to a resolved IP while preserving TLS SNI."""

    def __init__(self, host: str, address: str) -> None:
        self.host = host
        self.address = address
        self.transport = httpx.HTTPTransport()

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


def get_client() -> OpenAI:
    load_config()
    base_url = os.environ["OPENAI_BASE_URL"]
    host = urlparse(base_url).hostname
    if not host:
        raise RuntimeError(f"Invalid OPENAI_BASE_URL: {base_url}")
    transport = FixedDnsTransport(host, resolve_host(host))
    return OpenAI(
        base_url=base_url,
        api_key=os.environ["OPENAI_API_KEY"],
        timeout=120.0,
        http_client=httpx.Client(transport=transport, timeout=120.0),
    )


def chat(prompt: str) -> str:
    client = get_client()
    response = client.chat.completions.create(
        model=os.environ["OPENAI_MODEL"],
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
    )
    return response.choices[0].message.content or ""


if __name__ == "__main__":
    print(chat("Reply with exactly: READY"))
