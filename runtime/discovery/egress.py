"""
egress — an SSRF guard that respects enterprise topology.

Enterprise agent targets are frequently INTERNAL (RFC-1918, corporate DNS), so a blanket
"no private IPs" rule would break the tool for its main audience. This guard therefore
*allows* private/loopback ranges by default and blocks only what is almost never a legitimate
chat target and is the classic SSRF prize: **link-local + cloud instance metadata**
(169.254.0.0/16 incl. 169.254.169.254, IPv6 fe80::/10, and metadata.google.internal).

`--allow-internal` turns the guard off entirely for the rare case of deliberately probing a
metadata-adjacent service.
"""
import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse

# Hostnames that front cloud metadata regardless of the IP they resolve to.
_METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data",
                   "metadata.goog", "metadata.azure.com"}

# Cloud metadata that is NOT link-local (so is_link_local misses it): Alibaba/Oracle 100.100.100.200,
# AWS IPv6 IMDS (fd00:ec2::254, a ULA), GCP/others sometimes front via these.
_METADATA_IPS = {"100.100.100.200", "fd00:ec2::254", "fd00:ec2:0:0:0:0:0:254"}


def _blocked_ip(ip: str) -> Optional[str]:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if str(addr) in _METADATA_IPS or (addr.version == 6 and str(addr).startswith("fd00:ec2")):
        return f"{ip} is a cloud metadata address"
    if addr.is_link_local:                       # 169.254.0.0/16, fe80::/10 (incl. 169.254.169.254)
        return f"{ip} is link-local (cloud instance metadata / SSRF surface)"
    return None


def check_egress(url: str, allow_internal: bool = False) -> Optional[str]:
    """Return a human reason to REFUSE this URL, or None to allow.

    Allows localhost + RFC-1918 (internal targets are legitimate); blocks link-local/metadata
    unless allow_internal. Best-effort DNS resolution — a hostname that resolves to a
    link-local address is blocked too."""
    if allow_internal:
        return None
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return None
    if host in _METADATA_HOSTS:
        return f"{host} is a cloud metadata endpoint"
    direct = _blocked_ip(host)                    # host is already an IP literal
    if direct:
        return direct
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            reason = _blocked_ip(sockaddr[0])
            if reason:
                return f"{host} resolves to {reason}"
    except socket.gaierror:
        return None                               # can't resolve — let the request itself fail
    return None
