"""Tests for refusing to post when the database refuses writes.

Real, imminent risk: the Free Plan's database quota is 500 MB and past it
Supabase puts the project in read-only mode - every write is rejected while
reads keep working. Almost every write in main.py is fire-and-forget, so the
bot would carry on posting while silently failing to save its "already
posted" records, and each further event for a hot mint would re-post the same
alert. Both the poll cycle and the Alert Tracker now check that a write is
actually accepted first - but only a POSITIVE rejection counts, never a
network error.
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


class FakeRequest:
    def __init__(self, auth=None):
        self.headers = {"authorization": auth} if auth is not None else {}


def setup_function(_):
    main._db_writable_cache = None
    main._db_write_alert_last_sent = 0.0


def teardown_function(_):
    main._db_writable_cache = None
    main._db_write_alert_last_sent = 0.0


# ── _nft_alert_state_set now reports whether the write was accepted ──────

async def test_alert_state_set_reports_an_accepted_write():
    class FakeClient:
        async def post(self, url, headers=None, json=None):
            return FakeRes(201)

    assert await main._nft_alert_state_set(FakeClient(), "s", "t", 0) is True


async def test_alert_state_set_reports_a_rejected_write():
    # PostgREST answers a read-only transaction (SQLSTATE 25006) with 405.
    for status in (405, 400, 500):
        class FakeClient:
            async def post(self, url, headers=None, json=None, _status=status):
                return FakeRes(_status)

        assert await main._nft_alert_state_set(FakeClient(), "s", "t", 0) is False


async def test_alert_state_set_tolerates_a_client_that_returns_nothing():
    # Existing callers/tests use bare fakes; the return value is new and
    # must never break something that was ignoring it.
    class FakeClient:
        async def post(self, url, headers=None, json=None):
            return None

    assert await main._nft_alert_state_set(FakeClient(), "s", "t", 0) is True


# ── _db_writable ─────────────────────────────────────────────────────────

async def test_db_writable_is_false_only_when_a_write_is_positively_rejected():
    async def rejected(client, slug, alert_type, value):
        return False

    with patch.object(main, "_nft_alert_state_set", new=rejected):
        assert await main._db_writable(object()) is False


async def test_db_writable_is_true_when_writes_are_accepted():
    async def accepted(client, slug, alert_type, value):
        return True

    with patch.object(main, "_nft_alert_state_set", new=accepted):
        assert await main._db_writable(object()) is True


async def test_db_writable_treats_a_bare_none_result_as_writable():
    # A patched/legacy _nft_alert_state_set that returns nothing is not a rejection.
    async def returns_nothing(client, slug, alert_type, value):
        return None

    with patch.object(main, "_nft_alert_state_set", new=returns_nothing):
        assert await main._db_writable(object()) is True


async def test_db_writable_fails_open_on_a_network_error():
    # A network blip says nothing about whether writes are refused.
    async def unreachable(client, slug, alert_type, value):
        raise main.httpx.ConnectError("no route")

    with patch.object(main, "_nft_alert_state_set", new=unreachable):
        assert await main._db_writable(object()) is True


async def test_db_writable_caches_the_answer_then_probes_again_after_the_ttl():
    calls = []

    async def counted(client, slug, alert_type, value):
        calls.append((slug, alert_type))
        return False

    with patch.object(main, "_nft_alert_state_set", new=counted):
        assert await main._db_writable(object()) is False
        assert await main._db_writable(object()) is False
        assert len(calls) == 1  # second answer came from the cache

        cached_at, verdict = main._db_writable_cache
        main._db_writable_cache = (cached_at - main._DB_WRITABLE_CACHE_TTL_SECONDS - 1, verdict)
        assert await main._db_writable(object()) is False
        assert len(calls) == 2  # stale cache re-probed

    assert calls[0] == ("__db_probe__", "probe")


async def test_db_writable_recovers_on_its_own_once_writes_are_accepted_again():
    state = {"accept": False}

    async def toggled(client, slug, alert_type, value):
        return state["accept"]

    with patch.object(main, "_nft_alert_state_set", new=toggled):
        assert await main._db_writable(object()) is False
        state["accept"] = True
        cached_at, verdict = main._db_writable_cache
        main._db_writable_cache = (cached_at - main._DB_WRITABLE_CACHE_TTL_SECONDS - 1, verdict)
        assert await main._db_writable(object()) is True


# ── _alert_ops_db_not_writable ───────────────────────────────────────────

async def test_ops_alert_posts_once_then_respects_its_cooldown():
    posted = []

    async def fake_post(client, channel_id, embed, content=None, components=None):
        posted.append((channel_id, embed))
        return True

    settings.discord_ops_alert_channel_id = "ops-chan"
    try:
        with patch.object(main, "_post_channel_message", new=fake_post):
            await main._alert_ops_db_not_writable(object(), "nft-poll")
            await main._alert_ops_db_not_writable(object(), "Alert Tracker")
    finally:
        settings.discord_ops_alert_channel_id = ""

    assert len(posted) == 1
    channel_id, embed = posted[0]
    assert channel_id == "ops-chan"
    assert "read-only" in embed["description"] and "500 MB" in embed["description"]
    assert "nft-poll" in embed["description"]


async def test_ops_alert_does_not_start_its_cooldown_when_the_post_fails():
    attempts = []

    async def failing_post(client, channel_id, embed, content=None, components=None):
        attempts.append(1)
        return False

    settings.discord_ops_alert_channel_id = "ops-chan"
    try:
        with patch.object(main, "_post_channel_message", new=failing_post):
            await main._alert_ops_db_not_writable(object(), "nft-poll")
            await main._alert_ops_db_not_writable(object(), "nft-poll")
    finally:
        settings.discord_ops_alert_channel_id = ""

    assert len(attempts) == 2  # a failed alert must be retried, not silenced for an hour


async def test_ops_alert_is_a_no_op_without_a_configured_channel_and_never_raises():
    async def boom(*a, **k):
        raise RuntimeError("discord exploded")

    settings.discord_ops_alert_channel_id = ""
    await main._alert_ops_db_not_writable(object(), "nft-poll")  # nothing to send to, must not touch Discord

    settings.discord_ops_alert_channel_id = "ops-chan"
    try:
        with patch.object(main, "_post_channel_message", new=boom):
            await main._alert_ops_db_not_writable(object(), "nft-poll")  # must swallow, not propagate
    finally:
        settings.discord_ops_alert_channel_id = ""


# ── nft_poll ─────────────────────────────────────────────────────────────

_PHASES = {
    "_nft_poll_watchlist_alerts": [],
    "_alert_tracker_recheck_pending_convergences": {"pending": 0, "checked": 0, "posted": 0},
    "_tracked_wallet_watch_sweep": {"checked": 0, "mints_found": 0, "posted": 0},
    "_explorer_backstop_sweep": {"checked": 0, "mints_found": 0, "posted": 0},
    "_nft_scope_scan": [],
    "_nft_scope_followup_pass": [],
    "_alchemy_webhook_sync_addresses": None,
    "_alert_tracker_prove_due_calls": {"checked": 0, "proved": 0},
    "_alert_tracker_maybe_post_digest": False,
    "_prune_old_snapshots": True,
    "_prune_old_sale_events": True,
    "_prune_old_call_buyers": True,
}


def _patch_phases(called):
    patches = []
    for name, value in _PHASES.items():
        async def fake(*a, _name=name, _value=value, **k):
            called.append(_name)
            return _value

        patches.append(patch.object(main, name, new=fake))
    return patches


async def test_poll_skips_every_phase_and_alerts_ops_when_the_heartbeat_write_is_rejected():
    called, alerts = [], []

    async def rejected(client, slug, alert_type, value):
        return False

    async def fake_ops_alert(client, where):
        alerts.append(where)

    settings.nft_cron_secret = "poll-secret"
    patches = _patch_phases(called) + [
        patch.object(main, "_nft_alert_state_set", new=rejected),
        patch.object(main, "_alert_ops_db_not_writable", new=fake_ops_alert),
    ]
    for p in patches:
        p.start()
    try:
        result = await main.nft_poll(FakeRequest("Bearer poll-secret"))
    finally:
        for p in patches:
            p.stop()
        settings.nft_cron_secret = ""

    assert result["skipped"] == "database_not_writable"
    assert "database_not_writable" in result["errors"][0]
    assert called == []  # nothing that could post (or prune) ran
    assert alerts == ["nft-poll"]


async def test_poll_runs_every_phase_normally_when_the_heartbeat_write_is_accepted():
    called = []

    async def accepted(client, slug, alert_type, value):
        return True

    async def must_not_alert(client, where):
        raise AssertionError("ops must not be alerted while writes are working")

    original_scope = settings.nft_scope_enabled
    settings.nft_cron_secret = "poll-secret"
    settings.nft_scope_enabled = True
    patches = _patch_phases(called) + [
        patch.object(main, "_nft_alert_state_set", new=accepted),
        patch.object(main, "_alert_ops_db_not_writable", new=must_not_alert),
    ]
    for p in patches:
        p.start()
    try:
        result = await main.nft_poll(FakeRequest("Bearer poll-secret"))
    finally:
        for p in patches:
            p.stop()
        settings.nft_cron_secret = ""
        settings.nft_scope_enabled = original_scope

    assert "skipped" not in result
    assert set(called) == set(_PHASES)


async def test_poll_still_runs_when_the_heartbeat_fails_for_a_network_reason():
    # The original guarantee that must survive: a network blip on the
    # heartbeat write never blocks detection.
    called = []

    async def unreachable(client, slug, alert_type, value):
        raise main.httpx.ConnectError("no route")

    original_scope = settings.nft_scope_enabled
    settings.nft_cron_secret = "poll-secret"
    settings.nft_scope_enabled = True
    patches = _patch_phases(called) + [patch.object(main, "_nft_alert_state_set", new=unreachable)]
    for p in patches:
        p.start()
    try:
        result = await main.nft_poll(FakeRequest("Bearer poll-secret"))
    finally:
        for p in patches:
            p.stop()
        settings.nft_cron_secret = ""
        settings.nft_scope_enabled = original_scope

    assert "skipped" not in result
    assert "_nft_poll_watchlist_alerts" in called


# ── Alert Tracker ────────────────────────────────────────────────────────

_TRACKER_HITS = [{"address": "0xa", "tag": "REALCOIN", "rank": None, "pnl": None}]


def _tracker_collection():
    return {"name": "Test Collection", "slug": "test-collection", "floor": 0.05, "symbol": "ETH", "chain": "ethereum",
            "openseaUrl": "https://opensea.io/collection/test-collection", "image": None}


async def _run_tracker(writable):
    posted, alerts, recorded = [], [], []

    async def fake_already(client, slug):
        return False

    async def fake_wash(client, slug):
        return True

    async def fake_live(client, c):
        return False, None

    async def fake_writable(client):
        return writable

    async def fake_ops_alert(client, where):
        alerts.append(where)

    async def fake_post(client, channel_id, embed, content=None, components=None):
        posted.append(embed["title"])
        return True

    async def record(*a, **k):
        recorded.append(1)

    patches = [
        patch.object(main, "_alert_tracker_already_posted", new=fake_already),
        patch.object(main, "_nft_scope_clears_wash_check", new=fake_wash),
        patch.object(main, "_mint_has_ended", new=fake_live),
        patch.object(main, "_db_writable", new=fake_writable),
        patch.object(main, "_alert_ops_db_not_writable", new=fake_ops_alert),
        patch.object(main, "_post_channel_message", new=fake_post),
        patch.object(main, "_nft_scope_mark_posted", new=record),
        patch.object(main, "_nft_scope_record_call_buyers", new=record),
        patch.object(main, "_alert_tracker_record_call", new=record),
    ]
    for p in patches:
        p.start()
    try:
        result = await main._nft_scope_maybe_post_tracked_convergence(
            main.httpx.AsyncClient(), "slug", _tracker_collection(), _TRACKER_HITS,
            {"tier": "red", "blocked": False, "has_real_activity": True, "has_timeliness_signal": True},
        )
    finally:
        for p in patches:
            p.stop()
    return result, posted, alerts, recorded


async def test_alert_tracker_refuses_to_post_when_the_database_rejects_writes():
    result, posted, alerts, recorded = await _run_tracker(writable=False)
    assert result is False
    assert posted == []  # would have re-posted on every further event otherwise
    assert recorded == []
    assert alerts == ["Alert Tracker"]


async def test_alert_tracker_posts_normally_when_the_database_accepts_writes():
    result, posted, alerts, recorded = await _run_tracker(writable=True)
    assert result is True
    assert len(posted) == 1
    assert alerts == []
    assert recorded
