"""BTC address validation and handling.

HomeMiner only ever needs a *receiving* address, and only when a payout route
actually requires one. It never asks for, accepts or stores seed phrases,
private keys, extended private keys or exchange passwords, and it never
initiates a transaction.

Address checks follow BIP-173 (bech32), BIP-350 (bech32m) and Base58Check.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# --- bech32 / bech32m (BIP-173, BIP-350) ------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3

_HRP_NETWORK = {"bc": "mainnet", "tb": "testnet", "bcrt": "regtest"}


def _polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_decode(bech: str) -> tuple[str | None, list[int] | None, str | None]:
    """Return (hrp, data, encoding) or (None, None, None) if invalid."""
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        return None, None, None
    if bech.lower() != bech and bech.upper() != bech:
        return None, None, None
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech) or len(bech) > 90:
        return None, None, None
    if not all(x in _CHARSET for x in bech[pos + 1:]):
        return None, None, None
    hrp = bech[:pos]
    data = [_CHARSET.find(x) for x in bech[pos + 1:]]
    const = _polymod(_hrp_expand(hrp) + data)
    if const == _BECH32_CONST:
        encoding = "bech32"
    elif const == _BECH32M_CONST:
        encoding = "bech32m"
    else:
        return None, None, None
    return hrp, data[:-6], encoding


def _convertbits(data: list[int], frombits: int, tobits: int, pad: bool) -> list[int] | None:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def decode_segwit(addr: str) -> tuple[str, int, bytes] | None:
    """Return (hrp, witness_version, program) for a valid segwit address."""
    hrp, data, encoding = _bech32_decode(addr)
    if hrp is None or not data:
        return None
    if hrp not in _HRP_NETWORK:
        return None
    version = data[0]
    program = _convertbits(data[1:], 5, 8, False)
    if program is None or len(program) < 2 or len(program) > 40:
        return None
    if version > 16:
        return None
    if version == 0 and len(program) not in (20, 32):
        return None
    if version == 0 and encoding != "bech32":
        return None
    if version != 0 and encoding != "bech32m":
        return None
    return hrp, version, bytes(program)


# --- Base58Check -------------------------------------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode_check(s: str) -> bytes | None:
    if not s or any(c not in _B58 for c in s):
        return None
    num = 0
    for c in s:
        num = num * 58 + _B58.index(c)
    full = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))
    full = b"\x00" * pad + full
    if len(full) < 5:
        return None
    payload, checksum = full[:-4], full[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        return None
    return payload


_BASE58_VERSIONS = {
    0x00: ("mainnet", "P2PKH (legacy, starts with 1)"),
    0x05: ("mainnet", "P2SH (starts with 3)"),
    0x6F: ("testnet", "P2PKH (testnet)"),
    0xC4: ("testnet", "P2SH (testnet)"),
}


# --- Public API --------------------------------------------------------------

@dataclass
class AddressCheck:
    valid: bool
    network: str | None = None
    kind: str | None = None
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_WORD_RE = re.compile(r"^[a-z]{3,8}$")


def _secret_kind(s: str) -> str | None:
    """Name the kind of wallet *secret* ``s`` looks like, else None."""
    words = s.split()
    if len(words) in (12, 15, 18, 21, 24) and all(_WORD_RE.match(w) for w in words):
        return "seed phrase"
    if re.fullmatch(r"[0-9a-fA-F]{64}", s):
        return "raw private key or hash"
    if s[:4] in ("xprv", "yprv", "zprv", "tprv", "uprv", "vprv"):
        return "extended private key"
    payload = b58decode_check(s) if 50 <= len(s) <= 53 else None
    if payload is not None and payload[:1] in (b"\x80", b"\xef") and len(payload) in (33, 34):
        return "WIF private key"
    return None


def classify_sensitive(text: str) -> str | None:
    """Name the kind of wallet secret or identifier ``text`` looks like, else None.

    Used to refuse such values in configuration files and to warn the user
    before anything sensitive is pasted into the UI.
    """
    s = text.strip()
    if not s:
        return None
    secret = _secret_kind(s)
    if secret:
        return secret
    if s[:4] in ("xpub", "ypub", "zpub", "tpub", "upub", "vpub"):
        return "extended public key"
    if s.lower().startswith(("lnbc", "lntb", "lnurl")):
        return "Lightning invoice or LNURL"
    if _check_format(s).valid:
        return "BTC address"
    return None


def _check_format(s: str) -> AddressCheck:
    seg = decode_segwit(s)
    if seg is not None:
        hrp, version, program = seg
        network = _HRP_NETWORK[hrp]
        if version == 0:
            kind = "P2WPKH (native SegWit, bc1q…)" if len(program) == 20 else "P2WSH (native SegWit script)"
        elif version == 1 and len(program) == 32:
            kind = "P2TR (Taproot, bc1p…)"
        else:
            kind = f"SegWit v{version} (future version)"
        return AddressCheck(True, network=network, kind=kind)
    if s.lower().startswith(("bc1", "tb1", "bcrt1")):
        return AddressCheck(False, reason="Invalid bech32/bech32m checksum or length. Check for a typo.")
    payload = b58decode_check(s)
    if payload is None:
        if all(c in _B58 for c in s) and 25 <= len(s) <= 35:
            return AddressCheck(False, reason="Invalid Base58Check checksum. Check for a typo.")
        return AddressCheck(False, reason="Not a recognised BTC address format.")
    if len(payload) != 21 or payload[0] not in _BASE58_VERSIONS:
        return AddressCheck(False, reason="Base58 string is not a BTC address.")
    network, kind = _BASE58_VERSIONS[payload[0]]
    return AddressCheck(True, network=network, kind=kind)


def validate_btc_address(text: str, expected_network: str | None = "mainnet") -> AddressCheck:
    """Validate a BTC receiving address's format, checksum and network.

    A valid format does not prove the address belongs to you or that a
    service accepts it; that is stated in the warnings.
    """
    s = (text or "").strip()
    if not s:
        return AddressCheck(False, reason="No address entered.")
    secret = _secret_kind(s)
    if secret:
        return AddressCheck(
            False,
            reason=f"This looks like a {secret}. Never share it. HomeMiner only accepts a receiving address.",
        )
    check = _check_format(s)
    if not check.valid:
        return check
    if expected_network and check.network != expected_network:
        return AddressCheck(
            False,
            network=check.network,
            kind=check.kind,
            reason=f"This is a {check.network} address; payouts need a {expected_network} address.",
        )
    if check.kind and "future version" in check.kind:
        check.warnings.append("Future SegWit versions are not spendable by today's wallets. Do not use it.")
    if check.kind and check.kind.startswith("P2TR"):
        check.warnings.append("Not every service sends to Taproot addresses. Check the route's requirements.")
    check.warnings.append(
        "Format and checksum are valid. That does not prove the address is yours: "
        "confirm it in your own wallet app before using it."
    )
    return check


def mask(address: str) -> str:
    s = (address or "").strip()
    if len(s) <= 10:
        return "…"
    return f"{s[:4]}…{s[-4:]}"


ROUTE_ADDRESS_REQUIREMENTS = {
    "nicehash": (
        "Mining needs your NiceHash *mining address* (shown in your NiceHash wallet). It is a "
        "NiceHash account identifier, not your own wallet. Withdrawals to your own wallet are set up "
        "on nicehash.com and need a NiceHash account; users report identity (KYC) checks. HomeMiner "
        "never asks for your NiceHash password and never withdraws for you."
    ),
    "solo": (
        "Educational only. Solo mining would pay a block reward to an address in your own wallet, "
        "but at this PC's hash rate the expected wait is millions of years. No address is collected."
    ),
    "benchmark": "Benchmarks run offline and need no address or account.",
}


def route_needs_address(route: str) -> bool:
    return route == "nicehash"


class PayoutStore:
    """Keeps a payout address outside the repository, and only if the user asks.

    The address is written to the data folder (by default %LOCALAPPDATA%\\HomeMiner),
    never to the project folder, logs or diagnostic reports.
    """

    def __init__(self, data_dir: Path):
        self.path = data_dir / "payout.json"

    def save(self, route: str, address: str) -> AddressCheck:
        if not route_needs_address(route):
            raise ValueError(f"Route '{route}' does not need an address.")
        check = validate_btc_address(address)
        if not check.valid:
            return check
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._read()
        data[route] = address.strip()
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return check

    def load(self, route: str) -> str | None:
        return self._read().get(route)

    def forget(self, route: str) -> None:
        data = self._read()
        if route in data:
            del data[route]
            self.path.write_text(json.dumps(data), encoding="utf-8")

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
