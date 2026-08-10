"""Integration tests for the full _nft_scope_scan pipeline across all three
passes (fresh mints, trending discovery, tracked-collection momentum) -
verifying they compose correctly and that widening discovery never loosens
the strict pruning rules."""
from datetime import datetime, timezone
from unittest.mock import patch

import main
from config import settings


def _config_channel():
    settings.discord_nft_scope_channel_id = "1536099071272550470"


def _strong_collection(slug):
    return {
        "slug": slug, "name": "Trending Real", "floor": 0.05, "totalSupply": 500, "owners": 300,
        "sales24h": 5, "vol1d": 0.5, "symbol": "ETH", "twitter": "x", "discord": "d", "website": "w",
        "description": "A" * 50, "category": "art", "verified": True, "offerDecimals": None,
    }


def _scam_collection(slug):
    return {
        "slug": slug, "name": "Scam Trending", "floor": 0.5, "totalSupply": 1267, "owners": 131,
        "sales24h": 1267, "vol1d": 100, "symbol": "USDG", "twitter": "x", "discord": "d", "website": "w",
        "description": "A" * 50, "category": "art", "verified": False, "offerDecimals": None,
    }


async def test_trending_pass_finds_a_strong_established_collection_outside_the_other_two_passes():
    _config_channel()

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            if params["order_by"] == "created_date":
                return {"collections": []}
            if params["order_by"] == "seven_day_volume" and params["chain"] == "ethereum":
                return {"collections": [{"collection": "trending-real"}]}
            return {"collections": []}
        if path.startswith("/events/collection/"):
            return {"asset_events": []}
        raise AssertionError(f"unexpected {path}")

    scan_state = {}

    async def fake_alert_state_get(client, slug, alert_type):
        return scan_state.get((slug, alert_type))

    async def fake_alert_state_set(client, slug, alert_type, value):
        scan_state[(slug, alert_type)] = {"last_alerted_at": datetime.now(timezone.utc).isoformat()}

    async def fake_collection_core(slug):
        return _strong_collection(slug)

    async def fake_top_offer(client, slug):
        return None

    async def fake_recent_snapshots(client, slug, limit=30):
        return []

    posted_titles = []

    async def fake_post_channel_message(client, channel_id, embed, content=None):
        posted_titles.append(embed["title"])
        return True

    async def fake_wash_clean(client, slug):
        return True

    async def fake_tracked_slugs(client):
        return []

    async def fake_seen_has(client, slug):
        return False

    async def fake_mark_seen(client, slug):
        return None

    with patch.object(main, "_opensea_get", new=fake_opensea_get), \
         patch.object(main, "_nft_mint_radar_seen_has", new=fake_seen_has), \
         patch.object(main, "_nft_mint_radar_mark_seen", new=fake_mark_seen), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main, "_opensea_get_top_offer", new=fake_top_offer), \
         patch.object(main, "_nft_recent_snapshots", new=fake_recent_snapshots), \
         patch.object(main, "_nft_scope_clears_wash_check", new=fake_wash_clean), \
         patch.object(main, "_nft_poll_tracked_slugs", new=fake_tracked_slugs), \
         patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get), \
         patch.object(main, "_nft_alert_state_set", new=fake_alert_state_set), \
         patch.object(main, "_post_channel_message", new=fake_post_channel_message):
        async with main.httpx.AsyncClient() as client:
            posted = await main._nft_scope_scan(client)

    assert "trending-real" in posted
    assert any("Trending Pick" in t for t in posted_titles)
    assert ("trending-real", "nft_scope_trending_scan") in [
        (k[0], k[1]) for k in scan_state.keys()
    ]


async def test_trending_candidate_not_rescanned_within_cooldown():
    _config_channel()
    already_scanned_at = {("trending-real", "nft_scope_trending_scan"): {"last_alerted_at": datetime.now(timezone.utc).isoformat()}}

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            if params["order_by"] == "seven_day_volume" and params["chain"] == "ethereum":
                return {"collections": [{"collection": "trending-real"}]}
            return {"collections": []}
        raise AssertionError(f"unexpected {path}")

    async def fake_alert_state_get(client, slug, alert_type):
        return already_scanned_at.get((slug, alert_type))

    collection_core_calls = []

    async def counting_collection_core(slug):
        collection_core_calls.append(slug)
        return _strong_collection(slug)

    async def fake_tracked_slugs(client):
        return []

    async def fake_seen_has(client, slug):
        return False

    with patch.object(main, "_opensea_get", new=fake_opensea_get), \
         patch.object(main, "_nft_mint_radar_seen_has", new=fake_seen_has), \
         patch.object(main, "_nft_collection_core", new=counting_collection_core), \
         patch.object(main, "_nft_poll_tracked_slugs", new=fake_tracked_slugs), \
         patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get):
        async with main.httpx.AsyncClient() as client:
            await main._nft_scope_scan(client)

    assert "trending-real" not in collection_core_calls


async def test_trending_pass_still_blocks_scam_shaped_collections():
    _config_channel()

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            if params["order_by"] == "seven_day_volume" and params["chain"] == "ethereum":
                return {"collections": [{"collection": "scam-trending"}]}
            return {"collections": []}
        if path.startswith("/events/collection/"):
            return {"asset_events": []}
        raise AssertionError(f"unexpected {path}")

    async def fake_collection_core(slug):
        return _scam_collection(slug)

    async def fake_top_offer(client, slug):
        return None

    async def fake_recent_snapshots(client, slug, limit=30):
        return []

    posted_titles = []

    async def fake_post_channel_message(client, channel_id, embed, content=None):
        posted_titles.append(embed["title"])
        return True

    async def fake_wash_clean(client, slug):
        return True

    async def fake_tracked_slugs(client):
        return []

    async def fake_seen_has(client, slug):
        return False

    async def fake_mark_seen(client, slug):
        return None

    async def fake_alert_state_get(client, slug, alert_type):
        return None

    async def fake_alert_state_set(client, slug, alert_type, value):
        return None

    with patch.object(main, "_opensea_get", new=fake_opensea_get), \
         patch.object(main, "_nft_mint_radar_seen_has", new=fake_seen_has), \
         patch.object(main, "_nft_mint_radar_mark_seen", new=fake_mark_seen), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main, "_opensea_get_top_offer", new=fake_top_offer), \
         patch.object(main, "_nft_recent_snapshots", new=fake_recent_snapshots), \
         patch.object(main, "_nft_scope_clears_wash_check", new=fake_wash_clean), \
         patch.object(main, "_nft_poll_tracked_slugs", new=fake_tracked_slugs), \
         patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get), \
         patch.object(main, "_nft_alert_state_set", new=fake_alert_state_set), \
         patch.object(main, "_post_channel_message", new=fake_post_channel_message):
        async with main.httpx.AsyncClient() as client:
            posted = await main._nft_scope_scan(client)

    assert "scam-trending" not in posted
    assert posted_titles == []


async def test_trending_pass_queries_both_7day_and_24h_volume_and_dedupes():
    # 7-day volume alone can be dominated by activity earlier in the week
    # and miss something hot RIGHT NOW - the 24h source closes that gap.
    _config_channel()
    calls_by_order = {"seven_day_volume": 0, "twenty_four_hour_volume": 0}

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            if params["order_by"] == "created_date":
                return {"collections": []}
            if params["chain"] != "ethereum":
                return {"collections": []}
            calls_by_order[params["order_by"]] += 1
            if params["order_by"] == "seven_day_volume":
                return {"collections": [{"collection": "shared"}, {"collection": "only-in-7d"}]}
            if params["order_by"] == "twenty_four_hour_volume":
                return {"collections": [{"collection": "shared"}, {"collection": "only-in-24h"}]}
        if path.startswith("/events/collection/"):
            return {"asset_events": []}
        raise AssertionError(f"unexpected {path} {params}")

    evaluated = []

    async def fake_alert_state_get(client, slug, alert_type):
        return None

    async def fake_alert_state_set(client, slug, alert_type, value):
        return None

    async def fake_collection_core(slug):
        evaluated.append(slug)
        return _strong_collection(slug)

    async def fake_top_offer(client, slug):
        return None

    async def fake_recent_snapshots(client, slug, limit=30):
        return []

    async def fake_post(client, channel_id, embed, content=None):
        return True

    async def fake_wash_clean(client, slug):
        return True

    async def fake_tracked_slugs(client):
        return []

    async def fake_seen_has(client, slug):
        return False

    async def fake_mark_seen(client, slug):
        return None

    with patch.object(main, "_opensea_get", new=fake_opensea_get), \
         patch.object(main, "_nft_mint_radar_seen_has", new=fake_seen_has), \
         patch.object(main, "_nft_mint_radar_mark_seen", new=fake_mark_seen), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main, "_opensea_get_top_offer", new=fake_top_offer), \
         patch.object(main, "_nft_recent_snapshots", new=fake_recent_snapshots), \
         patch.object(main, "_nft_scope_clears_wash_check", new=fake_wash_clean), \
         patch.object(main, "_nft_poll_tracked_slugs", new=fake_tracked_slugs), \
         patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get), \
         patch.object(main, "_nft_alert_state_set", new=fake_alert_state_set), \
         patch.object(main, "_post_channel_message", new=fake_post):
        async with main.httpx.AsyncClient() as client:
            await main._nft_scope_scan(client)

    assert calls_by_order["seven_day_volume"] == 1
    assert calls_by_order["twenty_four_hour_volume"] == 1
    assert "only-in-24h" in evaluated
    assert "only-in-7d" in evaluated
    assert evaluated.count("shared") == 1
