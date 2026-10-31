"""SSRF prevention helpers (security audit round 2, 2026-10-29).

Public URLs supplied by users (downloader extract, recap source URLs) are
validated *before* they are handed to yt-dlp or any HTTP client:

* only http/https schemes, a hostname, and no embedded credentials;
* IP-literal hosts are checked directly;
* DNS hostnames are resolved with ``getaddrinfo`` and *every* resolved
  address is checked -- a hostname that resolves to even one private
  address is rejected, so a mixed public/private answer cannot slip
  through;
* redirect targets are validated the same way, hop by hop, by
  ``follow_redirects_safely`` (used by ``unshorten_url``).

Blocked address classes: private, loopback, link-local, multicast,
reserved, unspecified, and CGNAT/shared space (anything that is not a
globally routable unicast address).

DNS-rebinding limitation (stated honestly): validation resolves and
checks immediately before use, and the recap background job re-validates
just before downloading, but a hostile DNS record could still change
between the last check and yt-dlp's own fetch -- yt-dlp has no
pinned-IP fetch mode in this codebase. The window is therefore narrowed
to seconds, not eliminated. A fully pinned fetch would require
downloading the source through a controlled HTTP client first and
passing yt-dlp a local file, which is out of scope for this patch.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse, urljoin

import requests

MAX_REDIRECTS = 5
REDIRECT_TIMEOUT = 10


class SSRFBlockedError(ValueError):
    """Raised when a URL is unsafe (bad scheme, unresolvable, or private)."""


def is_ip_blocked(ip_str: str) -> bool:
    """True for any non-globally-routable IP address."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if ip.is_multicast or ip.is_loopback or ip.is_link_local:
        return True
    if ip.is_reserved or ip.is_unspecified:
        return True
    if ip.is_private:
        return True
    # ipaddress.is_global is False for CGNAT (100.64/10) and other
    # special ranges even where is_private is not set on some versions.
    return not ip.is_global


def validate_public_url(url: str) -> str:
    """Validate *url* as a public http(s) URL; return it unchanged.

    Raises SSRFBlockedError on scheme, host, credential, DNS, or
    private-address problems. Callers map this to HTTP 400.
    """
    if not url or not isinstance(url, str):
        raise SSRFBlockedError("empty URL")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise SSRFBlockedError("only http and https URLs are allowed")
    if not parsed.hostname:
        raise SSRFBlockedError("URL has no hostname")
    if parsed.username or parsed.password:
        raise SSRFBlockedError("URLs with embedded credentials are not allowed")
    host = parsed.hostname
    # IP literal: check directly, no DNS needed.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if is_ip_blocked(host):
            raise SSRFBlockedError("URL resolves to a private/internal address")
        return url.strip()
    # DNS hostname: resolve and check every answer.
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError) as exc:
        raise SSRFBlockedError(f"hostname could not be resolved: {exc}") from exc
    if not infos:
        raise SSRFBlockedError("hostname could not be resolved")
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        if is_ip_blocked(str(sockaddr[0])):
            raise SSRFBlockedError("URL resolves to a private/internal address")
    return url.strip()


def follow_redirects_safely(url: str, max_redirects: int = MAX_REDIRECTS) -> str:
    """Follow redirects manually, validating every hop as public.

    A HEAD request is tried first (no body downloaded); a 405/empty
    response falls back to a streamed GET. The final, validated URL is
    returned. Any blocked hop raises SSRFBlockedError.
    """
    current = validate_public_url(url)
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; VoiceClonePro/1.0)"}
    for _ in range(max_redirects + 1):
        try:
            resp = session.head(current, headers=headers, allow_redirects=False, timeout=REDIRECT_TIMEOUT)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    return current
                current = validate_public_url(urljoin(current, location))
                continue
            return current
        except SSRFBlockedError:
            raise
        except requests.RequestException:
            # HEAD unsupported / network hiccup: try a streamed GET once.
            try:
                resp = session.get(current, headers=headers, allow_redirects=False, stream=True, timeout=REDIRECT_TIMEOUT)
                try:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            return current
                        current = validate_public_url(urljoin(current, location))
                        continue
                    return current
                finally:
                    resp.close()
            except SSRFBlockedError:
                raise
            except requests.RequestException:
                # Resolution failed for a non-redirect hop: keep the
                # validated URL and let the caller's downloader report
                # its own error, rather than masking it as SSRF.
                return current
    raise SSRFBlockedError("too many redirects")


# ---------------------------------------------------------------------------
# Telegram link shape (tutorial / invite links)
# ---------------------------------------------------------------------------

# The three t.me shapes the product uses: a channel/group name, a name
# plus a post id, and a +invite hash. Shared by every surface that
# ACCEPTS a Telegram link for storage -- the Mini App admin settings
# (deep-hunt #4 B1: a malformed tutorial link saved there later broke
# the bot's main menu for every user, because Telegram rejects a
# message whose inline keyboard carries a bad URL) and the bot's own
# tutorial-URL receivers (hunt #5 parity: they used to accept any
# http(s) string, so the bot path could store exactly the junk the
# webapp path was fixed to reject).
TELEGRAM_LINK_RE = re.compile(
    r"^https://t\.me/(?:\+[A-Za-z0-9_-]+|[A-Za-z0-9_]+(?:/\d+)?)$"
)


def is_valid_telegram_link(url: str) -> bool:
    """True when ``url`` is one of the accepted t.me link shapes."""
    return bool(TELEGRAM_LINK_RE.match((url or "").strip()))
