"""Tests for /cron/check-collection-minters - answers "how many of our
tracked wallets actually minted this collection" directly and completely,
one real Alchemy query for the whole contract's mint history cross-
referenced against the full tracked list, rather than checking wallets
one at a time or trusting an unverified headcount.
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


async def test_check_collection_minters_requires_cron_secret():
    settings.cron_secret = ""
    try:
        try:
            await main.check_collection_minters(FakeRequest("Bearer anything"), contract="0xabc")
            assert False, "should have raised"
        except main.HTTPException as e:
            assert e.status_code == 401
    finally:
        settings.cron_secret = "test-cron-secret"


async def test_check_collection_minters_skips_without_alchemy_key():
    settings.cron_secret = "s"
    settings.alchemy_api_key = ""
    try:
        result = await main.check_collection_minters(FakeRequest("Bearer s"), contract="0xabc")
    finally:
        settings.cron_secret = "test-cron-secret"
    assert result == {"configured": False}


async def test_check_collection_minters_reports_bad_chain():
    settings.cron_secret = "s"
    settings.alchemy_api_key = "key"
    try:
        result = await main.check_collection_minters(FakeRequest("Bearer s"), contract="0xabc", chain="not-a-real-chain")
    finally:
        settings.cron_secret = "test-cron-secret"
        settings.alchemy_api_key = ""
    assert result["error"].startswith("no subdomain mapping")


async def test_check_collection_minters_finds_tracked_wallets_among_minters():
    settings.cron_secret = "s"
    settings.alchemy_api_key = "key"

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {"jsonrpc": "2.0", "result": {"transfers": [
                {"to": "0xTRACKED1"}, {"to": "0xTRACKED2"}, {"to": "0xUNTRACKED"},
            ]}})

        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [
                {"address": "0xtracked1", "tag": "Degen"},
                {"address": "0xtracked2", "tag": "Whale"},
                {"address": "0xtracked2", "tag": "KOL"},
            ])

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.check_collection_minters(FakeRequest("Bearer s"), contract="0xCONTRACT", chain="robinhood")
    finally:
        settings.cron_secret = "test-cron-secret"
        settings.alchemy_api_key = ""

    assert result["total_minters_found"] == 3
    assert result["tracked_minters_count"] == 2
    assert result["tracked_minters"] == {"0xtracked1": ["Degen"], "0xtracked2": ["Whale", "KOL"]}


async def test_check_collection_minters_follows_pagination():
    settings.cron_secret = "s"
    settings.alchemy_api_key = "key"
    calls = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            params = json["params"][0]
            calls.append(params.get("pageKey"))
            if params.get("pageKey") is None:
                return FakeRes(200, {"result": {"transfers": [{"to": "0xa"}], "pageKey": "next"}})
            return FakeRes(200, {"result": {"transfers": [{"to": "0xb"}]}})

        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.check_collection_minters(FakeRequest("Bearer s"), contract="0xcontract", chain="ethereum")
    finally:
        settings.cron_secret = "test-cron-secret"
        settings.alchemy_api_key = ""

    assert calls == [None, "next"]
    assert result["total_minters_found"] == 2


async def test_check_collection_minters_surfaces_a_real_alchemy_error():
    settings.cron_secret = "s"
    settings.alchemy_api_key = "key"

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            return FakeRes(500, {}, text_data="boom")

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.check_collection_minters(FakeRequest("Bearer s"), contract="0xcontract", chain="ethereum")
    finally:
        settings.cron_secret = "test-cron-secret"
        settings.alchemy_api_key = ""

    assert "500" in result["error"]
