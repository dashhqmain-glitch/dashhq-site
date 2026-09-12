"""Tests for the nft-poll dead-man's-switch: /cron/check-poll-heartbeat.

Real gap found in an infrastructure audit: nft-poll.yml is a GH Actions
job that loops on its own wall clock and is only restarted every 2 hours
by a coarse schedule - if that loop ever dies between restarts, nothing
would have noticed on its own, which is exactly the class of silent
failure this whole session was spent chasing down for other reasons.
"""
from datetime import datetime, timedelta, timezone

import main
from config import settings


class FakeRes:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.httpx.HTTPStatusError("boom", request=None, response=self)


class FakeRequest:
    def __init__(self, auth=None):
        self.headers = {"authorization": auth} if auth is not None else {}


def _iso(dt):
    return dt.isoformat()


async def test_check_poll_heartbeat_requires_cron_secret():
    settings.cron_secret = ""
    try:
        try:
            await main.check_poll_heartbeat(FakeRequest("Bearer anything"))
            assert False, "should have raised"
        except main.HTTPException as e:
            assert e.status_code == 401
    finally:
        settings.cron_secret = "test-cron-secret"


async def test_check_poll_heartbeat_rejects_wrong_token():
    settings.cron_secret = "real-secret"
    try:
        try:
            await main.check_poll_heartbeat(FakeRequest("Bearer wrong"))
            assert False, "should have raised"
        except main.HTTPException as e:
            assert e.status_code == 401
    finally:
        settings.cron_secret = "test-cron-secret"


async def test_healthy_heartbeat_reports_not_stale_and_posts_nothing():
    settings.cron_secret = "s"
    posted = []
    recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=2))

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if params.get("alert_type") == "eq.started":
                return FakeRes(200, [{"last_alerted_at": recent}])
            return FakeRes(200, [])

        async def post(self, url, headers=None, json=None):
            posted.append(json)
            return FakeRes(200, {})

    try:
        import unittest.mock as mock
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.check_poll_heartbeat(FakeRequest("Bearer s"))
    finally:
        settings.cron_secret = "test-cron-secret"

    assert result["stale"] is False
    assert posted == []


async def test_stale_heartbeat_alerts_when_ops_channel_configured():
    settings.cron_secret = "s"
    settings.discord_ops_alert_channel_id = "12345"
    stale_time = _iso(datetime.now(timezone.utc) - timedelta(minutes=40))
    posted_embeds = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if params.get("alert_type") == "eq.started":
                return FakeRes(200, [{"last_alerted_at": stale_time}])
            if params.get("alert_type") == "eq.alerted":
                return FakeRes(200, [])  # never alerted before - not in cooldown
            return FakeRes(200, [])

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

    async def fake_post_channel_message(client, channel_id, embed):
        posted_embeds.append((channel_id, embed))
        return True

    try:
        import unittest.mock as mock
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient, \
             mock.patch.object(main, "_post_channel_message", new=fake_post_channel_message):
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.check_poll_heartbeat(FakeRequest("Bearer s"))
    finally:
        settings.cron_secret = "test-cron-secret"
        settings.discord_ops_alert_channel_id = ""

    assert result["stale"] is True
    assert result["alerted"] is True
    assert len(posted_embeds) == 1
    assert posted_embeds[0][0] == "12345"
    assert "nft-poll" in posted_embeds[0][1]["description"]


async def test_stale_heartbeat_does_not_spam_within_alert_cooldown():
    settings.cron_secret = "s"
    settings.discord_ops_alert_channel_id = "12345"
    stale_time = _iso(datetime.now(timezone.utc) - timedelta(minutes=40))
    recently_alerted = _iso(datetime.now(timezone.utc) - timedelta(minutes=10))
    posted_embeds = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if params.get("alert_type") == "eq.started":
                return FakeRes(200, [{"last_alerted_at": stale_time}])
            if params.get("alert_type") == "eq.alerted":
                return FakeRes(200, [{"last_alerted_at": recently_alerted}])
            return FakeRes(200, [])

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

    async def fake_post_channel_message(client, channel_id, embed):
        posted_embeds.append(embed)
        return True

    try:
        import unittest.mock as mock
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient, \
             mock.patch.object(main, "_post_channel_message", new=fake_post_channel_message):
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.check_poll_heartbeat(FakeRequest("Bearer s"))
    finally:
        settings.cron_secret = "test-cron-secret"
        settings.discord_ops_alert_channel_id = ""

    assert result["stale"] is True
    assert result["alerted"] is False
    assert posted_embeds == []


async def test_never_recorded_a_heartbeat_counts_as_stale():
    settings.cron_secret = "s"
    settings.discord_ops_alert_channel_id = ""  # not configured - must not crash

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])  # no row at all, either query

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

    try:
        import unittest.mock as mock
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.check_poll_heartbeat(FakeRequest("Bearer s"))
    finally:
        settings.cron_secret = "test-cron-secret"

    assert result["stale"] is True
    assert result["alerted"] is False  # no channel configured, so no post - but doesn't crash
