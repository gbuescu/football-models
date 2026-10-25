import logging
import tomllib
from pathlib import Path

import pytest

from homeminer import config as config_mod
from homeminer import redact

ADDR = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
SAMPLE = Path(__file__).parents[1] / "config" / "homeminer.sample.toml"


def test_redacts_addresses_and_nicehash_usernames():
    text = f"-u {ADDR}.homeminer and legacy 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
    out = redact.redact(text)
    assert ADDR not in out and "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa" not in out
    assert out.count(redact.ADDRESS_PLACEHOLDER) == 2
    assert ".homeminer" in out


def test_keeps_hashes_and_non_address_text():
    text = "hash sum = 6C3C3A4F0E5D4B21 algo rx/0 sha256 " + "ab" * 32
    assert redact.redact(text) == text


def test_redacts_windows_profile_paths():
    out = redact.redact(r"C:\Users\Jane Doe\AppData\Local\HomeMiner\x.log".replace("Jane Doe", "janedoe"))
    assert "janedoe" not in out
    assert r"C:\Users\<user>\AppData" in out


def test_redacts_extra_identifiers():
    assert "MYPC-1234" not in redact.redact("host MYPC-1234 ready", extra=["MYPC-1234"])


def test_logging_filter_redacts(caplog):
    logger = logging.getLogger("homeminer.test")
    handler = logging.Handler()
    records = []
    handler.emit = records.append
    handler.addFilter(redact.RedactingFilter())
    logger.addHandler(handler)
    try:
        logger.warning("payout to %s", ADDR)
    finally:
        logger.removeHandler(handler)
    assert ADDR not in records[0].getMessage()


def test_sample_config_matches_defaults_and_has_no_secrets():
    with SAMPLE.open("rb") as fh:
        sample = tomllib.load(fh)
    config_mod.check_no_secrets(sample)

    def keys(d, prefix=""):
        out = set()
        for k, v in d.items():
            out.add(prefix + k)
            if isinstance(v, dict):
                out |= keys(v, prefix + k + ".")
        return out

    assert keys(sample) == keys(config_mod.DEFAULTS)
    assert sample["economics"]["electricity_eur_per_kwh"] == 0.0
    assert sample["mining"]["approved"] is False


@pytest.mark.parametrize("bad", [
    {"mining": {"wallet_address": "x"}},
    {"general": {"note": ADDR}},
    {"general": {"note": " ".join(["abandon"] * 11 + ["about"])}},
    {"secrets": {"password": "hunter2"}},
])
def test_config_with_secrets_is_refused(bad):
    with pytest.raises(config_mod.ConfigError):
        config_mod.check_no_secrets(bad)


def test_ui_values_persist_outside_the_repo(cfg):
    cfg.set_ui_value(("economics", "electricity_eur_per_kwh"), 0.25)
    assert cfg.settings_path.exists()
    assert config_mod.project_root() not in cfg.settings_path.parents
    reloaded = config_mod.load_config(SAMPLE)
    assert reloaded.get("economics", "electricity_eur_per_kwh") == 0.25
    with pytest.raises(config_mod.ConfigError):
        cfg.set_ui_value(("mining", "approved"), True)
