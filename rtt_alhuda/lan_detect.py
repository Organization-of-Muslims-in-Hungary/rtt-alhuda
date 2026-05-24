"""Best-effort LAN IPv4 for same-network links (e.g. QR codes). Not a security boundary."""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import sys


def _is_private_rfc1918_ipv4(ip: str) -> bool:
    try:
        parts = [int(x) for x in ip.split(".")]
    except ValueError:
        return False
    if len(parts) != 4 or any(x < 0 or x > 255 for x in parts):
        return False
    a, b, _, _ = parts
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    return False


def _is_link_local_169(ip: str) -> bool:
    return ip.startswith("169.254.")


def _looks_like_ipv4(ip: str) -> bool:
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip.strip()))


def _iter_hostname_i_linux() -> list[str]:
    """Raspberry Pi OS / Debian: `hostname -I` lists non-loopback IPv4/IPv6; we keep IPv4."""

    hi = shutil.which("hostname")
    if not hi or sys.platform in ("win32", "cygwin"):
        return []
    try:
        proc = subprocess.run(
            [hi, "-I"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return []
    return [t for t in proc.stdout.split() if t and _looks_like_ipv4(t)]


def _udp_route_guess() -> str | None:
    """Address chosen for outbound UDP (often the active LAN/Wi‑Fi interface)."""

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None
    if ip and not ip.startswith(("127.", "0.")):
        return ip
    return None


def _getaddrinfo_names() -> list[str]:
    out: list[str] = []
    names: list[str] = []
    try:
        hn = socket.gethostname()
        if hn:
            names.append(hn)
    except OSError:
        pass
    try:
        fq = socket.getfqdn()
        if fq and fq not in names:
            names.append(fq)
    except OSError:
        pass
    for name in names:
        try:
            for fam, _, _, _, sockaddr in socket.getaddrinfo(name, None):
                if fam != socket.AF_INET:
                    continue
                addr = sockaddr[0]
                if addr and not addr.startswith("127."):
                    out.append(addr)
        except OSError:
            continue
    return out


def pick_preferred_lan_ipv4(candidates: list[str]) -> str | None:
    """Pick one IPv4 from candidates: prefer RFC1918, then non–link-local, else first."""

    uniq: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        s = raw.strip()
        if not s or not _looks_like_ipv4(s) or s.startswith("127.") or s in seen:
            continue
        seen.add(s)
        uniq.append(s)

    priv = [x for x in uniq if _is_private_rfc1918_ipv4(x)]
    if priv:
        return priv[0]
    non_ll = [x for x in uniq if not _is_link_local_169(x)]
    if non_ll:
        return non_ll[0]
    return uniq[0] if uniq else None


def detect_lan_ipv4() -> str | None:
    """Return a likely LAN IPv4 (works well on Raspberry Pi OS + typical Wi‑Fi), or None."""

    candidates: list[str] = []

    u = _udp_route_guess()
    if u:
        candidates.append(u)

    # Raspberry Pi OS / Debian: stable ordering of interface addresses.
    candidates.extend(_iter_hostname_i_linux())

    candidates.extend(_getaddrinfo_names())

    return pick_preferred_lan_ipv4(candidates)
