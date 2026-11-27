from __future__ import annotations

import ipaddress

from starlette.requests import Request


_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr) for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _PRIVATE_NETWORKS)


def resolve_ip(request: Request) -> str:
    peer_ip = request.client.host if request.client else ""
    # Deployments intentionally trust X-Real-IP from private-network ingress peers.
    if _is_private(peer_ip):
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip
    return peer_ip
