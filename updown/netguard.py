"""SSRF guard: only allow public http(s)/ws(s) endpoints.

Semua URL keluar (REST & WebSocket) divalidasi: skema harus http/https/ws/wss,
host tidak boleh localhost/loopback/private/link-local/reserved. Hasil DNS
di-cache 5 menit agar tidak membebani event loop.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from urllib.parse import urlparse

log = logging.getLogger("updown.netguard")

ALLOWED_SCHEMES = {"http", "https", "ws", "wss"}
_CACHE_TTL = 300.0
_cache: dict = {}


class UnsafeUrlError(ValueError):
    pass


def _reject_non_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        raise UnsafeUrlError(f"alamat non-publik ditolak: {ip}")


def _check_host_literal(host: str):
    h = host.lower().rstrip(".")
    if (h in ("localhost", "localhost.localdomain", "ip6-localhost")
            or h.endswith(".localhost") or h.endswith(".local")
            or h.endswith(".internal") or h.endswith(".lan")):
        raise UnsafeUrlError(f"hostname lokal ditolak: {host}")
    try:
        _reject_non_public(ipaddress.ip_address(h))
    except ValueError:
        pass  # bukan IP literal → perlu DNS


def validate_url_sync(url: str) -> str:
    p = urlparse(url)
    if p.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"skema {p.scheme!r} tidak diizinkan (http/https/ws/wss)")
    host = p.hostname
    if not host:
        raise UnsafeUrlError(f"URL tanpa host: {url}")
    now = time.time()
    hit = _cache.get(host)
    if hit and hit[1] > now:
        return url
    _check_host_literal(host)
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"DNS gagal untuk {host}: {exc}") from exc
    if not infos:
        raise UnsafeUrlError(f"DNS kosong untuk {host}")
    for info in infos:
        try:
            _reject_non_public(ipaddress.ip_address(info[4][0]))
        except ValueError:
            raise UnsafeUrlError(f"alamat tak valid dari DNS {host}: {info[4][0]}")
    _cache[host] = (True, now + _CACHE_TTL)
    return url


async def validate_url(url: str) -> str:
    """Async variant: cache-hit instan, miss → DNS di thread terpisah."""
    host = (urlparse(url).hostname or "").lower()
    hit = _cache.get(host)
    if hit and hit[1] > time.time():
        return url
    return await asyncio.to_thread(validate_url_sync, url)
