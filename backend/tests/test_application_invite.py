"""Tests for the accepted-applicant Discord invite flow - specifically
that accepting the same application twice (a double-click on the Accept
button, or Discord retrying a slow interaction) reuses the invite already
generated instead of minting a second one and silently orphaning
whichever link the applicant already opened."""
from unittest.mock import patch

import main
from config import settings


class FakeRes:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


def _application_row(**overrides):
    row = {
        "id": "app1", "name": "Test Applicant", "x_username": "testuser",
        "status": "pending", "submitted_at": "2026-01-01T00:00:00Z",
        "invite_url": None, "intro": "Hello", "communities": "None", "value": "Vibes",
    }
    row.update(overrides)
    return row


async def test_first_accept_generates_a_new_invite():
    settings.discord_bot_token = "x"
    settings.discord_invite_channel_id = "999"
    row = _application_row()
    patched = {}

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [row])

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {"code": "brandnew123"})

        async def patch(self, url, headers=None, params=None, json=None):
            patched.update(json)
            return FakeRes(200)

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main._finalize_review("app1", "accepted", "reviewer1")

    assert patched["invite_url"] == "https://discord.gg/brandnew123"


async def test_reaccepting_reuses_the_existing_invite_instead_of_minting_a_new_one():
    settings.discord_bot_token = "x"
    settings.discord_invite_channel_id = "999"
    row = _application_row(status="accepted", invite_url="https://discord.gg/originallink")
    post_calls = []
    patched = {}

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [row])

        async def post(self, url, headers=None, json=None):
            post_calls.append(url)
            return FakeRes(200, {"code": "shouldnotbeused"})

        async def patch(self, url, headers=None, params=None, json=None):
            patched.update(json)
            return FakeRes(200)

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main._finalize_review("app1", "accepted", "reviewer1")

    assert post_calls == []  # never called Discord's invite-creation endpoint again
    assert patched["invite_url"] == "https://discord.gg/originallink"


async def test_declined_then_accepted_still_generates_a_fresh_invite():
    # A previously-declined application has no invite_url yet - flipping
    # it to accepted must still generate a real one, not silently skip.
    settings.discord_bot_token = "x"
    settings.discord_invite_channel_id = "999"
    row = _application_row(status="declined", invite_url=None)
    post_calls = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [row])

        async def post(self, url, headers=None, json=None):
            post_calls.append(url)
            return FakeRes(200, {"code": "afterdecline"})

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200)

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main._finalize_review("app1", "accepted", "reviewer1")

    assert len(post_calls) == 1


# ── _application_embed's "Required Follows" field - there's no real API
# follow-check (X dropped its free tier), so this is a self-reported claim
# surfaced with direct links so a reviewer can spot-check it manually. ──

def _follow_field(embed):
    return next(f for f in embed["fields"] if f["name"] == "Required Follows (self-reported)")


def test_embed_flags_a_true_claim_and_links_every_required_account():
    row = _application_row(followed_team=True)
    field = _follow_field(main._application_embed(row))
    assert "Claims all" in field["value"] and "NOT all" not in field["value"]
    for _, handle in main._REQUIRED_FOLLOW_ACCOUNTS:
        assert f"https://x.com/{handle}" in field["value"]


def test_embed_flags_a_false_claim():
    row = _application_row(followed_team=False)
    field = _follow_field(main._application_embed(row))
    assert "Claims NOT all" in field["value"]


def test_embed_treats_a_missing_followed_team_key_as_unclaimed_not_a_crash():
    # Older rows (or a test fixture that predates this column) shouldn't
    # blow up the embed - missing evidence must read the same as "false",
    # never silently as "true".
    row = _application_row()
    assert "followed_team" not in row
    field = _follow_field(main._application_embed(row))
    assert "Claims NOT all" in field["value"]
