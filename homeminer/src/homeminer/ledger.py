"""Earnings ledger that keeps four different things apart.

  estimated            what the profitability engine expects (a calculation)
  unpaid_balance       what the pool/marketplace shows as accrued but not yet paid
  confirmed_pool       what the pool/marketplace has paid into your account there
  received_in_wallet   BTC that has actually arrived in your own wallet

HomeMiner never reads your wallet or your NiceHash account. You enter the last
three from the marketplace dashboard and your wallet app. The ledger lives in
the data folder, outside the repository, and is never logged.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

KINDS = {
    "estimated": "Estimated (calculation, not money)",
    "unpaid_balance": "Unpaid balance shown by the marketplace",
    "confirmed_pool": "Paid into your marketplace account",
    "received_in_wallet": "Received in your own wallet",
}


@dataclass
class Entry:
    kind: str
    amount_btc: str
    as_of: str
    source: str
    note: str = ""


def _btc(value) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.00000001"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Not a BTC amount: {value!r}") from exc
    if amount < 0:
        raise ValueError("Amounts cannot be negative.")
    if amount > Decimal("21000000"):
        raise ValueError("Amount exceeds the BTC supply.")
    return amount


class Ledger:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "ledger.json"

    def entries(self) -> list[Entry]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [Entry(**e) for e in raw if e.get("kind") in KINDS]

    def add(self, kind: str, amount_btc, source: str, note: str = "", as_of: str | None = None) -> Entry:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        if kind != "estimated" and not source.strip():
            raise ValueError("Say where the figure came from (e.g. 'NiceHash dashboard').")
        entry = Entry(kind, format(_btc(amount_btc), "f"), as_of or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      source.strip()[:200], note.strip()[:500])
        entries = self.entries() + [entry]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(e) for e in entries], indent=2), encoding="utf-8")
        return entry

    def summary(self) -> dict:
        """Latest snapshot per kind. Kinds are never added together."""
        latest: dict[str, Entry] = {}
        for entry in self.entries():
            if entry.kind not in latest or entry.as_of >= latest[entry.kind].as_of:
                latest[entry.kind] = entry
        return {
            kind: {
                "label": label,
                "amount_btc": latest[kind].amount_btc if kind in latest else None,
                "as_of": latest[kind].as_of if kind in latest else None,
                "source": latest[kind].source if kind in latest else None,
            }
            for kind, label in KINDS.items()
        }
