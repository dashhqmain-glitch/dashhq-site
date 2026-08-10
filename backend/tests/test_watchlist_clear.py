"""Tests for /watchlist clear - bulk-removes everything on a citizen's own
personal watchlist in one shot, instead of one `/watchlist remove` per
collection."""
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
        pass


async def test_clear_returns_zero_and_skips_delete_when_already_empty():
    delete_calls = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])

        async def delete(self, url, headers=None, params=None):
            delete_calls.append(params)
            return FakeRes(200)

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        count = await main._discord_watchlist_clear("user1")

    assert count == 0
    assert delete_calls == []  # no wasted DELETE call against an already-empty list


async def test_clear_deletes_all_rows_for_that_user_only_and_returns_count():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            assert params["discord_user_id"] == "eq.user1"
            return FakeRes(200, [{"slug": "a"}, {"slug": "b"}, {"slug": "c"}])

        async def delete(self, url, headers=None, params=None):
            # Scoped to this user only - no slug filter, but the user
            # filter must still be present so this can never touch
            # another citizen's watchlist rows.
            assert params == {"discord_user_id": "eq.user1"}
            return FakeRes(200)

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        count = await main._discord_watchlist_clear("user1")

    assert count == 3


async def test_cmd_watchlist_clear_reports_empty_watchlist_without_announcing():
    async def fake_clear(discord_user_id):
        return 0

    announced = []

    async def fake_announce(discord_user_id, name, verb, check):
        announced.append((discord_user_id, name, verb, check))

    payload = {"data": {"options": [{"name": "clear"}]}}
    with patch.object(main, "_discord_watchlist_clear", new=fake_clear), \
         patch.object(main, "_announce_watchlist_change", new=fake_announce):
        result = await main._cmd_watchlist(payload, "user1")

    assert "empty" in result["description"].lower()
    assert announced == []


async def test_cmd_watchlist_clear_removes_everything_and_announces_in_channel():
    settings.discord_nft_monitor_channel_id = "123"

    async def fake_clear(discord_user_id):
        assert discord_user_id == "user1"
        return 4

    announced = []

    async def fake_announce(discord_user_id, name, verb, check):
        announced.append((discord_user_id, name, verb, check))

    payload = {"data": {"options": [{"name": "clear"}]}}
    with patch.object(main, "_discord_watchlist_clear", new=fake_clear), \
         patch.object(main, "_announce_watchlist_change", new=fake_announce):
        result = await main._cmd_watchlist(payload, "user1")

    assert "4" in result["description"]
    assert announced == [("user1", "4 collections", "cleared", "")]


async def test_missing_subcommand_message_mentions_clear():
    result = await main._cmd_watchlist({"data": {"options": []}}, "user1")
    assert "clear" in result["description"]
