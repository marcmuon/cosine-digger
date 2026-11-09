"""The iPhone and iPad companion: a pairing token, and the addresses a phone can reach.

A phone can't reach Digger at 127.0.0.1: on the phone, that is the phone. `serve --companion`
listens on every interface and lets a client from another device in only with the token saved
here. Clients on this computer (the browser at 127.0.0.1) keep the Host and Origin rules they
have today; the token is what authenticates a phone, since Host/Origin checks never did.
"""
from __future__ import annotations

import functools
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
from urllib.parse import urlencode

TOKEN_FILE = "companion_token"
ENV = "COSINE_DIGGER_COMPANION"
SCHEME = "cosinedigger"
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
# Where the Mac app keeps its command-line tool when `tailscale` isn't on the PATH.
TAILSCALE_APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
_TAILNET_NAME = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
LOOPBACK_CLIENTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

# What a paired phone may do: search, dig, judge and listen, and send a dig to its own private
# YouTube Music playlist. It follows that send by its job id, which is random, so it reads only a
# job it was given. It may ask this computer to log in to YouTube Music from this computer's
# Chrome: the cookies are read and kept here, and the answer carries only the login's state.
# Keys, pasted logins (cookies over the network), settings changes, library scans, feeds and
# other exports stay on the computer.
REMOTE_ROUTES = tuple((method, re.compile(path)) for method, path in (
    ("GET", r"/api/health"), ("GET", r"/api/companion"), ("GET", r"/api/settings"),
    ("GET", r"/api/search"),
    ("GET", r"/api/digs"), ("POST", r"/api/digs"), ("GET", r"/api/digs/[^/]+"),
    ("POST", r"/api/digs/[^/]+/export"),
    ("GET", r"/api/steps/[^/]+"), ("POST", r"/api/steps"),
    ("PUT", r"/api/judgments/.+"), ("DELETE", r"/api/judgments/.+"), ("POST", r"/api/heard"),
    ("GET", r"/api/jobs/sl-[^/]+"), ("GET", r"/api/jobs/sl-[^/]+/events"), ("GET", r"/api/jobs/[0-9a-f]{32}"),
    ("POST", r"/api/youtube/login/chrome"),
))


def enabled(environ=None) -> bool:
    return (os.environ if environ is None else environ).get(ENV, "").strip() == "1"


def token_path(home: Path) -> Path:
    return Path(home).expanduser() / TOKEN_FILE


def load_token(home: Path, *, rotate: bool = False) -> str:
    """The saved pairing token, made on first use (or again with ``rotate``), mode 0600."""
    path = token_path(home)
    if not rotate:
        try:
            token = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = ""
        if TOKEN.fullmatch(token):
            return token
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.replace(partial, path)
    return token


def presented(headers) -> str | None:
    """The bearer credential a request carries, if any."""
    scheme, _, credential = headers.get("authorization", "").partition(" ")
    credential = credential.strip()
    return credential if scheme.lower() == "bearer" and credential else None


def is_loopback(client_host: str) -> bool:
    return client_host in LOOPBACK_CLIENTS


class Companion:
    """Companion mode for one running Digger: which token pairs a device, and what it may do.

    Made with ``at(home)``, it follows the token file, so ``cosine-digger companion --rotate``
    takes effect in a Digger that is already running. A token file that goes missing or can't be
    read pairs nothing until it is made again."""

    def __init__(self, token: str, path: Path | None = None):
        if not TOKEN.fullmatch(token):
            raise ValueError("companion token has the wrong shape")
        self._token: bytes | None = token.encode()
        self._path = path
        self._seen = self._stamp()

    @classmethod
    def at(cls, home: Path) -> Companion:
        return cls(load_token(home), token_path(home))

    def accepts(self, credential: str | None) -> bool:
        token = self._current()
        return credential is not None and token is not None and hmac.compare_digest(credential.encode(), token)

    @functools.cached_property
    def name(self) -> str:
        """This computer's name for the app, read once (scutil is slow enough to notice)."""
        return display_name()

    def _stamp(self):
        if self._path is None:
            return None
        try:
            stat = self._path.stat()
        except OSError:
            return "missing"
        return stat.st_ino, stat.st_mtime_ns, stat.st_size

    def _current(self) -> bytes | None:
        """The token now: read again whenever the file changed (one stat per request)."""
        if self._path is None:
            return self._token
        stamp = self._stamp()
        if stamp != self._seen:
            self._seen = stamp
            try:
                token = self._path.read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
            self._token = token.encode() if TOKEN.fullmatch(token) else None
        return self._token

    @staticmethod
    def allows(method: str, path: str) -> bool:
        return any(method == allowed and pattern.fullmatch(path) for allowed, pattern in REMOTE_ROUTES)


def local_hostname() -> str | None:
    """This computer's Bonjour name (``name.local``), which survives a new DHCP address."""
    if sys.platform == "darwin":
        try:
            name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                                  timeout=2, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            name = ""
        if re.fullmatch(r"[A-Za-z0-9-]{1,63}", name):
            return f"{name}.local"
    name = socket.gethostname()
    return name if name.endswith(".local") else None


def lan_addresses() -> list[str]:
    """IPv4 addresses another device on this network may reach, the default route's first."""
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1; connecting a UDP socket sends nothing
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        found += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    usable = []
    for address in dict.fromkeys(found):
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not (parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified):
            usable.append(address)
    return usable


def tailnet_name() -> str | None:
    """This computer's MagicDNS name, when Tailscale is running here: a phone on the same tailnet
    reaches it from anywhere (WireGuard encrypts the way; the token still pairs)."""
    cli = shutil.which("tailscale") or (TAILSCALE_APP_CLI if os.path.exists(TAILSCALE_APP_CLI) else None)
    if cli is None:
        return None
    try:
        result = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=3, check=False)
        status = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(status, dict) or status.get("BackendState") != "Running":
        return None
    name = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
    return name if _TAILNET_NAME.fullmatch(name) else None


def addresses(host: str, port: int) -> list[str]:
    """Base URLs a phone can try: the Bonjour name, each LAN address, then the tailnet name.
    The app tries them together and keeps the first that answers."""
    if host not in {"0.0.0.0", "", "::"}:
        bound = host.strip("[]")
        try:
            loopback = ipaddress.ip_address(bound).is_loopback
        except ValueError:
            loopback = bound == "localhost"
        # Bound to loopback, only this computer (and its iOS Simulator) can reach it.
        names = [bound] if not loopback else ["127.0.0.1"]
    else:
        names = [name for name in [local_hostname(), *lan_addresses(), tailnet_name()] if name]
    return [f"http://{f'[{name}]' if ':' in name else name}:{port}" for name in names]


def pairing_link(urls: list[str], token: str, name: str | None = None) -> str:
    query = [("url", url) for url in urls] + [("token", token)] + ([("name", name)] if name else [])
    return f"{SCHEME}://pair?{urlencode(query)}"


def display_name() -> str:
    name = local_hostname() or socket.gethostname() or "Digger"
    return name.removesuffix(".local")


def pairing_lines(host: str, port: int, token: str) -> list[str]:
    urls = addresses(host, port)
    if not urls:
        return ["Digger couldn't find a network address for this computer. Connect to Wi-Fi and try again."]
    lines = ["Pair the Digger app on your iPhone or iPad:",
             "  Open this link on the phone (AirDrop it, or send it to yourself), or copy it on this",
             "  computer and press Paste in the app:",
             f"    {pairing_link(urls, token, display_name())}",
             "  Or type it in the app:",
             f"    Address  {urls[0]}"]
    lines += [f"             {url}" for url in urls[1:]]
    lines += [f"    Token    {token}",
              "The phone and this computer need to be on the same network, or the same Tailscale tailnet",
              "(with Tailscale running here, the link includes this computer's tailnet name)."]
    return lines


def copy_to_clipboard(text: str) -> bool:
    """macOS only: with Universal Clipboard, the phone can then paste it."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True
