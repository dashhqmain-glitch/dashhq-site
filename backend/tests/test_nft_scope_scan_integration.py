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
    calls_by_order = {"seven_day_volume": 0, "twenty_four_hour_volume": 0, "twenty_four_hour_sales": 0}

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            if params["order_by"] == "created_date":
                return {"collections": []}
            if params["chain"] != "ethereum":
                return {"collections": []}
            calls_by_order[params["order_by"]] += 1
            if params["order_by"] == "seven_day_volume":
                return {"collections": [{"collection": "only-in-7d"}]}
            if params["order_by"] == "twenty_four_hour_volume":
                return {"collections": [{"collection": "only-in-24h-volume"}]}
            if params["order_by"] == "twenty_four_hour_sales":
                return {"collections": [{"collection": "only-in-24h-sales"}]}
        if path.startswith("/events/collection/"):
            return {"asset_events": []}
        raise AssertionError(f"unexpected {path} {params}")

    async def fake_alert_state_get(client, slug, alert_type):
        return None

    async def fake_alert_state_set(client, slug, alert_type, value):
        return None

    async def fake_collection_core(slug):
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
    assert calls_by_order["twenty_four_hour_sales"] == 1


async def test_trending_slot_is_not_structurally_biased_toward_any_single_discovery_source():
    # The reserved trending slot only fits ONE winner per chain per
    # cycle, so real fairness means: across many cycles, no single
    # source (volume-ranked vs sales-count-ranked) should always win the
    # tie. Each source offers a distinct, equally-strong candidate;
    # over enough trials, more than one of them should win at least
    # once, proving the source order isn't fixed/favoring one over the
    # others (which is exactly what shuffling the source order fixes).
    _config_channel()

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            if params["order_by"] == "created_date" or params["chain"] != "ethereum":
                return {"collections": []}
            return {"collections": [{"collection": f"from-{params['order_by']}"}]}
        if path.startswith("/events/collection/"):
            return {"asset_events": []}
        raise AssertionError(f"unexpected {path} {params}")

    async def fake_alert_state_get(client, slug, alert_type):
        return None

    async def fake_alert_state_set(client, slug, alert_type, value):
        return None

    async def fake_collection_core(slug):
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

    winners = set()
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
        for _ in range(30):
            async with main.httpx.AsyncClient() as client:
                posted = await main._nft_scope_scan(client)
            winners.update(posted)

    assert len(winners) > 1, f"same source won every single trial - source order isn't actually being shuffled: {winners}"


async def test_trending_pass_is_not_structurally_biased_toward_any_single_chain():
    # The bug this fixes: chains used to be scanned in a fixed sequential
    # order, and the FIRST chain in the list could burn the entire
    # cross-chain scan budget on its own candidates before the loop ever
    # reached chain 2 - so every chain after the first effectively never
    # got evaluated, cycle after cycle. Each chain here offers one
    # distinct, equally-strong candidate; over enough trials, more than
    # one chain's candidate should win, proving chain order is genuinely
    # shuffled and the budget is spread across chains, not camped on
    # whichever one is listed first.
    _config_channel()

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            if params["order_by"] == "created_date":
                return {"collections": []}
            if params["order_by"] != "seven_day_volume":
                return {"collections": []}
            return {"collections": [{"collection": f"from-{params['chain']}"}]}
        if path.startswith("/events/collection/"):
            return {"asset_events": []}
        raise AssertionError(f"unexpected {path} {params}")

    async def fake_alert_state_get(client, slug, alert_type):
        return None

    async def fake_alert_state_set(client, slug, alert_type, value):
        return None

    async def fake_collection_core(slug):
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

    winners = set()
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
        for _ in range(30):
            async with main.httpx.AsyncClient() as client:
                posted = await main._nft_scope_scan(client)
            winners.update(posted)

    assert len(winners) > 1, f"same chain won every single trial - chain order isn't actually being shuffled: {winners}"


async def test_trending_pass_spreads_across_multiple_chains_within_one_cycle():
    # Stronger than rotation-over-time: with several chains each offering
    # a genuinely qualifying candidate and enough surge-mode slots to fit
    # more than one, a SINGLE cycle should be able to post candidates from
    # more than one chain - not exhaust the whole budget on one chain
    # before ever reaching the others.
    _config_channel()

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            if params["order_by"] == "created_date":
                return {"collections": []}
            if params["order_by"] != "seven_day_volume":
                return {"collections": []}
            return {"collections": [{"collection": f"from-{params['chain']}"}]}
        if path.startswith("/events/collection/"):
            return {"asset_events": []}
        raise AssertionError(f"unexpected {path} {params}")

    async def fake_alert_state_get(client, slug, alert_type):
        return None  # nothing rate-limited -> healthy -> surge slot count

    async def fake_alert_state_set(client, slug, alert_type, value):
        return None

    async def fake_collection_core(slug):
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
            posted = await main._nft_scope_scan(client)

    source_chains = {slug.removeprefix("from-") for slug in posted}
    assert len(source_chains) > 1, f"every post in this single cycle came from the same chain: {posted}"


async def test_trending_scan_budget_bounds_worst_case_api_cost():
    # A pushed-down cooldown is only safe if a cycle where NOTHING clears
    # the bar can't blow through OpenSea's shared free-tier budget - this
    # verifies the hard per-cycle evaluation cap actually holds even
    # against a huge, entirely-unqualified candidate pool (10 per source
    # x 3 sources x 4 chains = up to 120 unique candidates available).
    _config_channel()

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            if params["order_by"] == "created_date":
                return {"collections": []}
            prefix = f"{params['chain']}-{params['order_by']}"
            return {"collections": [{"collection": f"{prefix}-{i}"} for i in range(10)]}
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
        return {
            "slug": slug, "name": slug, "floor": 0, "totalSupply": None, "owners": 0, "sales24h": 0,
            "vol1d": 0, "symbol": "ETH", "twitter": None, "discord": None, "website": None,
            "description": "", "category": None, "verified": False, "offerDecimals": None,
        }

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
            posted = await main._nft_scope_scan(client)

    # _nft_alert_state_get is faked to return None for every lookup
    # (including the __opensea__/rate_limited health check), so this runs
    # in the "healthy" surge branch - the cap that actually applies here
    # is the surge-mode budget, not the raw base constant.
    assert len(evaluated) <= main._nft_scope_pass_limits(True)["trending_scan_budget"]
    assert posted == []


async def test_healthy_opensea_posts_more_than_one_when_genuine_demand_exists():
    # The "auto-scale up" half: with no recent real 429 and several
    # distinct candidates that ALL genuinely clear every gate on their
    # own merits, the trending pass should post more than the base
    # single-slot limit - up to the surge ceiling - in one cycle.
    _config_channel()

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            if params["order_by"] == "created_date":
                return {"collections": []}
            if params["order_by"] == "seven_day_volume":
                return {"collections": [{"collection": f"strong-{params['chain']}"}]}
            return {"collections": []}
        if path.startswith("/events/collection/"):
            return {"asset_events": []}
        raise AssertionError(f"unexpected {path}")

    async def fake_alert_state_get(client, slug, alert_type):
        return None  # nothing rate-limited, nothing cooled-down-blocked

    async def fake_alert_state_set(client, slug, alert_type, value):
        return None

    async def fake_collection_core(slug):
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
            posted = await main._nft_scope_scan(client)

    assert len(posted) == main._NFT_SCOPE_SURGE_MAX_POSTS
    assert len(posted) > main._NFT_SCOPE_TRENDING_MAX_POSTS


async def test_recent_real_429_forces_scan_back_to_conservative_base_limits():
    # The "auto-scale down" half - same exact multi-candidate demand as
    # above, but a real 429 was recorded moments ago. Even though several
    # candidates genuinely qualify, only the base (1-per-pass) limit
    # should post - the system protects the shared OpenSea budget
    # automatically, with no manual toggle involved.
    _config_channel()

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            if params["order_by"] == "created_date":
                return {"collections": []}
            if params["order_by"] == "seven_day_volume":
                return {"collections": [{"collection": f"strong-{params['chain']}"}]}
            return {"collections": []}
        if path.startswith("/events/collection/"):
            return {"asset_events": []}
        raise AssertionError(f"unexpected {path}")

    async def fake_alert_state_get(client, slug, alert_type):
        if (slug, alert_type) == ("__opensea__", "rate_limited"):
            return {"last_alerted_at": datetime.now(timezone.utc).isoformat()}
        return None

    async def fake_alert_state_set(client, slug, alert_type, value):
        return None

    async def fake_collection_core(slug):
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
            posted = await main._nft_scope_scan(client)

    assert len(posted) == main._NFT_SCOPE_TRENDING_MAX_POSTS
    assert len(posted) < main._NFT_SCOPE_SURGE_MAX_POSTS


async def test_surge_mode_never_loosens_whether_a_candidate_qualifies():
    # Surge mode must only raise HOW MANY posts a pass can make, never
    # touch WHETHER something qualifies - a scam-shaped collection stays
    # blocked even with every slot wide open and OpenSea fully healthy.
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

    async def fake_alert_state_get(client, slug, alert_type):
        return None  # healthy - every slot is at its widest

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
         patch.object(main, "_post_channel_message", new=fake_post):
        async with main.httpx.AsyncClient() as client:
            posted = await main._nft_scope_scan(client)

    assert "scam-trending" not in posted
    assert posted == []


async def test_trending_pass_rotates_off_a_recent_winner_instead_of_reposting_it():
    # The bug this fixes: with only the 5-min SCAN cooldown, a candidate
    # that clears the bar keeps winning the single reserved slot every
    # cycle it's re-evaluated, so the same one or two projects repeat
    # over and over even though plenty of other qualifying candidates
    # exist. The separate, much longer POST cooldown means a winner that
    # just posted steps aside next cycle so a different qualifying
    # candidate actually gets the slot.
    _config_channel()
    state: dict = {}  # shared across both calls, like the real Supabase-backed store

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            if params["order_by"] == "created_date" or params["chain"] != "ethereum":
                return {"collections": []}
            if params["order_by"] != "seven_day_volume":
                return {"collections": []}
            return {"collections": [{"collection": s} for s in ("winner-1", "winner-2", "winner-3", "winner-4")]}
        if path.startswith("/events/collection/"):
            return {"asset_events": []}
        raise AssertionError(f"unexpected {path}")

    async def fake_alert_state_get(client, slug, alert_type):
        if (slug, alert_type) == ("__opensea__", "rate_limited"):
            # Forced unhealthy so the reserved slot count is deterministic
            # (base limit of 1), isolating the rotation behavior itself.
            return {"last_alerted_at": datetime.now(timezone.utc).isoformat()}
        return state.get((slug, alert_type))

    async def fake_alert_state_set(client, slug, alert_type, value):
        state[(slug, alert_type)] = {"last_alerted_at": datetime.now(timezone.utc).isoformat()}

    async def fake_collection_core(slug):
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
            first_posted = await main._nft_scope_scan(client)
        # Clear the scan-cooldown bookkeeping only (simulates enough time
        # passing to re-scan) while leaving the post-cooldown intact -
        # that's the exact condition this fix targets.
        state = {k: v for k, v in state.items() if k[1] != "nft_scope_trending_scan"}
        async with main.httpx.AsyncClient() as client:
            second_posted = await main._nft_scope_scan(client)

    assert first_posted == ["winner-1"]
    assert "winner-1" not in second_posted
    assert second_posted == ["winner-2"]


async def test_momentum_pass_is_not_structurally_biased_toward_alphabetically_early_slugs():
    # _nft_poll_tracked_slugs returns sorted(slugs) - a fixed alphabetical
    # order every cycle. Truncating and evaluating straight from that
    # order used to mean alphabetically-early tracked collections could
    # permanently monopolize the scan-limit cutoff and the momentum slot -
    # the same structural bias the trending pass had before its own
    # chain-order fix, just for /watchlist and /monitor's tracked set
    # instead of open discovery. Every candidate here is equally strong;
    # over enough trials, more than one should win.
    _config_channel()

    async def fake_opensea_get(client, path, params=None):
        if path == "/collections":
            return {"collections": []}
        if path.startswith("/events/collection/"):
            return {"asset_events": []}
        raise AssertionError(f"unexpected {path}")

    # Deliberately alphabetical, and larger than a shrunk scan limit -
    # the old code would truncate to the same leading slice every time.
    all_slugs = [f"tracked-{c}" for c in "abcdefghij"]

    async def fake_tracked_slugs(client):
        return list(all_slugs)  # fresh copy - the fix shuffles in place

    async def fake_alert_state_get(client, slug, alert_type):
        if (slug, alert_type) == ("__opensea__", "rate_limited"):
            return {"last_alerted_at": datetime.now(timezone.utc).isoformat()}  # forced unhealthy -> base limits, momentum_max=1
        return None

    async def fake_alert_state_set(client, slug, alert_type, value):
        return None

    async def fake_collection_core(slug):
        return _strong_collection(slug)

    async def fake_top_offer(client, slug):
        return None

    async def fake_recent_snapshots(client, slug, limit=30):
        return [{"floor": 0.05, "volume_1d": 0.5, "owners": 300}] * main._NFT_SCOPE_MOMENTUM_MIN_SNAPSHOTS

    async def fake_post(client, channel_id, embed, content=None):
        return True

    async def fake_wash_clean(client, slug):
        return True

    async def fake_seen_has(client, slug):
        return False

    async def fake_mark_seen(client, slug):
        return None

    winners = set()
    with patch.object(main, "_NFT_SCOPE_MOMENTUM_SCAN_LIMIT", 2), \
         patch.object(main, "_opensea_get", new=fake_opensea_get), \
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
        for _ in range(30):
            async with main.httpx.AsyncClient() as client:
                posted = await main._nft_scope_scan(client)
            winners.update(posted)

    assert len(winners) > 1, f"same tracked slug won every single trial - alphabetical order isn't actually being shuffled: {winners}"
