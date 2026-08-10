"""Tests for /monitor clear - both the new clear-everything mode and the
fix for a real reported bug: clearing one collection could silently
delete the wrong slug (or nothing at all) if OpenSea search re-ranked
results differently than when /monitor set first resolved that same
query, while still reporting "Cleared" as if it worked."""
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


def _clear_payload(collection=None):
    sub = {"name": "clear"}
    if collection is not None:
        sub["options"] = [{"name": "collection", "value": collection}]
    return {"data": {"options": [sub]}}


async def test_clear_all_reports_nothing_when_not_monitoring_anything():
    async def fake_monitor_list(discord_user_id):
        return []

    async def fake_price_list(discord_user_id):
        return []

    delete_calls = []

    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            delete_calls.append(url)
            return FakeRes(200)

    with patch.object(main, "_nft_monitor_list", new=fake_monitor_list), \
         patch.object(main, "_nft_price_alerts_list", new=fake_price_list), \
         patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._cmd_monitor_response(_clear_payload(), "user1")

    assert "Nothing to clear" in result["embeds"][0]["title"]
    assert delete_calls == []


async def test_clear_all_removes_every_subscription_for_that_user_only():
    async def fake_monitor_list(discord_user_id):
        return [{"slug": "a", "event_type": "floor_up"}, {"slug": "b", "event_type": "sweep"}]

    async def fake_price_list(discord_user_id):
        return [{"slug": "c", "target_price": 1.0, "direction": "below", "loop_alert": False}]

    delete_calls = []

    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            assert params == {"discord_user_id": "eq.user1"}  # no slug filter, scoped to this user only
            delete_calls.append(url)
            return FakeRes(200)

    with patch.object(main, "_nft_monitor_list", new=fake_monitor_list), \
         patch.object(main, "_nft_price_alerts_list", new=fake_price_list), \
         patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._cmd_monitor_response(_clear_payload(), "user1")

    assert "Cleared everything" in result["embeds"][0]["title"]
    assert "3" in result["embeds"][0]["description"]
    assert len(delete_calls) == 2  # both nft_watch_subscriptions and nft_price_alerts


async def test_clear_one_collection_matches_ground_truth_without_calling_search():
    async def fake_monitor_list(discord_user_id):
        return [{"slug": "pudgy-penguins-nft", "event_type": "floor_up"}]

    async def fake_price_list(discord_user_id):
        return []

    async def fail_if_called(query):
        raise AssertionError("should not need to search when a subscribed slug already matches")

    deleted_slugs = []

    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            deleted_slugs.append(params.get("slug"))
            return FakeRes(200)

    with patch.object(main, "_nft_monitor_list", new=fake_monitor_list), \
         patch.object(main, "_nft_price_alerts_list", new=fake_price_list), \
         patch.object(main, "_nft_search_core", new=fail_if_called), \
         patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._cmd_monitor_response(_clear_payload("Pudgy Penguins"), "user1")

    assert "pudgy-penguins-nft" in result["embeds"][0]["title"]
    assert deleted_slugs == ["eq.pudgy-penguins-nft", "eq.pudgy-penguins-nft"]


async def test_clear_one_collection_refuses_instead_of_deleting_the_wrong_slug():
    # The real bug: OpenSea search resolves the typed query to a
    # DIFFERENT slug than the one this user is actually subscribed under
    # (search ranking drift, or the query just doesn't match anything of
    # theirs). The old code trusted search result #1 blindly and reported
    # "Cleared" even though it deleted nothing real. This must now refuse
    # instead of silently no-op-ing while claiming success.
    async def fake_monitor_list(discord_user_id):
        return [{"slug": "azuki-elementals", "event_type": "floor_up"}]

    async def fake_price_list(discord_user_id):
        return []

    async def fake_search(query):
        return [{"slug": "beanz-official", "name": "Beanz"}]  # a DIFFERENT, unrelated slug the user never subscribed to

    delete_calls = []

    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            delete_calls.append(params)
            return FakeRes(200)

    with patch.object(main, "_nft_monitor_list", new=fake_monitor_list), \
         patch.object(main, "_nft_price_alerts_list", new=fake_price_list), \
         patch.object(main, "_nft_search_core", new=fake_search), \
         patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._cmd_monitor_response(_clear_payload("Beanz"), "user1")

    assert "Not currently monitoring" in result["embeds"][0]["title"]
    assert delete_calls == []  # nothing deleted - no false "Cleared" claim


async def test_clear_one_collection_falls_back_to_search_when_it_agrees_with_subscription():
    async def fake_monitor_list(discord_user_id):
        return [{"slug": "some-obscure-slug-9f2", "event_type": "sweep"}]

    async def fake_price_list(discord_user_id):
        return []

    async def fake_search(query):
        # Search resolves the typed name to the SAME slug the user is
        # actually subscribed to - safe to trust in this case.
        return [{"slug": "some-obscure-slug-9f2", "name": "Some Obscure Project"}]

    deleted_slugs = []

    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            deleted_slugs.append(params.get("slug"))
            return FakeRes(200)

    with patch.object(main, "_nft_monitor_list", new=fake_monitor_list), \
         patch.object(main, "_nft_price_alerts_list", new=fake_price_list), \
         patch.object(main, "_nft_search_core", new=fake_search), \
         patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._cmd_monitor_response(_clear_payload("Some Obscure Project"), "user1")

    assert "some-obscure-slug-9f2" in result["embeds"][0]["title"]
    assert deleted_slugs == ["eq.some-obscure-slug-9f2", "eq.some-obscure-slug-9f2"]


def test_fuzzy_slug_match_ignores_punctuation_case_and_spacing():
    assert main._fuzzy_slug_match("Pudgy Penguins", "pudgypenguins") is True
    assert main._fuzzy_slug_match("pudgy-penguins", "PudgyPenguinsNFT") is True
    assert main._fuzzy_slug_match("azuki", "azuki-elementals") is True
    assert main._fuzzy_slug_match("completely different", "azuki") is False
    assert main._fuzzy_slug_match("", "azuki") is False
    assert main._fuzzy_slug_match("azuki", "") is False
