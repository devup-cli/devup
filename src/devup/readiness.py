"""HTTP and TCP readiness probes for service startup ordering."""

from __future__ import annotations

import http.client
import socket
import urllib.parse


def normalize_tcp_target(spec: str | int) -> str:
    """Normalize a port or host:port specification into a canonical string."""
    if isinstance(spec, int):
        if not 1 <= spec <= 65535:
            raise ValueError(f"Invalid TCP port: {spec}")
        return f"127.0.0.1:{spec}"

    raw = spec.strip()
    if raw.isdigit():
        port = int(raw)
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid TCP port: {spec}")
        return f"127.0.0.1:{port}"

    parse_tcp_target(raw)
    return raw


def parse_tcp_target(spec: str) -> tuple[str, int]:
    """Parse host:port, [ipv6]:port, or bare port into (host, port)."""
    raw = spec.strip()
    if raw.isdigit():
        port = int(raw)
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid TCP port: {spec}")
        return "127.0.0.1", port

    if raw.startswith("[") and "]:" in raw:
        host_part, port_part = raw[1:].split("]:", 1)
        host = host_part.strip()
    elif ":" in raw:
        host_part, port_part = raw.rsplit(":", 1)
        host = host_part.strip() or "127.0.0.1"
    else:
        raise ValueError(f"Invalid TCP readiness target '{spec}'. Expected host:port or port.")

    if not port_part.isdigit():
        raise ValueError(f"Invalid port in TCP readiness target '{spec}'.")
    port = int(port_part)
    if not 1 <= port <= 65535:
        raise ValueError(f"Port out of range in TCP readiness target '{spec}'.")
    return host, port


def probe_tcp(spec: str, timeout: float = 0.25) -> bool:
    """Return True if a TCP connection to spec succeeds."""
    host, port = parse_tcp_target(spec)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def validate_ready_url(url: str) -> str:
    """Validate that ready_url uses http or https with a network location."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid ready_url '{url}'. Expected http:// or https:// URL.")
    return url.strip()


def probe_http(url: str, timeout: float = 0.5) -> bool:
    """Return True if an HTTP(S) GET request responds with a 2xx or 3xx status."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    conn_cls = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    conn = conn_cls(parsed.netloc, timeout=timeout)
    try:
        conn.request("GET", path, headers={"User-Agent": "devup-readiness/1.0", "Connection": "close"})
        resp = conn.getresponse()
        resp.read()
        return 200 <= resp.status < 400
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        conn.close()
