"""Tests for the NFT Scope follow-up pass - the "did the call keep proving
right" check that runs once, 25-40 min after an original post, against the
nft_scope_any_post state every post already writes as part of its own
cooldown bookkeeping."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import main
from config import settings


def _config_channel():
    settings.discord_nft_scope_channel_id = "1536099071272550470"


def _due_row(slug, floor_then, minutes_ago=30):
    posted_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {"slug": slug, "last_value": floor_then, "last_alerted_at": posted_at.isoformat()}


def _collection(slug, floor):
    return {"slug": slug, "name": "Cooking Collection", "floor": floor, "symbol": "ETH", "openseaUrl": "x", "image": None}


async def test_followup_posts_when_floor_climbed_past_threshold():
    _config_channel()
    followup_state = {}

    async def fake_alert_state_get(client, slug, alert_type):
        return followup_state.get((slug, alert_type))

    async def fake_alert_state_set(client, slug, alert_type, value):
        followup_state[(slug, alert_type)] = value

    async def fake_due(client):
        return [_due_row("mooning", floor_then=0.10)]

    async def fake_collection_core(slug):
        return _collection(slug, floor=0.15)  # +50%, well past the 10% bar

    async def fake_rapid(client, slug):
        return None

    async def fake_wash_clean(client, slug):
        return True

    posted = []

    async def fake_post(client, channel_id, embed, content=None):
        posted.append(embed)
        return True

    with patch.object(main, "_nft_scope_due_followups", new=fake_due), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main, "_detect_rapid_activity", new=fake_rapid), \
         patch.object(main, "_nft_scope_clears_wash_check", new=fake_wash_clean), \
         patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get), \
         patch.object(main, "_nft_alert_state_set", new=fake_alert_state_set), \
         patch.object(main, "_post_channel_message", new=fake_post):
        result = await main._nft_scope_followup_pass(main.httpx.AsyncClient())

    assert result == ["mooning"]
    assert len(posted) == 1
    assert "Still Cooking" in posted[0]["title"]
    assert "50%" in posted[0]["description"]
    # Marked done so a later cycle never re-processes the same post.
    assert followup_state[("mooning", "nft_scope_followup_sent")] == 0.15


async def test_followup_stays_silent_on_a_fizzle_but_still_marks_it_done():
    _config_channel()
    followup_state = {}

    async def fake_alert_state_get(client, slug, alert_type):
        return followup_state.get((slug, alert_type))

    async def fake_alert_state_set(client, slug, alert_type, value):
        followup_state[(slug, alert_type)] = value

    async def fake_due(client):
        return [_due_row("flat", floor_then=0.10)]

    async def fake_collection_core(slug):
        return _collection(slug, floor=0.101)  # ~1% - nowhere near the bar

    async def fake_rapid(client, slug):
        return None

    async def fake_wash_clean(client, slug):
        return True

    posted = []

    async def fake_post(client, channel_id, embed, content=None):
        posted.append(embed)
        return True

    with patch.object(main, "_nft_scope_due_followups", new=fake_due), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main, "_detect_rapid_activity", new=fake_rapid), \
         patch.object(main, "_nft_scope_clears_wash_check", new=fake_wash_clean), \
         patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get), \
         patch.object(main, "_nft_alert_state_set", new=fake_alert_state_set), \
         patch.object(main, "_post_channel_message", new=fake_post):
        result = await main._nft_scope_followup_pass(main.httpx.AsyncClient())

    assert result == []
    assert posted == []
    # Still marked processed, so this exact post is never re-checked again.
    assert ("flat", "nft_scope_followup_sent") in followup_state


async def test_followup_skips_a_post_already_followed_up():
    _config_channel()

    async def fake_alert_state_get(client, slug, alert_type):
        return {"last_value": 1} if alert_type == "nft_scope_followup_sent" else None

    calls = []

    async def fake_due(client):
        return [_due_row("already-done", floor_then=0.10)]

    async def fake_collection_core(slug):
        calls.append(slug)
        return _collection(slug, floor=0.50)

    with patch.object(main, "_nft_scope_due_followups", new=fake_due), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get):
        result = await main._nft_scope_followup_pass(main.httpx.AsyncClient())

    assert result == []
    assert calls == []  # never even re-fetched - the whole point of the marker


async def test_followup_posts_on_sustained_rapid_activity_even_without_floor_gain():
    _config_channel()
    followup_state = {}

    async def fake_alert_state_get(client, slug, alert_type):
        return followup_state.get((slug, alert_type))

    async def fake_alert_state_set(client, slug, alert_type, value):
        followup_state[(slug, alert_type)] = value

    async def fake_due(client):
        return [_due_row("still-sweeping", floor_then=0.10)]

    async def fake_collection_core(slug):
        return _collection(slug, floor=0.10)  # flat floor

    async def fake_rapid(client, slug):
        return {"count": 6, "window_minutes": 30, "unique_buyers": 4, "unique_sellers": 3, "is_sharp": True, "sharp_count": 3, "sharp_window_minutes": 5}

    async def fake_wash_clean(client, slug):
        return True

    posted = []

    async def fake_post(client, channel_id, embed, content=None):
        posted.append(embed)
        return True

    with patch.object(main, "_nft_scope_due_followups", new=fake_due), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main, "_detect_rapid_activity", new=fake_rapid), \
         patch.object(main, "_nft_scope_clears_wash_check", new=fake_wash_clean), \
         patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get), \
         patch.object(main, "_nft_alert_state_set", new=fake_alert_state_set), \
         patch.object(main, "_post_channel_message", new=fake_post):
        result = await main._nft_scope_followup_pass(main.httpx.AsyncClient())

    assert result == ["still-sweeping"]
    assert "still moving" in posted[0]["description"].lower()


async def test_followup_skipped_when_it_fails_the_wash_check():
    _config_channel()
    followup_state = {}

    async def fake_alert_state_get(client, slug, alert_type):
        return followup_state.get((slug, alert_type))

    async def fake_alert_state_set(client, slug, alert_type, value):
        followup_state[(slug, alert_type)] = value

    async def fake_due(client):
        return [_due_row("washy", floor_then=0.10)]

    async def fake_collection_core(slug):
        return _collection(slug, floor=0.30)  # +200%, would clear the floor bar

    async def fake_rapid(client, slug):
        return None

    async def fake_wash_dirty(client, slug):
        return False  # suspicious - must not post regardless of the floor move

    posted = []

    async def fake_post(client, channel_id, embed, content=None):
        posted.append(embed)
        return True

    with patch.object(main, "_nft_scope_due_followups", new=fake_due), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main, "_detect_rapid_activity", new=fake_rapid), \
         patch.object(main, "_nft_scope_clears_wash_check", new=fake_wash_dirty), \
         patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get), \
         patch.object(main, "_nft_alert_state_set", new=fake_alert_state_set), \
         patch.object(main, "_post_channel_message", new=fake_post):
        result = await main._nft_scope_followup_pass(main.httpx.AsyncClient())

    assert result == []
    assert posted == []
