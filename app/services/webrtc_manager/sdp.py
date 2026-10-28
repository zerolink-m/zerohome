"""SDP hygiene for what the server hands to the client: no private addresses, optional public candidate.

aiortc gathers a host candidate per local interface (LAN, docker bridges, VPNs...), so the raw
local description exposes the whole internal topology. `sanitize_sdp` rewrites the text we send:
private candidates are dropped, private `raddr` values and the `c=`/`m=` placeholders are replaced,
and (with WEBRTC_PUBLIC_IP) a public server-reflexive candidate is added for the 1:1-forwarded port.
The addresses are always judged by the parsed `ipaddress` value, never by string prefix.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket

from aioice.candidate import candidate_foundation, candidate_priority

from app.core.config import settings

logger = logging.getLogger(__name__)

_NETWORKS_V4 = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8",        # unspecified / "this" network
    "10.0.0.0/8",
    "100.64.0.0/10",    # CGNAT
    "127.0.0.0/8",
    "169.254.0.0/16",   # link-local
    "172.16.0.0/12",
    "192.168.0.0/16",
))
_NETWORKS_V6 = tuple(ipaddress.ip_network(n) for n in (
    "::/128",
    "::1/128",
    "fc00::/7",         # unique local
    "fe80::/10",        # link-local
))

_CANDIDATE_PREFIX = "a=candidate:"
_IN_IP = re.compile(r"\bIN IP([46]) (\S+)")


def is_private_address(addr: str) -> bool:
    """True if `addr` must never be shown to a client. Unparseable values (mDNS names,
    junk) count as private: when hiding, "unknown" is not allowed through."""
    try:
        ip = ipaddress.ip_address(str(addr).strip().split("%", 1)[0])
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    nets = _NETWORKS_V4 if ip.version == 4 else _NETWORKS_V6
    return any(ip in net for net in nets)


def _valid_public_ip(value: str) -> str | None:
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if is_private_address(str(ip)):
        return None
    return str(ip)


def default_route_ip() -> str | None:
    """Local IPv4 address the kernel would use towards the internet (UDP connect sends nothing)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def _parse_candidate(line: str) -> dict | None:
    tokens = line[len(_CANDIDATE_PREFIX):].split()
    if len(tokens) < 8 or tokens[6] != "typ":
        return None
    try:
        port = int(tokens[5])
    except ValueError:
        return None
    return {"tokens": tokens, "ip": tokens[4], "port": port, "type": tokens[7], "protocol": tokens[2].lower()}


def _mask_related(tokens: list[str]) -> list[str]:
    """raddr/rport of srflx/relay candidates is the (private) base: replace by 0.0.0.0/0."""
    out = list(tokens)
    for i in range(8, len(out) - 1):
        if out[i] == "raddr" and is_private_address(out[i + 1]):
            out[i + 1] = "0.0.0.0"
            for j in range(8, len(out) - 1):
                if out[j] == "rport":
                    out[j + 1] = "0"
    return out


def _public_candidate_line(base: dict, public_ip: str) -> str:
    component = base["tokens"][1]
    foundation = candidate_foundation("srflx", "udp", public_ip)
    priority = candidate_priority(int(component), "srflx")
    return (f"{_CANDIDATE_PREFIX}{foundation} {component} udp {priority} {public_ip} {base['port']} "
            f"typ srflx raddr 0.0.0.0 rport 0")


def _split_sections(lines: list[str]) -> list[list[str]]:
    sections: list[list[str]] = [[]]
    for line in lines:
        if line.startswith("m="):
            sections.append([])
        sections[-1].append(line)
    return sections


def _rewrite_section(lines: list[str], hide: bool, public_ip: str | None, base_ip: str | None) -> list[str]:
    parsed: list[tuple[str, dict | None]] = [
        (line, _parse_candidate(line) if line.startswith(_CANDIDATE_PREFIX) else None) for line in lines
    ]
    kept: list[dict] = []
    new_lines: list[str] = []
    existing = {(c["ip"], c["port"]) for line, c in parsed if c is not None}
    injected: list[str] = []
    last_candidate_at = -1

    for line, cand in parsed:
        if line.startswith(_CANDIDATE_PREFIX):
            if cand is None:
                if hide:
                    continue
                new_lines.append(line)
                continue
            private = is_private_address(cand["ip"])
            if public_ip and cand["type"] == "host" and cand["protocol"] == "udp" and base_ip \
                    and cand["ip"] == base_ip and (public_ip, cand["port"]) not in existing:
                injected.append(_public_candidate_line(cand, public_ip))
                existing.add((public_ip, cand["port"]))
            if hide and private:
                continue
            if hide:
                cand = dict(cand, tokens=_mask_related(cand["tokens"]))
                line = _CANDIDATE_PREFIX + " ".join(cand["tokens"])
            kept.append(cand)
            new_lines.append(line)
            last_candidate_at = len(new_lines)
        elif hide and line.startswith("a=remote-candidates:"):
            continue
        else:
            new_lines.append(line)

    for extra in injected:
        parsed_extra = _parse_candidate(extra)
        assert parsed_extra is not None
        kept.append(parsed_extra)
        at = last_candidate_at if last_candidate_at >= 0 else len(new_lines)
        new_lines.insert(at, extra)
        last_candidate_at = at + 1

    if hide:
        default = kept[0] if kept else None
        connection_private = _section_connection_private(new_lines)
        rewritten: list[str] = []
        for line in new_lines:
            if line.startswith("c=IN IP"):
                addr = line.split()[-1]
                if is_private_address(addr):
                    if default is not None:
                        fam = 6 if ipaddress.ip_address(default["ip"]).version == 6 else 4
                        line = f"c=IN IP{fam} {default['ip']}"
                    else:
                        line = "c=IN IP4 0.0.0.0"
            elif line.startswith("m="):
                parts = line.split(" ")
                if connection_private:
                    parts[1] = str(default["port"]) if default is not None else "9"
                    line = " ".join(parts)
            rewritten.append(line)
        new_lines = rewritten
    return new_lines


def _section_connection_private(lines: list[str]) -> bool:
    for line in lines:
        if line.startswith("c=IN IP"):
            return is_private_address(line.split()[-1])
    return False


def _scrub_addresses(line: str) -> str:
    """Any remaining `IN IP4|IP6 <addr>` (o=, c=, a=rtcp) with a private address -> 0.0.0.0."""
    def repl(m: re.Match) -> str:
        return "IN IP4 0.0.0.0" if is_private_address(m.group(2)) else m.group(0)
    return _IN_IP.sub(repl, line)


def sanitize_sdp(sdp: str, *, hide_private: bool = True, public_ip: str = "", base_ip: str | None = None) -> str:
    """Return `sdp` safe to send to a client (see module docstring).

    `public_ip` (validated, must itself be public) adds a srflx candidate `public_ip:<port>` for the
    udp host candidate bound to `base_ip` (the interface behind the 1:1 port forwarding)."""
    public = _valid_public_ip(public_ip) if public_ip else None
    if public_ip and public is None:
        logger.warning("webrtc: WEBRTC_PUBLIC_IP %r is not a valid public address, ignored", public_ip)
    if not hide_private and public is None:
        return sdp

    lines = sdp.replace("\r\n", "\n").split("\n")
    trailing = lines and lines[-1] == ""
    if trailing:
        lines = lines[:-1]

    out: list[str] = []
    for section in _split_sections(lines):
        out.extend(_rewrite_section(section, hide_private, public, base_ip))
    if hide_private:
        out = [_scrub_addresses(line) if not line.startswith(_CANDIDATE_PREFIX) else line for line in out]
    return "\r\n".join(out) + ("\r\n" if trailing else "")


def sanitize_local_sdp(sdp: str) -> str:
    """sanitize_sdp configured from settings: the single exit point for server SDP."""
    public_ip = (settings.WEBRTC_PUBLIC_IP or "").strip()
    # a public candidate is only trustworthy when the local ports are confined to the open range
    if public_ip and not port_range_configured():
        logger.warning("webrtc: WEBRTC_PUBLIC_IP set without WEBRTC_UDP_PORT_MIN/MAX, public candidate skipped")
        public_ip = ""
    return sanitize_sdp(
        sdp,
        hide_private=settings.WEBRTC_HIDE_PRIVATE_CANDIDATES,
        public_ip=public_ip,
        base_ip=default_route_ip() if public_ip else None,
    )


def port_range_configured() -> bool:
    lo, hi = settings.WEBRTC_UDP_PORT_MIN, settings.WEBRTC_UDP_PORT_MAX
    return 0 < lo <= hi <= 65535
