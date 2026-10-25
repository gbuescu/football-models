import pytest

from homeminer import wallet

# Test vectors from BIP-173 and BIP-350, plus long-published example addresses.
VALID_MAINNET = [
    ("BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4", "P2WPKH"),
    ("bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3", "P2WSH"),
    ("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0", "P2TR"),
    ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "P2PKH"),
    ("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy", "P2SH"),
]

INVALID = [
    # BIP-350: v1+ must use bech32m, v0 must use bech32
    "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqh2y7hd",
    "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kemeawh",
    # BIP-173: bad checksum / mixed case / invalid program length
    "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5",
    "bc1QW508d6QEJxTDG4y5R3ZArVARY0c5xW7kv8f3t4",
    "bc1zw508d6qejxtdg4y5r3zarvaryvqyzf3du",
    # Base58 with a corrupted checksum
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb",
    "",
    "not an address",
]


@pytest.mark.parametrize("address,kind", VALID_MAINNET)
def test_valid_mainnet_addresses(address, kind):
    check = wallet.validate_btc_address(address)
    assert check.valid, check.reason
    assert check.network == "mainnet"
    assert kind in check.kind


@pytest.mark.parametrize("address", INVALID)
def test_invalid_addresses(address):
    assert not wallet.validate_btc_address(address).valid


def test_testnet_rejected_for_mainnet_payouts():
    testnet = "tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7"
    check = wallet.validate_btc_address(testnet)
    assert not check.valid
    assert check.network == "testnet"
    assert "testnet" in check.reason
    assert wallet.validate_btc_address(testnet, expected_network="testnet").valid


def test_future_segwit_version_warns():
    check = wallet.validate_btc_address("bc1zw508d6qejxtdg4y5r3zarvaryvaxxpcs")
    assert check.valid
    assert any("Future SegWit" in w for w in check.warnings)


def test_taproot_warns_about_service_support():
    check = wallet.validate_btc_address(VALID_MAINNET[2][0])
    assert any("Taproot" in w for w in check.warnings)


def test_every_valid_result_says_format_is_not_ownership():
    check = wallet.validate_btc_address(VALID_MAINNET[0][0])
    assert any("does not prove" in w for w in check.warnings)


def test_seed_phrase_is_refused_without_echo():
    seed = " ".join(["abandon"] * 11 + ["about"])
    check = wallet.validate_btc_address(seed)
    assert not check.valid
    assert "seed phrase" in check.reason
    assert "abandon" not in check.reason


def test_private_keys_refused():
    # The well-known WIF example for private key 1 (never use it).
    wif = "5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ"
    assert wallet.classify_sensitive(wif) == "WIF private key"
    assert not wallet.validate_btc_address(wif).valid
    assert wallet.classify_sensitive("a" * 64) == "raw private key or hash"
    assert wallet.classify_sensitive("xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi") == "extended private key"


def test_lightning_invoice_is_not_an_address():
    assert wallet.classify_sensitive("lnbc1500n1pj9xyz") == "Lightning invoice or LNURL"
    assert not wallet.validate_btc_address("lnbc1500n1pj9xyz").valid


def test_mask_hides_middle():
    assert wallet.mask("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4") == "bc1q…f3t4"


def test_payout_store_only_for_routes_that_need_it(tmp_path):
    store = wallet.PayoutStore(tmp_path)
    with pytest.raises(ValueError):
        store.save("benchmark", VALID_MAINNET[0][0])
    bad = store.save("nicehash", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5")
    assert not bad.valid and store.load("nicehash") is None
    assert store.save("nicehash", VALID_MAINNET[0][0]).valid
    assert store.load("nicehash") == VALID_MAINNET[0][0]
    store.forget("nicehash")
    assert store.load("nicehash") is None
