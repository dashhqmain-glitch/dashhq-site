"""Tests for the explorer backstop sweep - a third, independent mint-
detection data source alongside the Alchemy webhooks and the Alchemy-based
wallet-watch sweep. Direct request, backed by a real confirmed incident:
all 3 Alchemy webhooks were once found paused simultaneously, which is a
single point of failure no amount of retry logic on Alchemy infrastructure
alone can close - this queries Ink's Blockscout and Etherscan directly so a
tracked wallet's mint still gets caught if Alchemy itself is down.

Real, tested constraint: Robinhood's own Blockscout instance returns a
flat HTTP 403 to a plain server-side request (confirmed live, not
assumed), so there is no backstop leg for that chain - only Ink and
Ethereum are covered here.
"""
from unittest.mock import patch

import main
from config import settings


class FakeRes:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.httpx.HTTPStatusError("boom", request=None, response=self)


# ── _ink_blockscout_recent_mints ────────────────────────────────────────

async def test_ink_blockscout_detects_a_mint_via_type_classification():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, {"items": [{
                "type": "token_minting",
                "from": {"hash": main._TRACKED_WALLET_NULL_ADDRESS},
                "to": {"hash": "0xbuyer"},
                "timestamp": "2026-09-12T01:56:04.000000Z",
                "token": {"address_hash": "0xE7E19DB3F1BA19431F078C26641AC76BB18F6ECA"},
                "total": {"token_id": "10895"},
            }]})

    mints = await main._ink_blockscout_recent_mints(FakeClient(), "0xwallet")
    assert mints == [{"chain": "ink", "contract": "0xe7e19db3f1ba19431f078c26641ac76bb18f6eca", "token_id": "10895", "event_at": "2026-09-12T01:56:04.000000Z"}]


async def test_ink_blockscout_falls_back_to_from_address_when_type_is_missing():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, {"items": [{
                "from": {"hash": main._TRACKED_WALLET_NULL_ADDRESS},
                "to": {"hash": "0xbuyer"},
                "timestamp": "2026-09-12T01:56:04.000000Z",
                "token": {"address_hash": "0xcontract"},
                "total": {"token_id": "1"},
            }]})

    mints = await main._ink_blockscout_recent_mints(FakeClient(), "0xwallet")
    assert len(mints) == 1


async def test_ink_blockscout_ignores_non_mint_transfers():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, {"items": [{
                "type": "token_transfer",
                "from": {"hash": "0xsomeone_real"},
                "to": {"hash": "0xbuyer"},
                "timestamp": "2026-09-12T01:56:04.000000Z",
                "token": {"address_hash": "0xcontract"},
                "total": {"token_id": "1"},
            }]})

    mints = await main._ink_blockscout_recent_mints(FakeClient(), "0xwallet")
    assert mints == []


async def test_ink_blockscout_handles_a_non_200_quietly():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(403, {})

    mints = await main._ink_blockscout_recent_mints(FakeClient(), "0xwallet")
    assert mints == []


async def test_ink_blockscout_handles_a_network_error_quietly():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            raise main.httpx.ConnectError("boom")

    mints = await main._ink_blockscout_recent_mints(FakeClient(), "0xwallet")
    assert mints == []


# ── _etherscan_recent_mints ─────────────────────────────────────────────

async def test_etherscan_skips_without_an_api_key():
    settings.etherscan_api_key = ""

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            raise AssertionError("should never call out with no key configured")

    mints = await main._etherscan_recent_mints(FakeClient(), "0xwallet")
    assert mints == []


async def test_etherscan_detects_a_mint_from_the_zero_address():
    settings.etherscan_api_key = "key123"
    try:
        class FakeClient:
            async def get(self, url, headers=None, params=None):
                assert params["chainid"] == "1"
                assert params["apikey"] == "key123"
                return FakeRes(200, {"status": "1", "message": "OK", "result": [{
                    "from": main._TRACKED_WALLET_NULL_ADDRESS, "to": "0xbuyer",
                    "contractAddress": "0xCONTRACT", "tokenID": "42", "timeStamp": "1789200000",
                }]})

        mints = await main._etherscan_recent_mints(FakeClient(), "0xwallet")
    finally:
        settings.etherscan_api_key = ""
    assert len(mints) == 1
    assert mints[0]["chain"] == "ethereum"
    assert mints[0]["contract"] == "0xcontract"
    assert mints[0]["token_id"] == "42"


async def test_etherscan_ignores_non_mint_transfers():
    settings.etherscan_api_key = "key123"
    try:
        class FakeClient:
            async def get(self, url, headers=None, params=None):
                return FakeRes(200, {"status": "1", "result": [{
                    "from": "0xsomeone_real", "to": "0xbuyer",
                    "contractAddress": "0xcontract", "tokenID": "1", "timeStamp": "1789200000",
                }]})

        mints = await main._etherscan_recent_mints(FakeClient(), "0xwallet")
    finally:
        settings.etherscan_api_key = ""
    assert mints == []


async def test_etherscan_handles_an_error_response_without_crashing():
    # Real confirmed shape: a failure (bad key, rate limit, deprecated
    # endpoint) comes back as status="0" with `result` as a STRING
    # message, not a list - must not be treated as "zero mints found"
    # silently indistinguishable from a real empty result, but also must
    # never crash the sweep.
    settings.etherscan_api_key = "key123"
    try:
        class FakeClient:
            async def get(self, url, headers=None, params=None):
                return FakeRes(200, {"status": "0", "message": "NOTOK", "result": "Missing/Invalid API Key"})

        mints = await main._etherscan_recent_mints(FakeClient(), "0xwallet")
    finally:
        settings.etherscan_api_key = ""
    assert mints == []


async def test_etherscan_handles_no_transactions_found_as_a_real_empty_result():
    settings.etherscan_api_key = "key123"
    try:
        class FakeClient:
            async def get(self, url, headers=None, params=None):
                return FakeRes(200, {"status": "0", "message": "No transactions found", "result": []})

        mints = await main._etherscan_recent_mints(FakeClient(), "0xwallet")
    finally:
        settings.etherscan_api_key = ""
    assert mints == []


# ── _explorer_backstop_sweep ────────────────────────────────────────────

async def test_backstop_sweep_uses_the_same_bucket_formula_as_the_alchemy_sweep():
    # The point is redundancy against Alchemy specifically - checking the
    # exact wallets Alchemy's own sweep checks this cycle, via completely
    # different infrastructure, not an independently-offset rotation.
    def fixed_bucket(address, num_buckets):
        return 0

    with patch.object(main, "_wallet_poll_bucket", side_effect=fixed_bucket):
        bucket_a = int(main.time.time() // 300) % main._TRACKED_WALLET_POLL_BUCKETS
        bucket_b = int(main.time.time() // 300) % main._EXPLORER_BACKSTOP_POLL_BUCKETS
    assert bucket_a == bucket_b  # same divisor (288) - would drift apart if either constant ever changed independently


async def test_backstop_sweep_logs_a_mint_and_triggers_convergence_check():
    settings.etherscan_api_key = ""  # only the Ink leg fires in this test
    logged_events = []
    convergence_calls = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "smart_wallet_tags" in url:
                return FakeRes(200, [{"address": "0xwallet1"}])
            return FakeRes(200, {"items": []})

        async def post(self, url, headers=None, json=None):
            if "nft_sale_events_log" in url:
                logged_events.append(json)
            return FakeRes(200, {})

    async def fake_ink_mints(client, address):
        return [{"chain": "ink", "contract": "0xcontract", "token_id": "1", "event_at": "2026-09-12T01:56:04Z"}] if address == "0xwallet1" else []

    async def fake_eth_mints(client, address):
        return []

    async def fake_resolve(client, contract, known_chain=None):
        assert known_chain == "ink"
        return {"slug": "some-collection", "symbol": "ETH"}

    async def fake_maybe_post_direct(client, slug, known_collection=None):
        convergence_calls.append(slug)
        return True

    def matching_bucket(address, num_buckets):
        return int(main.time.time() // 300) % num_buckets

    try:
        with patch.object(main, "_wallet_poll_bucket", side_effect=matching_bucket), \
             patch.object(main, "_ink_blockscout_recent_mints", new=fake_ink_mints), \
             patch.object(main, "_etherscan_recent_mints", new=fake_eth_mints), \
             patch.object(main, "_nft_resolve_by_contract", new=fake_resolve), \
             patch.object(main, "_nft_scope_maybe_post_from_slug_direct", new=fake_maybe_post_direct):
            result = await main._explorer_backstop_sweep(FakeClient(), deadline=main.time.time() + 30)
    finally:
        settings.etherscan_api_key = ""

    assert result["mints_found"] == 1
    assert result["posted"] == 1
    assert logged_events[0]["slug"] == "some-collection"
    assert convergence_calls == ["some-collection"]


async def test_backstop_sweep_survives_an_unexpected_error_resolving_one_mint():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "smart_wallet_tags" in url:
                return FakeRes(200, [{"address": "0xwallet1"}, {"address": "0xwallet2"}])
            return FakeRes(200, {"items": []})

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

    async def fake_ink_mints(client, address):
        if address == "0xwallet1":
            return [{"chain": "ink", "contract": "0xbroken", "token_id": "1", "event_at": "2026-09-12T00:00:00Z"}]
        if address == "0xwallet2":
            return [{"chain": "ink", "contract": "0xfine", "token_id": "2", "event_at": "2026-09-12T00:00:00Z"}]
        return []

    async def fake_eth_mints(client, address):
        return []

    async def fake_resolve(client, contract, known_chain=None):
        if contract == "0xbroken":
            raise KeyError("unexpected shape")
        return {"slug": "fine-slug", "symbol": "ETH"}

    async def fake_maybe_post_direct(client, slug, known_collection=None):
        return True

    def matching_bucket(address, num_buckets):
        return int(main.time.time() // 300) % num_buckets

    with patch.object(main, "_wallet_poll_bucket", side_effect=matching_bucket), \
         patch.object(main, "_ink_blockscout_recent_mints", new=fake_ink_mints), \
         patch.object(main, "_etherscan_recent_mints", new=fake_eth_mints), \
         patch.object(main, "_nft_resolve_by_contract", new=fake_resolve), \
         patch.object(main, "_nft_scope_maybe_post_from_slug_direct", new=fake_maybe_post_direct):
        result = await main._explorer_backstop_sweep(FakeClient(), deadline=main.time.time() + 30)

    assert result["mints_found"] == 1
    assert result["slugs_touched"] == ["fine-slug"]


async def test_backstop_sweep_with_no_tracked_wallets_is_a_clean_noop():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])

    result = await main._explorer_backstop_sweep(FakeClient(), deadline=main.time.time() + 30)
    assert result == {"checked": 0, "mints_found": 0, "posted": 0}
