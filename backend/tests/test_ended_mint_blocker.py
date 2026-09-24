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

    samples_asked_for = []

    async def fake_signals(client, c, sample=1):
        samples_asked_for.append(sample)
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
    assert live["passes_live_gate"] is False  # the exact wrapper production uses agrees with the verdict
    assert samples_asked_for == [100, 1]  # the diagnostic samples deeply; the live gate asks for just the newest mint
    assert "recent_mint_timestamps" not in live
    assert missing == {"slug": "missing", "error": "collection lookup failed (404)"}


# ── _mint_query_probe / probe=true ──────────────────────────────────────

async def test_query_probe_reports_each_variants_real_status_and_error():
    settings.alchemy_api_key = "key"
    seen = []

    class FakeHeaders(dict):
        pass

    class ProbeRes:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body
            self.headers = {"content-type": "application/json"}
            self.text = str(body)

        def json(self):
            return self._body

    class FakeClient:
        async def post(self, url, json=None):
            params = json["params"][0]
            seen.append((params["order"], params["maxCount"], "withMetadata" in params, tuple(params["category"])))
            if params["order"] == "desc":
                return ProbeRes(200, {"error": {"message": "order desc unsupported on this network"}})
            return ProbeRes(200, {"result": {"transfers": [{"to": "0x1"}]}})

    try:
        out = await main._mint_query_probe(FakeClient(), "ink", "0xabc")
    finally:
        settings.alchemy_api_key = ""

    assert set(out) == {"desc_100_meta", "desc_1_meta", "asc_1_meta", "desc_1_no_meta", "desc_1_erc721_only"}
    assert out["asc_1_meta"] == {
        "http": 200, "transfers_returned": 1, "error": None,
        "first_metadata": None, "first_blockNum": None, "first_timestamp_parses_to": None,
    }
    assert out["desc_1_meta"]["transfers_returned"] is None
    assert out["desc_1_meta"]["error"] == {"message": "order desc unsupported on this network"}
    assert ("desc", hex(100), True, ("erc721", "erc1155")) in seen
    assert ("desc", hex(1), False, ("erc721", "erc1155")) in seen


async def test_query_probe_is_a_no_op_without_a_key_or_supported_chain():
    settings.alchemy_api_key = ""
    assert await main._mint_query_probe(object(), "ink", "0xabc") == {"error": "no Alchemy key or unsupported chain"}


async def test_check_mint_status_only_probes_when_asked():
    settings.cron_secret = "s"
    probed = []

    async def fake_core(slug):
        return collection(contract="0xprobe", chain="ink")

    async def fake_signals(client, c, sample=1):
        return {"chain": "ink", "alchemy_chain": "ink", "contract": "0xprobe", "last_mint_age_seconds": None,
                "recent_mint_timestamps": [], "total_supply": None, "max_supply": None}

    async def fake_probe(client, chain, contract):
        probed.append((chain, contract))
        return {"desc_1_meta": {"http": 200}}

    try:
        with patch.object(main, "_nft_collection_core", new=fake_core), patch.object(main, "_mint_status_signals", new=fake_signals), \
             patch.object(main, "_mint_query_probe", new=fake_probe):
            plain = await main.check_mint_status(FakeRequest("Bearer s"), slugs="x")
            probed_result = await main.check_mint_status(FakeRequest("Bearer s"), slugs="x", probe=True)
    finally:
        settings.cron_secret = "test-cron-secret"

    assert "query_probe" not in plain["results"][0]
    assert probed_result["results"][0]["query_probe"] == {"desc_1_meta": {"http": 200}}
    assert probed == [("ink", "0xprobe")]


async def test_query_probe_surfaces_a_timestamp_the_parser_cannot_read():
    # The exact failure mode the dry run pointed at: Alchemy returns
    # transfers, but the timestamp on them can't be turned into a time.
    settings.alchemy_api_key = "key"

    class ProbeRes:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = ""

        def json(self):
            return {"result": {"transfers": [{"metadata": {"blockTimestamp": "2026-13-45 nonsense"}}]}}

    class FakeClient:
        async def post(self, url, json=None):
            return ProbeRes()

    try:
        out = await main._mint_query_probe(FakeClient(), "ink", "0xabc")
    finally:
        settings.alchemy_api_key = ""

    assert out["desc_1_meta"]["first_metadata"] == {"blockTimestamp": "2026-13-45 nonsense"}
    assert out["desc_1_meta"]["first_timestamp_parse_error"].startswith("ValueError")
    assert "first_timestamp_parses_to" not in out["desc_1_meta"]


# ── block-timestamp fallback (Ink returns metadata: null) ────────────────

async def test_recent_timestamps_fall_back_to_the_block_when_metadata_is_null():
    # Confirmed live on Ink: transfers come back with metadata: null, so
    # there is no blockTimestamp at all. Only the newest transfer's block is
    # resolved - one cheap call, not one per transfer.
    resolved = []

    async def fake_rpc(client, chain, method, params):
        return {"transfers": [
            {"blockNum": "0x20", "metadata": None},
            {"blockNum": "0x10", "metadata": None},
        ]}

    async def fake_block_ts(client, chain, block_num):
        resolved.append((chain, block_num))
        return 1_800_000_000.0

    with patch.object(main, "_alchemy_rpc", new=fake_rpc), patch.object(main, "_alchemy_block_timestamp", new=fake_block_ts):
        stamps = await main._mint_recent_timestamps(object(), "ink", "0xabc", count=2)

    assert stamps == [1_800_000_000.0]
    assert resolved == [("ink", "0x20")]


async def test_recent_timestamps_do_not_hit_the_block_fallback_when_metadata_exists():
    async def fake_rpc(client, chain, method, params):
        return {"transfers": [{"blockNum": "0x20", "metadata": {"blockTimestamp": iso(30)}}]}

    async def boom(*a, **k):
        raise AssertionError("must not resolve a block when the timestamp is already there")

    with patch.object(main, "_alchemy_rpc", new=fake_rpc), patch.object(main, "_alchemy_block_timestamp", new=boom):
        stamps = await main._mint_recent_timestamps(object(), "robinhood", "0xabc")

    assert len(stamps) == 1


async def test_recent_timestamps_stay_empty_when_the_block_cannot_be_resolved():
    async def fake_rpc(client, chain, method, params):
        return {"transfers": [{"blockNum": "0x20", "metadata": None}]}

    async def fake_block_ts(client, chain, block_num):
        return None

    with patch.object(main, "_alchemy_rpc", new=fake_rpc), patch.object(main, "_alchemy_block_timestamp", new=fake_block_ts):
        assert await main._mint_recent_timestamps(object(), "ink", "0xabc") == []


async def test_block_timestamp_decodes_the_chains_own_block_time():
    settings.alchemy_api_key = "key"
    seen = {}

    class FakeClient:
        async def post(self, url, json=None):
            seen["json"] = json
            return FakeRes(200, {"result": {"number": "0x20", "timestamp": hex(1_789_888_608)}})

    try:
        value = await main._alchemy_block_timestamp(FakeClient(), "ink", "0x20")
    finally:
        settings.alchemy_api_key = ""

    assert value == 1_789_888_608.0
    assert seen["json"]["method"] == "eth_getBlockByNumber"
    assert seen["json"]["params"] == ["0x20", False]


async def test_block_timestamp_returns_none_for_bad_input_or_responses():
    settings.alchemy_api_key = "key"

    class FakeClient:
        def __init__(self, res):
            self.res = res

        async def post(self, url, json=None):
            return self.res

    try:
        assert await main._alchemy_block_timestamp(FakeClient(FakeRes(200, {"result": None})), "ink", "0x20") is None
        assert await main._alchemy_block_timestamp(FakeClient(FakeRes(200, {"result": {"timestamp": "zzz"}})), "ink", "0x20") is None
        assert await main._alchemy_block_timestamp(FakeClient(FakeRes(500, {})), "ink", "0x20") is None
        assert await main._alchemy_block_timestamp(FakeClient(FakeRes(200, {"result": {"timestamp": "0x1"}})), "ink", None) is None
        assert await main._alchemy_block_timestamp(FakeClient(FakeRes(200, {"result": {"timestamp": "0x1"}})), "ink", "20") is None
    finally:
        settings.alchemy_api_key = ""


# ── wired into the Alert Tracker ("N Wallet Minting X") ─────────────────

def _tracker_collection():
    return {"name": "Test Collection", "slug": "test-collection", "floor": 0.05, "symbol": "ETH", "chain": "ethereum",
            "openseaUrl": "https://opensea.io/collection/test-collection", "image": None}


def _tracker_score(**overrides):
    data = {"tier": "red", "blocked": False, "has_real_activity": True, "has_timeliness_signal": True}
    data.update(overrides)
    return data


_TRACKER_HITS = [{"address": "0xa", "tag": "REALCOIN", "rank": None, "pnl": None}]


async def _run_tracker(ended_check=None, score=None, patch_ended=True):
    posted, recorded = [], []

    async def fake_already(client, slug):
        return False

    async def fake_wash(client, slug):
        return True

    async def fake_post(client, channel_id, embed, content=None, components=None):
        posted.append(embed["title"])
        return True

    async def record(*a, **k):
        recorded.append(1)

    patches = [
        patch.object(main, "_alert_tracker_already_posted", new=fake_already),
        patch.object(main, "_nft_scope_clears_wash_check", new=fake_wash),
        patch.object(main, "_post_channel_message", new=fake_post),
        patch.object(main, "_nft_scope_mark_posted", new=record),
        patch.object(main, "_nft_scope_record_call_buyers", new=record),
        patch.object(main, "_alert_tracker_record_call", new=record),
    ]
    if patch_ended:
        patches.append(patch.object(main, "_mint_has_ended", new=ended_check))
    for p in patches:
        p.start()
    try:
        result = await main._nft_scope_maybe_post_tracked_convergence(
            main.httpx.AsyncClient(), "slug", _tracker_collection(), _TRACKER_HITS, score or _tracker_score(),
        )
    finally:
        for p in patches:
            p.stop()
    return result, posted, recorded


async def test_alert_tracker_withholds_a_post_when_the_mint_has_ended():
    async def ended(client, c):
        return True, "sold out (4,444 of 4,444 minted)"

    result, posted, recorded = await _run_tracker(ended)
    assert result is False
    assert posted == []
    assert recorded == []  # never counted as a call it didn't make


async def test_alert_tracker_still_posts_when_the_mint_is_live():
    async def live(client, c):
        return False, None

    result, posted, recorded = await _run_tracker(live)
    assert result is True
    assert len(posted) == 1
    assert recorded  # the call is recorded exactly as before


async def test_alert_tracker_still_posts_when_the_ended_check_itself_breaks():
    # Fail open end to end: a broken blocker must never silence a real mint.
    async def boom(*a, **k):
        raise RuntimeError("alchemy exploded")

    with patch.object(main, "_mint_status_signals", new=boom):
        result, posted, _ = await _run_tracker(patch_ended=False)
    assert result is True
    assert len(posted) == 1


async def test_alert_tracker_does_not_pay_for_the_ended_check_on_posts_that_fail_earlier_gates():
    # The check costs an Alchemy call - it must only run for a post that
    # would otherwise actually go out.
    async def must_not_run(client, c):
        raise AssertionError("ended-mint lookup ran for a post another gate had already rejected")

    result, posted, _ = await _run_tracker(must_not_run, score=_tracker_score(blocked=True))
    assert result is False
    assert posted == []


async def test_mint_still_live_reports_the_reason_it_blocked(caplog):
    import logging

    async def ended(client, c):
        return True, "no one has minted in the last 254 min"

    with patch.object(main, "_mint_has_ended", new=ended), caplog.at_level(logging.INFO):
        live = await main._mint_still_live(object(), "gas-ghosts", collection(), "Alert Tracker")

    assert live is False
    assert "withholding gas-ghosts" in caplog.text
    assert "no one has minted in the last 254 min" in caplog.text


# ── _mint_progress_text ─────────────────────────────────────────────────

def test_mint_progress_text_shows_percent_against_a_known_cap():
    assert main._mint_progress_text({"total_supply": 9999, "max_supply": 10000}) == "9,999 / 10,000 (100%)"
    assert main._mint_progress_text({"total_supply": 500, "max_supply": 1000}) == "500 / 1,000 (50%)"


def test_mint_progress_text_shows_a_bare_count_without_a_cap():
    assert main._mint_progress_text({"total_supply": 4321, "max_supply": None}) == "4,321 minted"
    assert main._mint_progress_text({"total_supply": 4321, "max_supply": 0}) == "4,321 minted"  # open-edition "no cap"


def test_mint_progress_text_is_none_when_nothing_is_known():
    assert main._mint_progress_text(None) is None
    assert main._mint_progress_text({}) is None
    assert main._mint_progress_text({"total_supply": None, "max_supply": None}) is None


# ── _mint_status_signals_cached: shared by the blocker and the embed ────

async def test_status_signals_cached_shares_its_cache_with_the_ended_mint_check():
    # The whole point of the refactor: one real on-chain lookup serves both
    # the blocker's verdict and the embed's display data, not two.
    calls = []

    async def fake_signals(client, c, sample=1):
        calls.append(1)
        return {"last_mint_age_seconds": 5.0, "total_supply": 100, "max_supply": 200, "recent_mint_timestamps": []}

    with patch.object(main, "_mint_status_signals", new=fake_signals):
        ended, _ = await main._mint_has_ended(object(), collection(contract="0xshared"))
        signals = await main._mint_status_signals_cached(object(), collection(contract="0xshared"))

    assert ended is False  # 100/200, not sold out, and recently minted
    assert signals["total_supply"] == 100 and signals["max_supply"] == 200
    assert len(calls) == 1  # second call served from the same cache _mint_has_ended already warmed


async def test_status_signals_cached_returns_the_empty_shape_for_an_unsupported_collection():
    class Boom:
        async def post(self, *a, **k):
            raise AssertionError("must not call out")

    signals = await main._mint_status_signals_cached(Boom(), collection(chain="avalanche"))
    assert signals["total_supply"] is None and signals["max_supply"] is None
    assert signals["alchemy_chain"] is None


async def test_status_signals_cached_fails_open_on_any_exception():
    async def boom(*a, **k):
        raise RuntimeError("alchemy exploded")

    with patch.object(main, "_mint_status_signals", new=boom):
        signals = await main._mint_status_signals_cached(object(), collection(contract="0xerr"))
    assert signals["total_supply"] is None and signals["max_supply"] is None


# ── End-to-end: the Alert Tracker post actually carries real mint data ──

async def test_alert_tracker_post_carries_real_floor_and_mint_progress_end_to_end():
    # Not just "the embed builder accepts a mint_signals param" - this
    # proves _nft_scope_maybe_post_tracked_convergence actually fetches
    # real on-chain signals and the posted embed actually contains them.
    posted_embeds = []

    async def fake_already(client, slug):
        return False

    async def fake_wash(client, slug):
        return True

    async def fake_signals(client, c, sample=1):
        return {"last_mint_age_seconds": 30.0, "total_supply": 777, "max_supply": 1000, "recent_mint_timestamps": [time.time() - 30]}

    async def fake_post(client, channel_id, embed, content=None, components=None):
        posted_embeds.append(embed)
        return True

    async def record(*a, **k):
        pass

    c = {
        "name": "Real Collection", "slug": "real-collection", "floor": 0.42, "floorUsd": 1500.0, "symbol": "ETH",
        "chain": "ethereum", "openseaUrl": "https://opensea.io/collection/real-collection", "image": None,
        "contractAddress": "0xrealcontract",
    }
    hits = [{"address": "0xa", "tag": "T1", "rank": None, "pnl": None, "category": "Whale"}]
    score = {"tier": "red", "blocked": False, "has_real_activity": True, "has_timeliness_signal": True}

    with patch.object(main, "_alert_tracker_already_posted", new=fake_already), \
         patch.object(main, "_nft_scope_clears_wash_check", new=fake_wash), \
         patch.object(main, "_mint_status_signals", new=fake_signals), \
         patch.object(main, "_post_channel_message", new=fake_post), \
         patch.object(main, "_nft_scope_mark_posted", new=record), \
         patch.object(main, "_nft_scope_record_call_buyers", new=record), \
         patch.object(main, "_alert_tracker_record_call", new=record):
        result = await main._nft_scope_maybe_post_tracked_convergence(main.httpx.AsyncClient(), "real-collection", c, hits, score)

    assert result is True
    embed = posted_embeds[0]
    floor_field = next(f for f in embed["fields"] if f["name"] == "💰 Floor Price")
    assert floor_field["value"] == "0.4200 ETH (~$1,500.00)"
    progress_field = next(f for f in embed["fields"] if f["name"] == "🎟️ Mint Progress")
    assert progress_field["value"] == "777 / 1,000 (78%)"
