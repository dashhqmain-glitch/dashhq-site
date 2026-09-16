"""Tests for the Arc Mint Radar - "see the projects currently minting on
Arc" (chain ID 5042, Circle's USDC-gas L1), the general-discovery
equivalent of NFT Scope's Pass 1 for ethereum/robinhood/ink. Can't reuse
Pass 1 itself: every scoring/posting function it depends on
(_nft_resolve_by_contract, _nft_collection_core, floor price, verified
badge, wash-trade checks) is built on OpenSea's API, and OpenSea does not
support Arc at all (confirmed live). MintGo (mintgo.fun) does track Arc but
sits behind an explicit same-origin browser-session check that would need
spoofing to defeat - not an option here, same reasoning as the declined
Twitter automation earlier this session.

Instead, this scans the whole Arc chain directly via Alchemy's
alchemy_getAssetTransfers (fromAddress = the null address, no toAddress
filter) using a block-range watermark, and posts a deliberately reduced-
confidence alert: no floor/verified/score, just "this contract is minting
right now" plus a link to arc-scan.org.
"""
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


def alchemy_rpc_url():
    return f"https://arc-mainnet.g.alchemy.com/v2/{settings.alchemy_api_key}"


async def test_arc_mint_radar_skips_without_an_alchemy_key():
    settings.alchemy_api_key = ""
    result = await main._arc_mint_radar_sweep(object(), deadline=main.time.time() + 30)
    assert result == {"configured": False}


async def test_arc_mint_radar_reports_when_arc_is_not_enabled():
    settings.alchemy_api_key = "key"

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            # Mirrors the real, live-confirmed Alchemy response when a
            # network isn't toggled on for this app yet.
            return FakeRes(403, {"jsonrpc": "2.0", "id": 1, "error": {
                "code": -32600, "message": "ARC_MAINNET is not enabled for this app.",
            }})

    try:
        result = await main._arc_mint_radar_sweep(FakeClient(), deadline=main.time.time() + 30)
    finally:
        settings.alchemy_api_key = ""
    assert result == {"checked": 0, "new_contracts": 0, "posted": 0, "skipped": "arc_not_enabled_or_unreachable"}


async def test_arc_mint_radar_seeds_a_lookback_window_on_first_run():
    settings.alchemy_api_key = "key"
    calls = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            if not isinstance(json, dict):
                return FakeRes(200, {})  # a Supabase write (nft_alert_state), not an Alchemy RPC call
            method = json["method"]
            calls.append((method, json["params"][0]))
            if method == "eth_blockNumber":
                return FakeRes(200, {"result": "0x2710"})  # 10000
            if method == "alchemy_getAssetTransfers":
                return FakeRes(200, {"result": {"transfers": []}})
            raise AssertionError(f"unexpected method {method}")

        async def get(self, url, headers=None, params=None):
            if "nft_alert_state" in url:
                return FakeRes(200, [])  # no watermark yet
            return FakeRes(200, [])

    try:
        result = await main._arc_mint_radar_sweep(FakeClient(), deadline=main.time.time() + 30)
    finally:
        settings.alchemy_api_key = ""

    assert result["from_block"] == 10000 - main._ARC_MINT_RADAR_LOOKBACK_BLOCKS
    assert result["to_block"] == 10000
    transfer_call = [c for c in calls if c[0] == "alchemy_getAssetTransfers"][0]
    assert transfer_call[1]["fromAddress"] == main._TRACKED_WALLET_NULL_ADDRESS
    assert transfer_call[1]["fromBlock"] == hex(10000 - main._ARC_MINT_RADAR_LOOKBACK_BLOCKS)


async def test_arc_mint_radar_resumes_from_its_watermark():
    settings.alchemy_api_key = "key"
    seen_from_blocks = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            if not isinstance(json, dict):
                return FakeRes(200, {})
            method = json["method"]
            if method == "eth_blockNumber":
                return FakeRes(200, {"result": "0x2710"})  # 10000
            if method == "alchemy_getAssetTransfers":
                seen_from_blocks.append(json["params"][0]["fromBlock"])
                return FakeRes(200, {"result": {"transfers": []}})
            raise AssertionError(f"unexpected method {method}")

        async def get(self, url, headers=None, params=None):
            if "nft_alert_state" in url:
                return FakeRes(200, [{"slug": main._ARC_MINT_RADAR_WATERMARK_SLUG, "alert_type": "last_block", "last_value": 9500}])
            return FakeRes(200, [])

    try:
        result = await main._arc_mint_radar_sweep(FakeClient(), deadline=main.time.time() + 30)
    finally:
        settings.alchemy_api_key = ""

    assert result["from_block"] == 9501
    assert seen_from_blocks == [hex(9501)]


async def test_arc_mint_radar_advances_watermark_even_with_nothing_found():
    settings.alchemy_api_key = "key"
    written = {}

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            method = json["method"]
            if method == "eth_blockNumber":
                return FakeRes(200, {"result": "0x2710"})
            if method == "alchemy_getAssetTransfers":
                return FakeRes(200, {"result": {"transfers": []}})
            raise AssertionError(f"unexpected method {method}")

        async def get(self, url, headers=None, params=None):
            if "nft_alert_state" in url:
                return FakeRes(200, [{"last_value": 9000}])
            return FakeRes(200, [])

    async def fake_alert_state_set(client, slug, alert_type, value):
        written[(slug, alert_type)] = value

    with patch.object(main, "_nft_alert_state_set", new=fake_alert_state_set):
        try:
            result = await main._arc_mint_radar_sweep(FakeClient(), deadline=main.time.time() + 30)
        finally:
            settings.alchemy_api_key = ""

    assert result["posted"] == 0
    assert written[(main._ARC_MINT_RADAR_WATERMARK_SLUG, "last_block")] == 10000


def _mint_transfer(contract, token_id, buyer):
    return {
        "rawContract": {"address": contract},
        "tokenId": token_id,
        "to": buyer,
        "metadata": {"blockTimestamp": "2026-09-16T12:00:00Z"},
    }


async def test_arc_mint_radar_looks_up_tracked_wallets_without_an_oversized_url():
    # Real bug, live-confirmed: a launch-day backlog window can carry
    # thousands of distinct buyers, and an address=in.(...) filter with all
    # of them crammed into one URL is exactly the oversized-URL problem
    # already fixed elsewhere in this file
    # (_alert_tracker_pending_convergence_slugs) for the identical shape of
    # problem. This must fetch the whole tracked list unconditionally and
    # intersect in Python instead, regardless of how many buyers this
    # window contains.
    settings.alchemy_api_key = "key"
    tags_get_params = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            if not isinstance(json, dict):
                return FakeRes(200, {})
            method = json["method"]
            if method == "eth_blockNumber":
                return FakeRes(200, {"result": "0x2710"})
            if method == "alchemy_getAssetTransfers":
                transfers = [_mint_transfer("0xCONTRACT1", str(i), f"0xBUYER{i}") for i in range(500)]
                return FakeRes(200, {"result": {"transfers": transfers}})
            if method == "alchemy_getContractMetadata":
                return FakeRes(200, {"result": {}})
            raise AssertionError(f"unexpected method {method}")

        async def get(self, url, headers=None, params=None):
            if "smart_wallet_tags" in url:
                tags_get_params.append(params)
                return FakeRes(200, [])
            if "nft_alert_state" in url:
                if (params or {}).get("alert_type") == "eq.last_block":
                    return FakeRes(200, [{"last_value": 9000}])
                return FakeRes(200, [])
            return FakeRes(200, [])

    async def fake_post_channel_message(*a, **k):
        return True

    with patch.object(main, "_post_channel_message", new=fake_post_channel_message):
        try:
            await main._arc_mint_radar_sweep(FakeClient(), deadline=main.time.time() + 30)
        finally:
            settings.alchemy_api_key = ""

    assert len(tags_get_params) == 1
    assert "address" not in tags_get_params[0]
    assert tags_get_params[0] == {"select": "address,tag"}


async def test_arc_mint_radar_posts_a_new_contract_and_highlights_tracked_wallets():
    settings.alchemy_api_key = "key"
    posted_embeds = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            if not isinstance(json, dict):
                return FakeRes(200, {})
            method = json["method"]
            if method == "eth_blockNumber":
                return FakeRes(200, {"result": "0x2710"})
            if method == "alchemy_getAssetTransfers":
                return FakeRes(200, {"result": {"transfers": [
                    _mint_transfer("0xCONTRACT1", "1", "0xTRACKED1"),
                    _mint_transfer("0xCONTRACT1", "2", "0xUNTRACKED"),
                ]}})
            if method == "alchemy_getContractMetadata":
                return FakeRes(200, {"result": {"name": "Cool Arc Project"}})
            raise AssertionError(f"unexpected method {method}")

        async def get(self, url, headers=None, params=None):
            if "nft_alert_state" in url:
                if (params or {}).get("alert_type") == "eq.last_block":
                    return FakeRes(200, [{"last_value": 9000}])
                return FakeRes(200, [])  # per-contract dedup check - not posted yet
            if "smart_wallet_tags" in url:
                return FakeRes(200, [{"address": "0xtracked1", "tag": "Whale"}])
            return FakeRes(200, [])

    async def fake_post_channel_message(client, channel_id, embed, content=None, components=None):
        posted_embeds.append((channel_id, embed))
        return True

    with patch.object(main, "_post_channel_message", new=fake_post_channel_message):
        try:
            result = await main._arc_mint_radar_sweep(FakeClient(), deadline=main.time.time() + 30)
        finally:
            settings.alchemy_api_key = ""

    assert result["new_contracts"] == 1
    assert result["posted"] == 1
    assert len(posted_embeds) == 1
    channel_id, embed = posted_embeds[0]
    assert channel_id == settings.discord_nft_scope_channel_id
    assert embed["title"] == "Cool Arc Project"
    assert "🏷️ Includes tracked wallet(s): Whale" in embed["description"]
    assert "0xcontract1" in embed["fields"][0]["value"]
    assert "arc-scan.org/address/0xcontract1" in embed["url"]


async def test_arc_mint_radar_never_double_posts_the_same_contract():
    settings.alchemy_api_key = "key"
    post_calls = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            if not isinstance(json, dict):
                return FakeRes(200, {})
            method = json["method"]
            if method == "eth_blockNumber":
                return FakeRes(200, {"result": "0x2710"})
            if method == "alchemy_getAssetTransfers":
                return FakeRes(200, {"result": {"transfers": [_mint_transfer("0xCONTRACT1", "1", "0xBUYER")]}})
            if method == "alchemy_getContractMetadata":
                return FakeRes(200, {"result": {}})
            raise AssertionError(f"unexpected method {method}")

        async def get(self, url, headers=None, params=None):
            if "nft_alert_state" in url:
                if (params or {}).get("alert_type") == "eq.last_block":
                    return FakeRes(200, [{"last_value": 9000}])
                return FakeRes(200, [{"last_value": 0}])  # already posted this contract before
            return FakeRes(200, [])

    async def fake_post_channel_message(*a, **k):
        post_calls.append(1)
        return True

    with patch.object(main, "_post_channel_message", new=fake_post_channel_message):
        try:
            result = await main._arc_mint_radar_sweep(FakeClient(), deadline=main.time.time() + 30)
        finally:
            settings.alchemy_api_key = ""

    assert result["posted"] == 0
    assert post_calls == []


async def test_arc_mint_radar_caps_new_contract_posts_per_cycle():
    settings.alchemy_api_key = "key"
    post_calls = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            if not isinstance(json, dict):
                return FakeRes(200, {})
            method = json["method"]
            if method == "eth_blockNumber":
                return FakeRes(200, {"result": "0x2710"})
            if method == "alchemy_getAssetTransfers":
                transfers = [_mint_transfer(f"0xCONTRACT{i}", "1", "0xBUYER") for i in range(10)]
                return FakeRes(200, {"result": {"transfers": transfers}})
            if method == "alchemy_getContractMetadata":
                return FakeRes(200, {"result": {}})
            raise AssertionError(f"unexpected method {method}")

        async def get(self, url, headers=None, params=None):
            if "nft_alert_state" in url:
                if (params or {}).get("alert_type") == "eq.last_block":
                    return FakeRes(200, [{"last_value": 9000}])
                return FakeRes(200, [])  # per-contract dedup check - none posted yet
            return FakeRes(200, [])

    async def fake_post_channel_message(*a, **k):
        post_calls.append(1)
        return True

    with patch.object(main, "_post_channel_message", new=fake_post_channel_message):
        try:
            result = await main._arc_mint_radar_sweep(FakeClient(), deadline=main.time.time() + 30)
        finally:
            settings.alchemy_api_key = ""

    assert result["new_contracts"] == 10
    assert result["posted"] == main._ARC_MINT_RADAR_MAX_NEW_CONTRACTS_PER_CYCLE
    assert len(post_calls) == main._ARC_MINT_RADAR_MAX_NEW_CONTRACTS_PER_CYCLE
