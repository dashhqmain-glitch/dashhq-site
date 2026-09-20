"""Tests for the ended-mint blocker - members kept getting "N Wallet
Minting X" (Alert Tracker) and "New Mint" (NFT Scope Pass 1) posts for
mints that had already ended (sold out or closed), because nothing in the
posting pipeline ever asked "can someone still mint this RIGHT NOW".

Two contract-agnostic signals, either one blocks: sold out (on-chain
totalSupply() reached maxSupply()/MAX_SUPPLY()) and gone quiet (nobody has
minted the contract within _MINT_ENDED_QUIET_SECONDS). Both fail OPEN -
an unknown is never treated as "ended".
"""
import time
from unittest.mock import patch

import main
from config import settings


class FakeRes:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.httpx.HTTPStatusError("boom", request=None, response=self)


class FakeRequest:
    def __init__(self, auth=None):
        self.headers = {"authorization": auth} if auth is not None else {}


def iso(seconds_ago):
    return main.datetime.fromtimestamp(time.time() - seconds_ago, tz=main.timezone.utc).isoformat().replace("+00:00", "Z")


def collection(contract="0xabc", chain="ethereum"):
    return {"slug": "some-slug", "name": "Some Collection", "contractAddress": contract, "chain": chain}


def setup_function(_):
    main._mint_ended_cache.clear()


# ── _mint_ended_verdict ─────────────────────────────────────────────────

def test_verdict_blocks_a_sold_out_mint_even_if_it_minted_a_second_ago():
    ended, reason = main._mint_ended_verdict(last_mint_age_seconds=1, total_supply=1000, max_supply=1000)
    assert ended is True
    assert "sold out" in reason and "1,000" in reason


def test_verdict_blocks_a_mint_that_has_gone_quiet():
    ended, reason = main._mint_ended_verdict(main._MINT_ENDED_QUIET_SECONDS + 60, total_supply=10, max_supply=1000)
    assert ended is True
    assert "no one has minted" in reason


def test_verdict_allows_a_live_mint():
    assert main._mint_ended_verdict(30, total_supply=400, max_supply=1000) == (False, None)


def test_verdict_boundary_is_strictly_greater_than_the_quiet_window():
    assert main._mint_ended_verdict(main._MINT_ENDED_QUIET_SECONDS, None, None) == (False, None)


def test_verdict_fails_open_when_nothing_is_known():
    assert main._mint_ended_verdict(None, None, None) == (False, None)


def test_verdict_ignores_a_zero_or_missing_max_supply():
    # Open editions report 0 for "no cap" - that's not "sold out at 0".
    assert main._mint_ended_verdict(30, total_supply=500, max_supply=0) == (False, None)
    assert main._mint_ended_verdict(30, total_supply=500, max_supply=None) == (False, None)


def test_verdict_does_not_treat_a_clock_skewed_age_as_ended():
    assert main._mint_ended_verdict(0.0, None, None) == (False, None)


# ── _mint_recent_timestamps ─────────────────────────────────────────────

async def test_recent_timestamps_are_parsed_newest_first_and_skip_malformed_rows():
    async def fake_rpc(client, chain, method, params):
        assert method == "alchemy_getAssetTransfers"
        assert params["fromAddress"] == main._TRACKED_WALLET_NULL_ADDRESS
        assert params["contractAddresses"] == ["0xabc"]
        assert params["order"] == "desc"
        assert params["maxCount"] == hex(3)
        return {"transfers": [
            {"metadata": {"blockTimestamp": iso(60)}},
            {"metadata": {}},
            {"metadata": {"blockTimestamp": "not-a-date"}},
            {"metadata": {"blockTimestamp": iso(120)}},
        ]}

    with patch.object(main, "_alchemy_rpc", new=fake_rpc):
        stamps = await main._mint_recent_timestamps(object(), "ethereum", "0xabc", count=3)

    assert len(stamps) == 2
    assert stamps[0] > stamps[1]


async def test_recent_timestamps_are_empty_when_alchemy_returns_nothing():
    async def fake_rpc(*a, **k):
        return None

    with patch.object(main, "_alchemy_rpc", new=fake_rpc):
        assert await main._mint_recent_timestamps(object(), "ethereum", "0xabc") == []


# ── _alchemy_eth_call_uint ──────────────────────────────────────────────

async def test_eth_call_uint_decodes_a_32_byte_return():
    settings.alchemy_api_key = "key"
    seen = {}

    class FakeClient:
        async def post(self, url, json=None):
            seen["json"] = json
            return FakeRes(200, {"result": "0x" + hex(1234)[2:].rjust(64, "0")})

    try:
        value = await main._alchemy_eth_call_uint(FakeClient(), "ethereum", "0xabc", "0x18160ddd")
    finally:
        settings.alchemy_api_key = ""

    assert value == 1234
    assert seen["json"]["method"] == "eth_call"
    assert seen["json"]["params"] == [{"to": "0xabc", "data": "0x18160ddd"}, "latest"]


async def test_eth_call_uint_returns_none_for_a_missing_function_or_error():
    settings.alchemy_api_key = "key"

    class FakeClient:
        def __init__(self, res):
            self.res = res

        async def post(self, url, json=None):
            return self.res

    try:
        assert await main._alchemy_eth_call_uint(FakeClient(FakeRes(200, {"result": "0x"})), "ethereum", "0xabc", "0x1") is None
        assert await main._alchemy_eth_call_uint(FakeClient(FakeRes(200, {"error": {"message": "execution reverted"}})), "ethereum", "0xabc", "0x1") is None
        assert await main._alchemy_eth_call_uint(FakeClient(FakeRes(429, {})), "ethereum", "0xabc", "0x1") is None
    finally:
        settings.alchemy_api_key = ""


async def test_eth_call_uint_is_a_no_op_without_a_key_or_supported_chain():
    class Boom:
        async def post(self, *a, **k):
            raise AssertionError("must not call out")

    settings.alchemy_api_key = ""
    assert await main._alchemy_eth_call_uint(Boom(), "ethereum", "0xabc", "0x1") is None
    settings.alchemy_api_key = "key"
    try:
        assert await main._alchemy_eth_call_uint(Boom(), "avalanche", "0xabc", "0x1") is None
    finally:
        settings.alchemy_api_key = ""


# ── _mint_status_signals ────────────────────────────────────────────────

async def test_signals_skip_every_call_for_an_unsupported_chain():
    class Boom:
        async def post(self, *a, **k):
            raise AssertionError("must not call out")

    signals = await main._mint_status_signals(Boom(), collection(chain="avalanche"))
    assert signals["alchemy_chain"] is None
    assert signals["last_mint_age_seconds"] is None and signals["total_supply"] is None


async def test_signals_combine_mint_age_and_supply_and_prefer_the_first_real_cap():
    async def fake_stamps(client, chain, contract, count=1):
        return [time.time() - 90]

    async def fake_call(client, chain, contract, selector):
        return {
            main._MINT_ENDED_TOTAL_SUPPLY_SELECTOR: 750,
            main._MINT_ENDED_MAX_SUPPLY_SELECTORS[0]: 0,  # open-edition style "no cap" - skipped
            main._MINT_ENDED_MAX_SUPPLY_SELECTORS[1]: 1000,
        }[selector]

    with patch.object(main, "_mint_recent_timestamps", new=fake_stamps), patch.object(main, "_alchemy_eth_call_uint", new=fake_call):
        signals = await main._mint_status_signals(object(), collection(chain="matic"))

    assert signals["alchemy_chain"] == "polygon"
    assert 85 < signals["last_mint_age_seconds"] < 95
    assert signals["total_supply"] == 750
    assert signals["max_supply"] == 1000


# ── _mint_has_ended ─────────────────────────────────────────────────────

async def test_has_ended_reports_sold_out_and_caches_the_verdict():
    calls = []

    async def fake_signals(client, c, sample=1):
        calls.append(1)
        return {"last_mint_age_seconds": 5.0, "total_supply": 100, "max_supply": 100, "recent_mint_timestamps": []}

    with patch.object(main, "_mint_status_signals", new=fake_signals):
        first = await main._mint_has_ended(object(), collection(contract="0xsoldout"))
        second = await main._mint_has_ended(object(), collection(contract="0xsoldout"))

    assert first[0] is True and "sold out" in first[1]
    assert second == first
    assert len(calls) == 1


async def test_has_ended_fails_open_on_any_exception():
    async def boom(*a, **k):
        raise RuntimeError("alchemy exploded")

    with patch.object(main, "_mint_status_signals", new=boom):
        assert await main._mint_has_ended(object(), collection(contract="0xerr")) == (False, None)


async def test_has_ended_does_not_cache_when_nothing_was_learned():
    calls = []

    async def fake_signals(client, c, sample=1):
        calls.append(1)
        return {"last_mint_age_seconds": None, "total_supply": None, "max_supply": None, "recent_mint_timestamps": []}

    with patch.object(main, "_mint_status_signals", new=fake_signals):
        assert await main._mint_has_ended(object(), collection(contract="0xunknown")) == (False, None)
        assert await main._mint_has_ended(object(), collection(contract="0xunknown")) == (False, None)

    assert len(calls) == 2


async def test_has_ended_is_unknown_for_a_collection_without_a_contract_or_supported_chain():
    class Boom:
        async def post(self, *a, **k):
            raise AssertionError("must not call out")

    assert await main._mint_has_ended(Boom(), {"contractAddress": None, "chain": "ethereum"}) == (False, None)
    assert await main._mint_has_ended(Boom(), collection(chain="avalanche")) == (False, None)


# ── /cron/check-mint-status ─────────────────────────────────────────────

async def test_check_mint_status_requires_the_cron_secret():
    settings.cron_secret = "s"
    try:
        try:
            await main.check_mint_status(FakeRequest("Bearer wrong"), slugs="a")
            assert False, "should have raised"
        except main.HTTPException as e:
            assert e.status_code == 401
    finally:
        settings.cron_secret = "test-cron-secret"


async def test_check_mint_status_reports_signals_rates_and_the_verdict():
    settings.cron_secret = "s"
    now = time.time()

    async def fake_core(slug):
        if slug == "missing":
            raise main.HTTPException(status_code=404, detail="Collection not found")
        return collection(contract="0xdiag")

    async def fake_signals(client, c, sample=1):
        assert sample == 100
        stamps = [now - 30, now - 400, now - 1500, now - 3000, now - 9000]
        return {
            "chain": "ethereum", "alchemy_chain": "ethereum", "contract": "0xdiag",
            "last_mint_age_seconds": 30.0, "recent_mint_timestamps": stamps, "total_supply": 5, "max_supply": 5,
        }

    try:
        with patch.object(main, "_nft_collection_core", new=fake_core), patch.object(main, "_mint_status_signals", new=fake_signals):
            result = await main.check_mint_status(FakeRequest("Bearer s"), slugs="live-one, missing")
    finally:
        settings.cron_secret = "test-cron-secret"

    live, missing = result["results"]
    assert live["slug"] == "live-one"
    assert live["sampled_mints"] == 5
    assert live["mints_last_10m"] == 2
    assert live["mints_last_30m"] == 3
    assert live["mints_last_60m"] == 4
    assert live["would_block"] is True and "sold out" in live["reason"]
    assert "recent_mint_timestamps" not in live
    assert missing == {"slug": "missing", "error": "collection lookup failed (404)"}
