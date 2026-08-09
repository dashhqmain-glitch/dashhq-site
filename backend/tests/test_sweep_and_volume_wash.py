"""Sweep and volume-spike detectors both had the same blind spot: neither
originally verified the trades behind an alert were real. Both now share
the _analyze_wash_trading module - these tests lock that in."""
import time
from unittest.mock import patch

import main


def sale(buyer, seller, token_id="1"):
    return {
        "buyer": buyer, "seller": seller, "nft": {"identifier": token_id},
        "event_timestamp": time.time() - 60,
        "payment": {"quantity": "1000000000000000000", "decimals": 18, "symbol": "ETH"},
    }


async def test_detect_sweep_finds_genuine_sweep():
    async def fake_get(client, path, params=None):
        return {"asset_events": [sale("whale", f"seller{i}", str(i)) for i in range(9)]}

    with patch.object(main, "_opensea_get", new=fake_get):
        async with main.httpx.AsyncClient() as client:
            result = await main._detect_sweep(client, "test-slug")
    assert result is not None
    assert result["uniqueSellers"] == 9


async def test_detect_sweep_rejects_self_trade_wash_pattern():
    async def fake_get(client, path, params=None):
        return {"asset_events": [sale("A", "A", "1")] * 9}

    with patch.object(main, "_opensea_get", new=fake_get):
        async with main.httpx.AsyncClient() as client:
            result = await main._detect_sweep(client, "test-slug")
    assert result is None


def _watchlist_alert_mocks(events_fetcher):
    """Common patch set for exercising _nft_poll_watchlist_alerts' volume
    spike path in isolation - everything except the wash-trade fetch and
    the alert-posting sink is a plain stub."""
    c = {"name": "TestColl", "slug": "test", "floor": 0.05, "vol1d": 5.0, "totalSupply": 500,
         "owners": 300, "sales24h": 20, "symbol": "ETH", "image": None, "chain": "ethereum"}
    history = [{"floor": 0.05, "total_supply": 500, "volume_1d": 1.0} for _ in range(10)]

    async def fake_dm_subscribers(client, slug, event_type, embed):
        return None

    async def fake_dm_watchlist_owners(client, slug, embed):
        return None

    async def fake_alert_state_get(client, slug, alert_type):
        return None

    async def fake_alert_state_set(client, slug, alert_type, value):
        return None

    async def fake_nft_collection_core(slug):
        return c

    async def fake_recent_snapshots(client, slug, limit=50):
        return history

    async def fake_store_snapshot(client, coll):
        return None

    async def fake_check_price_alerts(client, slug, coll):
        return None

    async def fake_detect_sweep(client, slug):
        return None

    async def fake_tracked_slugs(client):
        return ["test"]

    return dict(
        _dm_subscribers=fake_dm_subscribers,
        _dm_watchlist_owners=fake_dm_watchlist_owners,
        _nft_alert_state_get=fake_alert_state_get,
        _nft_alert_state_set=fake_alert_state_set,
        _nft_collection_core=fake_nft_collection_core,
        _nft_recent_snapshots=fake_recent_snapshots,
        _nft_store_snapshot=fake_store_snapshot,
        _check_price_alerts=fake_check_price_alerts,
        _detect_sweep=fake_detect_sweep,
        _nft_poll_tracked_slugs=fake_tracked_slugs,
        _opensea_get=events_fetcher,
    )


async def test_volume_spike_suppressed_when_wash_tainted():
    async def fake_wash_events(client, path, params=None):
        return {"asset_events": [sale("A", "A", str(i)) for i in range(10)]}

    posted = []
    async def fake_post_nft_alert(client, channel_id, embed):
        posted.append(embed["title"])
        return True

    mocks = _watchlist_alert_mocks(fake_wash_events)
    mocks["_post_nft_alert"] = fake_post_nft_alert
    patchers = [patch.object(main, name, new=fn) for name, fn in mocks.items()]
    for p in patchers:
        p.start()
    try:
        async with main.httpx.AsyncClient() as client:
            alerted = await main._nft_poll_watchlist_alerts(client)
    finally:
        for p in patchers:
            p.stop()

    assert not any("Volume Spike" in t for t in posted)
    assert not any("volume_spike" in a for a in alerted)


async def test_volume_spike_fires_when_organic():
    async def fake_clean_events(client, path, params=None):
        return {"asset_events": [sale(f"b{i}", f"s{i}", str(i)) for i in range(10)]}

    posted = []
    async def fake_post_nft_alert(client, channel_id, embed):
        posted.append(embed["title"])
        return True

    mocks = _watchlist_alert_mocks(fake_clean_events)
    mocks["_post_nft_alert"] = fake_post_nft_alert
    patchers = [patch.object(main, name, new=fn) for name, fn in mocks.items()]
    for p in patchers:
        p.start()
    try:
        async with main.httpx.AsyncClient() as client:
            alerted = await main._nft_poll_watchlist_alerts(client)
    finally:
        for p in patchers:
            p.stop()

    assert any("Volume Spike" in t for t in posted)
    assert any("volume_spike" in a for a in alerted)
