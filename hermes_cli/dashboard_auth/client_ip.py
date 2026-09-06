"""Trusted-proxy-aware client IP resolution (batch94, task11 D1).

The dashboard auth stack previously trusted ``X-Forwarded-For``
unconditionally in three duplicated ``_client_ip`` helpers. A client can
set that header to anything, so the password-login rate limiter keyed on
it was bypassable by rotating the header value.

Rule: the socket peer is the client, UNLESS the peer itself is a trusted
proxy — loopback by default (the dashboard's default bind is loopback,
with a same-host reverse proxy being the common fronting setup), plus any
CIDR listed in ``security.trusted_proxies`` (config.yaml). Only then is
the XFF header honored, and the RIGHTMOST entry is taken: our nearest
trusted proxy appended it, so a client-supplied forgery always sits to
the left of it.

One-proxy assumption: with deeper trusted chains the rightmost entry is
the next proxy's address, which collapses clients into one limiter
bucket — fail-safe toward throttling, never toward bypass.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import List

_log = logging.getLogger(__name__)

_LOOPBACK_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def _extra_trusted_nets() -> List[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """``security.trusted_proxies`` config entries → networks (invalid skipped)."""
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly().get("security", {}) or {}).get(
            "trusted_proxies"
        )
    except Exception:
        return []
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in raw:
        try:
            out.append(ipaddress.ip_network(str(entry), strict=False))
        except ValueError:
            _log.warning(
                "dashboard-auth: ignoring invalid security.trusted_proxies "
                "entry %r (expected CIDR, e.g. 10.0.0.0/8)",
                entry,
            )
    return out


def _peer_is_trusted_proxy(peer: str) -> bool:
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        # Non-IP peer ("testclient" in TestClient, unix socket placeholders)
        # is never a trusted proxy — fail closed to the peer itself.
        return False
    # Dual-stack sockets may surface v4 peers as v4-mapped v6 addresses.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return any(addr in net for net in (*_LOOPBACK_NETS, *_extra_trusted_nets()))


def client_ip(request) -> str:
    """Client IP for rate limiting / audit: peer, or XFF origin behind a
    trusted proxy only. Never trusts XFF from an untrusted (e.g. direct
    internet) peer — that is the D1 bypass fix."""
    peer = request.client.host if request.client else ""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd and _peer_is_trusted_proxy(peer):
        return fwd.split(",")[-1].strip()
    return peer
