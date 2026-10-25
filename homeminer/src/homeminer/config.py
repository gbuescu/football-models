"""Configuration loading.

Order of precedence, lowest first:
  1. DEFAULTS below (mirrors config/homeminer.sample.toml)
  2. config/homeminer.toml in the project folder, if present
  3. settings.json in the data folder (values edited from the UI)

A configuration that appears to contain a wallet address, seed phrase, private
key or password is refused, so such values can never end up in a file that
might be committed or shared.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import wallet

DEFAULTS: dict[str, Any] = {
    "general": {
        "data_dir": "",
        "tools_dir": "tools",
        "allow_elevated": False,
    },
    "economics": {
        "electricity_eur_per_kwh": 0.00,
        "wear_cost_eur_per_day": 0.00,
        "withdrawal_fee_target_pct": 5.0,
    },
    "limits": {
        "cpu_max_threads_hint": 50,
        "cpu_priority": 0,
        "gpu_temp_limit_c": 80,
        "cpu_temp_limit_c": 90,
        "huge_pages": True,
        "max_benchmark_minutes": 45,
    },
    "benchmark": {
        "cpu_algorithm": "rx/0",
        "cpu_size": "1M",
        "gpu_algorithm": "rx/0",
        "gpu_size": "250K",
        "sample_interval_s": 2.0,
        "allow_simultaneous": False,
    },
    "power": {
        "cpu_idle_watts": 10,
        "cpu_full_load_watts": 65,
        "gpu_board_power_watts": 145,
        "wall_meter_cpu_watts": 0,
        "wall_meter_gpu_watts": 0,
    },
    "market": {
        "max_quote_age_minutes": 30,
        "timeout_s": 15,
        "nicehash_api": "https://api2.nicehash.com",
        "mempool_api": "https://mempool.space/api",
        "blockchain_info_api": "https://blockchain.info",
        "coingecko_api": "https://api.coingecko.com/api/v3",
    },
    "fees": {
        "nicehash": {
            "verified_on": "2026-10-25",
            "verification": "secondary",
            "mining_fee_pct": 2.0,
            "payout_min_btc": 0.00001,
            "payout_interval_hours": 4,
            "withdrawal_fee_btc_onchain": 0.0001,
            "withdrawal_min_btc_onchain": 0.0002,
            "inactive_funds_days": 60,
            "source": "https://www.nicehash.com/support/mining-help/earnings-and-payments/when-and-how-do-you-get-paid",
        },
    },
    "miners": {
        "xmrig": {
            "version": "6.26.0",
            "asset": "xmrig-6.26.0-windows-x64.zip",
            "release_url": "https://github.com/xmrig/xmrig/releases/tag/v6.26.0",
            "download_base": "https://github.com/xmrig/xmrig/releases/download/v6.26.0",
            "donate_level_pct": 1,
        },
        "xmrig_cuda": {
            "version": "6.22.1",
            "asset": "xmrig-cuda-6.22.1-cuda12_9-win64.zip",
            "release_url": "https://github.com/xmrig/xmrig-cuda/releases/tag/v6.22.1",
            "download_base": "https://github.com/xmrig/xmrig-cuda/releases/download/v6.22.1",
        },
    },
    "mining": {
        "approved": False,
        "route": "nicehash",
        "nicehash_stratum": "stratum+ssl://randomxmonero.auto.nicehash.com:443",
        "worker_name": "homeminer",
    },
}

# Settings the UI may change and persist to settings.json.
UI_EDITABLE = {
    ("economics", "electricity_eur_per_kwh"),
    ("economics", "wear_cost_eur_per_day"),
    ("economics", "withdrawal_fee_target_pct"),
    ("power", "wall_meter_cpu_watts"),
    ("power", "wall_meter_gpu_watts"),
    ("fees", "nicehash", "mining_fee_pct"),
    ("fees", "nicehash", "withdrawal_fee_btc_onchain"),
    ("fees", "nicehash", "withdrawal_min_btc_onchain"),
    ("fees", "nicehash", "payout_min_btc"),
    ("fees", "nicehash", "verification"),
    ("fees", "nicehash", "verified_on"),
}

FORBIDDEN_KEYS = {
    "seed", "seed_phrase", "mnemonic", "private_key", "privkey", "wif", "xprv",
    "password", "passphrase", "api_secret", "api_key", "secret", "address",
    "wallet", "wallet_address", "btc_address", "username", "email",
}


class ConfigError(ValueError):
    pass


def project_root() -> Path:
    override = os.environ.get("HOMEMINER_HOME")
    if override:
        return Path(override)
    # src/homeminer/config.py -> project folder
    return Path(__file__).resolve().parents[2]


def default_data_dir() -> Path:
    override = os.environ.get("HOMEMINER_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "HomeMiner"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "homeminer"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def check_no_secrets(data: Any, path: str = "") -> None:
    """Raise ConfigError if a config tree contains a secret or wallet identifier."""
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ConfigError(
                    f"Configuration key '{path}{key}' is not allowed. HomeMiner never "
                    "stores wallet addresses, keys, seeds or passwords in config files."
                )
            check_no_secrets(value, f"{path}{key}.")
    elif isinstance(data, list):
        for item in data:
            check_no_secrets(item, path)
    elif isinstance(data, str):
        kind = wallet.classify_sensitive(data)
        if kind is not None:
            raise ConfigError(
                f"Configuration value at '{path.rstrip('.')}' looks like a {kind}. "
                "Remove it: HomeMiner asks for a payout address only when a route needs it."
            )


class Config:
    def __init__(self, data: dict[str, Any], source_files: list[Path]):
        self.data = data
        self.source_files = source_files

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def data_dir(self) -> Path:
        configured = self.get("general", "data_dir") or ""
        return Path(configured) if configured else default_data_dir()

    @property
    def tools_dir(self) -> Path:
        configured = Path(self.get("general", "tools_dir") or "tools")
        return configured if configured.is_absolute() else project_root() / configured

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    def set_ui_value(self, keys: tuple[str, ...], value: Any) -> None:
        """Change a UI-editable value and persist it to settings.json."""
        if keys not in UI_EDITABLE:
            raise ConfigError(f"{'.'.join(keys)} cannot be changed from the UI")
        check_no_secrets({keys[-1]: value})
        node = self.data
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value

        overrides = _read_json(self.settings_path)
        node = overrides
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(overrides, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_config(path: Path | None = None) -> Config:
    data = copy.deepcopy(DEFAULTS)
    sources: list[Path] = []

    candidate = path or project_root() / "config" / "homeminer.toml"
    if candidate.exists():
        with candidate.open("rb") as fh:
            user = tomllib.load(fh)
        check_no_secrets(user)
        data = _merge(data, user)
        sources.append(candidate)
    elif path is not None:
        raise ConfigError(f"Configuration file not found: {path}")

    config = Config(data, sources)
    overrides = _read_json(config.settings_path)
    if overrides:
        check_no_secrets(overrides)
        allowed: dict = {}
        for keys in UI_EDITABLE:
            node: Any = overrides
            for key in keys:
                node = node.get(key) if isinstance(node, dict) else None
            if node is not None:
                target = allowed
                for key in keys[:-1]:
                    target = target.setdefault(key, {})
                target[keys[-1]] = node
        config.data = _merge(config.data, allowed)
        sources.append(config.settings_path)
    return config
