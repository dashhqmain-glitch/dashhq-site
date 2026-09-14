"""Tests for /cron/backfill-collection-mints - the direct follow-up to
/cron/check-collection-minters: once that's confirmed real tracked wallets
minted a collection our own detection missed (a real gap, e.g. a webhook
outage window, or a chain like Robinhood with no explorer-backstop leg),
this logs their ACTUAL on-chain mint events into nft_sale_events_log and
runs the exact same real scoring/posting pipeline every other detection
path uses - it never fabricates or hand-writes a post.
"""
from unittest import mock

import main
from config import settings


class FakeRes:
    def __init__(self, status_code=200, json_data=None, text_data=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text_data

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.httpx.HTTPStatusError("boom", request=None, response=self)


class FakeRequest:
    def __init__(self, auth=None):
        self.headers = {"authorization": auth} if auth is not None else {}


async def test_backfill_collection_mints_requires_cron_secret():
    settings.cron_secret = ""
    try:
        try:
            await main.backfill_collection_mints(FakeRequest("Bearer anything"), contract="0xabc")
            assert False, "should have raised"
        except main.HTTPException as e:
            assert e.status_code == 401
    finally:
        settings.cron_secret = "test-cron-secret"


async def test_backfill_collection_mints_skips_without_alchemy_key():
    settings.cron_secret = "s"
    settings.alchemy_api_key = ""
    try:
        result = await main.backfill_collection_mints(FakeRequest("Bearer s"), contract="0xabc")
    finally:
        settings.cron_secret = "test-cron-secret"
    assert result == {"configured": False}


async def test_backfill_collection_mints_reports_bad_chain():
    settings.cron_secret = "s"
    settings.alchemy_api_key = "key"
    try:
        result = await main.backfill_collection_mints(FakeRequest("Bearer s"), contract="0xabc", chain="not-a-real-chain")
    finally:
        settings.cron_secret = "test-cron-secret"
        settings.alchemy_api_key = ""
    assert result["error"].startswith("no subdomain mapping")


async def test_backfill_collection_mints_surfaces_a_real_alchemy_error():
    settings.cron_secret = "s"
    settings.alchemy_api_key = "key"

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            return FakeRes(500, {}, text_data="boom")

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.backfill_collection_mints(FakeRequest("Bearer s"), contract="0xcontract", chain="ethereum")
    finally:
        settings.cron_secret = "test-cron-secret"
        settings.alchemy_api_key = ""

    assert "500" in result["error"]


async def test_backfill_collection_mints_reports_unresolvable_contract():
    settings.cron_secret = "s"
    settings.alchemy_api_key = "key"

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {"result": {"transfers": [{"to": "0xtracked1"}]}})

        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"address": "0xtracked1"}])

    async def fake_resolve(client, contract, known_chain=None):
        return None

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient, \
             mock.patch.object(main, "_nft_resolve_by_contract", new=fake_resolve):
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.backfill_collection_mints(FakeRequest("Bearer s"), contract="0xcontract", chain="robinhood")
    finally:
        settings.cron_secret = "test-cron-secret"
        settings.alchemy_api_key = ""

    assert result["error"] == "could not resolve this contract to an OpenSea collection"


async def test_backfill_collection_mints_logs_only_tracked_wallets_and_posts_via_real_pipeline():
    settings.cron_secret = "s"
    settings.alchemy_api_key = "key"
    logged_events = []
    posted_calls = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            if "nft_sale_events_log" in url:
                logged_events.append(json)
                return FakeRes(200, {})
            return FakeRes(200, {"result": {"transfers": [
                {"to": "0xTRACKED1", "tokenId": "0x2a", "metadata": {"blockTimestamp": "2026-09-10T00:00:00Z"}},
                {"to": "0xUNTRACKED", "tokenId": "5", "metadata": {"blockTimestamp": "2026-09-10T00:01:00Z"}},
                {"to": "0xTRACKED1", "tokenId": None, "metadata": {"blockTimestamp": "2026-09-10T00:02:00Z"}},
            ]}})

        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"address": "0xtracked1"}])

    async def fake_resolve(client, contract, known_chain=None):
        assert known_chain == "robinhood"
        return {"slug": "founding-charter", "symbol": "FC"}

    async def fake_maybe_post_direct(client, slug, known_collection=None):
        posted_calls.append((slug, known_collection))
        return True

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient, \
             mock.patch.object(main, "_nft_resolve_by_contract", new=fake_resolve), \
             mock.patch.object(main, "_nft_scope_maybe_post_from_slug_direct", new=fake_maybe_post_direct):
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.backfill_collection_mints(
                FakeRequest("Bearer s"), contract="0xCONTRACT", chain="robinhood",
            )
    finally:
        settings.cron_secret = "test-cron-secret"
        settings.alchemy_api_key = ""

    # Only the one transfer to a tracked wallet with a valid tokenId gets logged -
    # the untracked recipient and the malformed (missing tokenId) transfer are skipped.
    assert len(logged_events) == 1
    assert logged_events[0]["buyer"] == "0xtracked1"
    assert logged_events[0]["token_id"] == "42"  # hex "0x2a" decoded
    assert logged_events[0]["slug"] == "founding-charter"

    assert posted_calls == [("founding-charter", {"slug": "founding-charter", "symbol": "FC"})]
    assert result == {
        "chain": "robinhood", "contract": "0xcontract", "slug": "founding-charter",
        "backfilled_events": 1, "posted": True,
    }


async def test_backfill_collection_mints_reports_zero_backfilled_when_no_tracked_minters():
    settings.cron_secret = "s"
    settings.alchemy_api_key = "key"

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            if "nft_sale_events_log" in url:
                assert False, "should never log an event for an untracked wallet"
            return FakeRes(200, {"result": {"transfers": [{"to": "0xUNTRACKED", "tokenId": "1", "metadata": {"blockTimestamp": "t"}}]}})

        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"address": "0xtracked1"}])

    async def fake_resolve(client, contract, known_chain=None):
        return {"slug": "some-collection"}

    async def fake_maybe_post_direct(client, slug, known_collection=None):
        return False

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient, \
             mock.patch.object(main, "_nft_resolve_by_contract", new=fake_resolve), \
             mock.patch.object(main, "_nft_scope_maybe_post_from_slug_direct", new=fake_maybe_post_direct):
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.backfill_collection_mints(FakeRequest("Bearer s"), contract="0xcontract", chain="ethereum")
    finally:
        settings.cron_secret = "test-cron-secret"
        settings.alchemy_api_key = ""

    assert result["backfilled_events"] == 0
    assert result["posted"] is False
