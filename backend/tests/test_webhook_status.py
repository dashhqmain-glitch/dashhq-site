"""Tests for the Alchemy webhook health diagnostics:
/cron/check-webhook-status and /cron/check-webhook-address.

Real precedent this session: all 3 Alchemy webhooks were once
auto-paused with TOO_MANY_ERRORS, and it was only caught because a human
happened to check the dashboard directly. These make that self-serve and
proactive instead of relying on someone noticing.
"""
from unittest import mock

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


def _set_webhook_settings():
    settings.alchemy_webhook_auth_token = "token"
    settings.alchemy_webhook_id_ethereum = "wh_eth"
    settings.alchemy_webhook_id_robinhood = "wh_rh"
    settings.alchemy_webhook_id_ink = "wh_ink"


def _clear_webhook_settings():
    settings.alchemy_webhook_auth_token = ""
    settings.alchemy_webhook_id_ethereum = ""
    settings.alchemy_webhook_id_robinhood = ""
    settings.alchemy_webhook_id_ink = ""
    settings.discord_ops_alert_channel_id = ""


# ── /cron/check-webhook-status ────────────────────────────────────────────

async def test_check_webhook_status_requires_cron_secret():
    settings.cron_secret = ""
    try:
        try:
            await main.check_webhook_status(FakeRequest("Bearer anything"))
            assert False, "should have raised"
        except main.HTTPException as e:
            assert e.status_code == 401
    finally:
        settings.cron_secret = "test-cron-secret"


async def test_check_webhook_status_skips_without_auth_token():
    settings.cron_secret = "s"
    settings.alchemy_webhook_auth_token = ""
    try:
        result = await main.check_webhook_status(FakeRequest("Bearer s"))
    finally:
        settings.cron_secret = "test-cron-secret"
    assert result == {"checked": False, "reason": "alchemy_webhook_auth_token not configured"}


async def test_check_webhook_status_reports_active_webhooks_without_alerting():
    settings.cron_secret = "s"
    _set_webhook_settings()
    posted = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "team-webhooks" in url:
                return FakeRes(200, {"data": [
                    {"id": "wh_eth", "is_active": True, "deactivation_reason": "TOO_MANY_ERRORS", "network": "ETH_MAINNET"},
                    {"id": "wh_rh", "is_active": True, "deactivation_reason": None, "network": "ROBINHOOD_MAINNET"},
                    {"id": "wh_ink", "is_active": True, "deactivation_reason": None, "network": "INK_MAINNET"},
                ]})
            return FakeRes(200, [])

    async def fake_post_channel_message(client, channel_id, embed):
        posted.append(embed)
        return True

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient, \
             mock.patch.object(main, "_post_channel_message", new=fake_post_channel_message):
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.check_webhook_status(FakeRequest("Bearer s"))
    finally:
        settings.cron_secret = "test-cron-secret"
        _clear_webhook_settings()

    assert result["inactive"] == []
    assert result["reactivated"] == []
    assert result["alerted"] is False
    assert posted == []
    # A stale deactivation_reason from a past, already-resolved pause must
    # not be read as a current problem - only is_active matters.
    assert result["webhooks"]["ethereum"]["is_active"] is True


async def test_check_webhook_status_reactivates_and_alerts_when_a_webhook_goes_inactive():
    # Real live incident this exact fix was built for: Robinhood's webhook
    # was found inactive mid-session. Safe to auto-reactivate now that the
    # actual crash gap likely causing the underlying errors (see the
    # webhook receiver's broad except Exception fix) is closed.
    settings.cron_secret = "s"
    settings.discord_ops_alert_channel_id = "12345"
    _set_webhook_settings()
    posted = []
    patched = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "team-webhooks" in url:
                return FakeRes(200, {"data": [
                    {"id": "wh_eth", "is_active": True, "network": "ETH_MAINNET"},
                    {"id": "wh_rh", "is_active": False, "deactivation_reason": "TOO_MANY_ERRORS", "network": "ROBINHOOD_MAINNET"},
                    {"id": "wh_ink", "is_active": True, "network": "INK_MAINNET"},
                ]})
            if "nft_alert_state" in url:
                return FakeRes(200, [])  # never alerted before
            return FakeRes(200, [])

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def put(self, url, headers=None, json=None):
            patched.append(json)
            return FakeRes(200, {})

    async def fake_post_channel_message(client, channel_id, embed):
        posted.append((channel_id, embed))
        return True

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient, \
             mock.patch.object(main, "_post_channel_message", new=fake_post_channel_message):
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.check_webhook_status(FakeRequest("Bearer s"))
    finally:
        settings.cron_secret = "test-cron-secret"
        _clear_webhook_settings()

    assert result["inactive"] == ["robinhood"]
    assert result["reactivated"] == ["robinhood"]
    assert result["webhooks"]["robinhood"]["is_active"] is True  # reflects the post-reactivation state
    assert patched == [{"webhook_id": "wh_rh", "is_active": True}]
    assert result["alerted"] is True
    assert len(posted) == 1
    assert posted[0][0] == "12345"
    assert "reactivated" in posted[0][1]["description"].lower()
    assert "robinhood" in posted[0][1]["description"]


async def test_check_webhook_status_alerts_loudly_when_reactivation_fails():
    settings.cron_secret = "s"
    settings.discord_ops_alert_channel_id = "12345"
    _set_webhook_settings()
    posted = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "team-webhooks" in url:
                return FakeRes(200, {"data": [{"id": "wh_rh", "is_active": False, "network": "ROBINHOOD_MAINNET"}]})
            if "nft_alert_state" in url:
                return FakeRes(200, [])
            return FakeRes(200, [])

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def put(self, url, headers=None, json=None):
            return FakeRes(500, {})  # reactivation attempt itself fails

    async def fake_post_channel_message(client, channel_id, embed):
        posted.append(embed)
        return True

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient, \
             mock.patch.object(main, "_post_channel_message", new=fake_post_channel_message):
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.check_webhook_status(FakeRequest("Bearer s"))
    finally:
        settings.cron_secret = "test-cron-secret"
        _clear_webhook_settings()

    assert result["inactive"] == ["robinhood"]
    assert result["reactivated"] == []
    assert result["alerted"] is True
    assert "manual attention" in posted[0]["description"].lower()
    assert posted[0]["title"] == "🚨 CI/Ops Alert"  # still down - the loud variant, not the recovered one


async def test_check_webhook_status_does_not_spam_within_alert_cooldown():
    settings.cron_secret = "s"
    settings.discord_ops_alert_channel_id = "12345"
    _set_webhook_settings()
    posted = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "team-webhooks" in url:
                return FakeRes(200, {"data": [{"id": "wh_rh", "is_active": False, "network": "ROBINHOOD_MAINNET"}]})
            if "nft_alert_state" in url:
                from datetime import datetime, timedelta, timezone
                recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
                return FakeRes(200, [{"last_alerted_at": recent}])
            return FakeRes(200, [])

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def put(self, url, headers=None, json=None):
            return FakeRes(200, {})

    async def fake_post_channel_message(client, channel_id, embed):
        posted.append(embed)
        return True

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient, \
             mock.patch.object(main, "_post_channel_message", new=fake_post_channel_message):
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.check_webhook_status(FakeRequest("Bearer s"))
    finally:
        settings.cron_secret = "test-cron-secret"
        _clear_webhook_settings()

    assert result["inactive"] == ["robinhood"]
    assert result["alerted"] is False
    assert posted == []


async def test_check_webhook_status_handles_no_ops_channel_configured():
    settings.cron_secret = "s"
    settings.discord_ops_alert_channel_id = ""
    _set_webhook_settings()

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "team-webhooks" in url:
                return FakeRes(200, {"data": [{"id": "wh_rh", "is_active": False, "network": "ROBINHOOD_MAINNET"}]})
            return FakeRes(200, [])

        async def put(self, url, headers=None, json=None):
            return FakeRes(200, {})

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.check_webhook_status(FakeRequest("Bearer s"))
    finally:
        settings.cron_secret = "test-cron-secret"
        _clear_webhook_settings()

    assert result["inactive"] == ["robinhood"]
    assert result["reactivated"] == ["robinhood"]  # still attempted even with no channel to alert
    assert result["alerted"] is False  # no channel configured - must not crash


# ── /cron/check-webhook-address ───────────────────────────────────────────

async def test_check_webhook_address_requires_cron_secret():
    settings.cron_secret = ""
    try:
        try:
            await main.check_webhook_address(FakeRequest("Bearer anything"), address="0xabc")
            assert False, "should have raised"
        except main.HTTPException as e:
            assert e.status_code == 401
    finally:
        settings.cron_secret = "test-cron-secret"


async def test_check_webhook_address_skips_without_auth_token():
    settings.cron_secret = "s"
    settings.alchemy_webhook_auth_token = ""
    try:
        result = await main.check_webhook_address(FakeRequest("Bearer s"), address="0xabc")
    finally:
        settings.cron_secret = "test-cron-secret"
    assert result == {"address": "0xabc", "checked": False, "reason": "alchemy_webhook_auth_token not configured"}


async def test_check_webhook_address_reports_watching_and_not_watching_per_chain():
    settings.cron_secret = "s"
    _set_webhook_settings()

    async def fake_current_addresses(client, webhook_id):
        if webhook_id == "wh_eth":
            return {"0xabc"}
        if webhook_id == "wh_rh":
            return {"0xother"}  # not watching 0xabc
        return None  # ink: could not fetch

    try:
        with mock.patch.object(main, "_alchemy_webhook_current_addresses", new=fake_current_addresses):
            result = await main.check_webhook_address(FakeRequest("Bearer s"), address="0xABC")
    finally:
        settings.cron_secret = "test-cron-secret"
        _clear_webhook_settings()

    assert result["address"] == "0xabc"  # lowercased
    assert result["results"]["ethereum"] == "watching"
    assert result["results"]["robinhood"] == "NOT watching"
    assert result["results"]["ink"] == "could not fetch current addresses"
    assert result["results"]["base"] == "no webhook configured for this chain"
