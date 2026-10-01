"""Optional HTTPS for the dashboard: a given certificate, or a self-signed one made by openssl."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from private_file import WINDOWS, restrict_to_owner

_VALID_DAYS = 825
# a pair this close to expiry is replaced before any client starts refusing it
_RENEW_BEFORE = 30 * 86400
_DNS_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")


class TLSError(Exception):
    pass


def subject_alt_names(addresses: list[str], bound: str) -> str:
    """SAN entries for every name the dashboard answers on; anything malformed is dropped."""
    names = {"localhost"}
    hostname = socket.gethostname()
    if _DNS_NAME.fullmatch(hostname):
        names.update((hostname.casefold(), f"{hostname.casefold()}.local"))
    ips = {"127.0.0.1", "::1"}
    # addresses may also carry host names (declared origins): each value is an IP or a DNS name
    for value in (*addresses, bound):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            if _DNS_NAME.fullmatch(value) and not value.replace(".", "").isdigit():
                names.add(value.casefold())
            continue
        # a zoned address (fe80::1%eth0) is no valid SAN and would make openssl fail
        if not address.is_unspecified and getattr(address, "scope_id", None) is None:
            ips.add(str(address))
    return ",".join([*(f"DNS:{name}" for name in sorted(names)), *(f"IP:{ip}" for ip in sorted(ips))])


def _reusable(info_file: Path, wanted: str) -> bool:
    """The saved pair covers every wanted name and stays valid for a while yet."""
    try:
        info = json.loads(info_file.read_text(encoding="ascii"))
        saved, not_after = set(info["names"]), float(info["not_after"])
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return False
    return set(wanted.split(",")) <= saved and not_after - time.time() > _RENEW_BEFORE


def self_signed_pair(directory: Path, addresses: list[str], bound: str) -> tuple[Path, Path]:
    """Return the saved pair while it covers every current name, else make a new one.

    The key is owner-only before it holds anything.
    """
    cert, key, info_file = directory / "cert.pem", directory / "key.pem", directory / "pair.json"
    names = subject_alt_names(addresses, bound)
    if cert.is_file() and key.is_file() and _reusable(info_file, names):
        return cert, key
    openssl = shutil.which("openssl")
    if openssl is None:
        raise TLSError("openssl not found: install it, or pass --dashboard-cert and --dashboard-key")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdtemp is owner-only, so the key is never readable by others while openssl writes it
        with tempfile.TemporaryDirectory(dir=directory, prefix=".new-") as staging:
            staged_key, staged_cert = Path(staging, "key.pem"), Path(staging, "cert.pem")
            staged_key.touch(mode=0o600)
            restrict_to_owner(staged_key)
            result = subprocess.run(
                [openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                 "-nodes", "-keyout", str(staged_key), "-out", str(staged_cert), "-days", str(_VALID_DAYS),
                 "-subj", "/CN=TwitchDropsMiner dashboard",
                 "-addext", "subjectAltName=" + names,
                 "-addext", "basicConstraints=critical,CA:FALSE",
                 "-addext", "extendedKeyUsage=serverAuth"],
                capture_output=True, timeout=60, check=False,
            )
            if result.returncode != 0 or not staged_cert.is_file():
                raise TLSError(f"openssl could not create the dashboard certificate (exit {result.returncode})")
            os.chmod(staged_key, 0o600)
            staged_info = Path(staging, "pair.json")
            # one hour of margin: openssl starts the validity a moment before this point
            staged_info.write_text(json.dumps({
                "names": names.split(","), "not_after": int(time.time()) + _VALID_DAYS * 86400 - 3600,
            }), encoding="ascii")
            # key, then certificate, then its info: a pair only counts once all three match
            info_file.unlink(missing_ok=True)
            os.replace(staged_key, key)
            os.replace(staged_cert, cert)
            os.replace(staged_info, info_file)
    except subprocess.TimeoutExpired as exc:
        raise TLSError("openssl timed out creating the dashboard certificate") from exc
    except OSError as exc:
        raise TLSError(f"cannot create the dashboard certificate in {directory}: {exc.strerror or exc}") from exc
    return cert, key


def _check_key(key: Path) -> None:
    # Windows ACLs are not inspected here; the dashboard warns at startup instead
    try:
        info = key.stat()
    except OSError as exc:
        raise TLSError(f"Dashboard TLS key is inaccessible: {key}: {exc.strerror}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise TLSError(f"Dashboard TLS key is not a regular file: {key}")
    if not WINDOWS and stat.S_IMODE(info.st_mode) & 0o077:
        raise TLSError(f"Dashboard TLS key is readable by other users; run: chmod 600 {key}")


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    _check_key(key)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as exc:
        raise TLSError(f"cannot load the dashboard certificate {cert}: {exc}") from exc
    return context


def fingerprint(cert: Path) -> str:
    """SHA-256 of the certificate, as browsers show it, to compare on the warning page."""
    try:
        text = cert.read_text(encoding="ascii")
        # a full chain starts with the server certificate, which is the one browsers show
        match = re.search(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", text, re.DOTALL)
        if match is None:
            raise ValueError("no certificate found")
        der = ssl.PEM_cert_to_DER_cert(match.group(0))
    except (OSError, ValueError, UnicodeError) as exc:
        raise TLSError(f"cannot read the dashboard certificate {cert}: {exc}") from exc
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[index:index + 2] for index in range(0, len(digest), 2))
