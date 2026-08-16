"""Tests for the /monitor set dropdown's clear-by-deselecting-everything
path - the real reported bug: it only ever deleted nft_watch_subscriptions,
never nft_price_alerts, while its own confirmation message claimed "you
won't get any /monitor alerts for this collection." A /monitor price
target is a /monitor alert too, so someone who set one and later
"cleared" through this dropdown kept getting DM'd forever, with the bot
having told them it was gone."""
from unittest.mock import patch

import main


class FakeRes:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


def _select_payload(slug, values):
    return {
        "data": {"custom_id": f"monitor_select:{slug}", "values": values},
        "member": {"user": {"id": "user1"}},
    }


async def test_deselecting_everything_also_clears_price_targets_for_that_slug():
    calls = []

    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            calls.append((url, params))
            if url.endswith("/nft_price_alerts"):
                return FakeRes(200, json_data=[{"discord_user_id": "user1", "slug": "azuki", "target_price": 1.0}])
            return FakeRes(200)

        async def post(self, url, headers=None, json=None):
            calls.append((url, json))
            return FakeRes(200)

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main.settings, "discord_nft_monitor_channel_id", ""):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_monitor_select(_select_payload("azuki", []))

    price_deletes = [c for c in calls if c[0].endswith("/nft_price_alerts")]
    assert len(price_deletes) == 1
    assert price_deletes[0][1] == {"discord_user_id": "eq.user1", "slug": "eq.azuki"}
    desc = result["data"]["embeds"][0]["description"]
    assert "price target" in desc.lower()
    assert "won't get any /monitor alerts" in desc


async def test_deselecting_everything_with_no_price_targets_reports_plain_clear():
    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            if url.endswith("/nft_price_alerts"):
                return FakeRes(200, json_data=[])  # nothing to clear
            return FakeRes(200)

        async def post(self, url, headers=None, json=None):
            return FakeRes(200)

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main.settings, "discord_nft_monitor_channel_id", ""):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_monitor_select(_select_payload("azuki", []))

    desc = result["data"]["embeds"][0]["description"]
    assert desc == "Cleared - you won't get any /monitor alerts for this collection."


async def test_selecting_events_does_not_touch_price_alerts():
    calls = []

    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            calls.append(url)
            return FakeRes(200)

        async def post(self, url, headers=None, json=None):
            calls.append(url)
            return FakeRes(200)

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main.settings, "discord_nft_monitor_channel_id", ""):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_monitor_select(_select_payload("azuki", ["floor_up"]))

    assert not any(url.endswith("/nft_price_alerts") for url in calls)
    desc = result["data"]["embeds"][0]["description"]
    assert "Floor Price Up" in desc


async def test_price_alert_clear_failure_does_not_break_the_response():
    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            if url.endswith("/nft_price_alerts"):
                raise main.httpx.HTTPError("boom")
            return FakeRes(200)

        async def post(self, url, headers=None, json=None):
            return FakeRes(200)

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main.settings, "discord_nft_monitor_channel_id", ""):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_monitor_select(_select_payload("azuki", []))

    # Still responds successfully even though the price-alert cleanup failed -
    # a Supabase hiccup on one table shouldn't turn a working clear into an
    # error for the user. The failure itself is logged (see main.py), not
    # silently discarded.
    assert result["data"]["embeds"][0]["title"] == "🔔 Monitor settings saved"
