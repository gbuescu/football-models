"""Remove personal identifiers from text before it is logged or reported.

Covers BTC addresses (including NiceHash ``address.worker`` usernames), the
Windows user name in profile paths, the computer name and the login name.
"""

from __future__ import annotations

import getpass
import logging
import re
import socket

from . import wallet

_BECH32_RE = re.compile(r"\b(?:bc|tb|bcrt)1[02-9ac-hj-np-z]{6,87}\b", re.IGNORECASE)
_BASE58_RE = re.compile(r"\b[123mn][1-9A-HJ-NP-Za-km-z]{24,34}\b")
_USER_PATH_RE = re.compile(r"(?i)([a-z]:[\\/]+(?:users|documents and settings)[\\/]+)([^\\/\s\"']+)")
_HOME_PATH_RE = re.compile(r"(/home/|/Users/)([^/\s\"']+)")

ADDRESS_PLACEHOLDER = "<btc-address>"


def _local_identifiers() -> list[str]:
    names: list[str] = []
    for getter in (socket.gethostname, getpass.getuser):
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - best effort only
            continue
        if value and len(value) >= 3 and value.lower() not in {"root", "user", "admin", "localhost"}:
            names.append(value)
    return names


def _replace_address(match: re.Match) -> str:
    text = match.group(0)
    # Only redact strings that really are addresses, so hashes and ids survive.
    if wallet.validate_btc_address(text, expected_network=None).valid:
        return ADDRESS_PLACEHOLDER
    return text


def redact(text: str, extra: list[str] | None = None) -> str:
    if not text:
        return text
    out = _BECH32_RE.sub(_replace_address, text)
    out = _BASE58_RE.sub(_replace_address, out)
    out = _USER_PATH_RE.sub(r"\1<user>", out)
    out = _HOME_PATH_RE.sub(r"\1<user>", out)
    for name in [*_local_identifiers(), *(extra or [])]:
        if name:
            out = re.sub(re.escape(name), "<redacted>", out, flags=re.IGNORECASE)
    return out


def redact_obj(obj):
    """Recursively redact strings inside dicts and lists."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        record.msg = redact(message)
        record.args = ()
        return True


def configure_logging(level: int = logging.INFO, log_file=None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        handler.addFilter(RedactingFilter())
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(level)
