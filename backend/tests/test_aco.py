"""Tests for the ACO ticketing system - wallet parsing, deadline parsing,
embed rendering, and (most importantly) that every admin-only action
actually rejects a non-staff member instead of silently allowing it."""
from unittest.mock import patch

import main
from config import settings


class FakeRes:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


def _payload(custom_id=None, components=None, roles=None, permissions="0", channel_id="chan1", user_id="user1", cmd_name=None):
    p = {
        "id": "int1", "token": "tok1", "channel_id": channel_id,
        "member": {
            "user": {"id": user_id, "username": "someone", "global_name": "Someone"},
            "roles": roles or [], "permissions": permissions,
        },
    }
    if cmd_name:
        p["data"] = {"name": cmd_name, "options": []}
    elif components is not None:
        p["data"] = {"custom_id": custom_id, "components": components}
    else:
        p["data"] = {"custom_id": custom_id}
    return p


def _staff_payload(**kwargs):
    return _payload(permissions="32", **kwargs)


# ── _parse_aco_wallets ───────────────────────────────────────────────────

def test_parses_newline_and_comma_separated_wallets():
    raw = "0x1234567890123456789012345678901234567890\n0xabcdefABCDEF12345678901234567890abcdef12,0x1111111111111111111111111111111111111111"
    valid, invalid = main._parse_aco_wallets(raw)
    assert len(valid) == 3
    assert invalid == []


def test_flags_invalid_looking_addresses():
    valid, invalid = main._parse_aco_wallets("not-a-wallet\n0x123\n0x1234567890123456789012345678901234567890")
    assert len(valid) == 1
    assert len(invalid) == 2


def test_dedupes_case_insensitively_keeping_first_casing():
    addr = "0xAbCdEf1234567890123456789012345678901234"
    valid, invalid = main._parse_aco_wallets(f"{addr}\n{addr.lower()}")
    assert valid == [addr]
    assert invalid == []


def test_handles_empty_and_whitespace_only_input():
    valid, invalid = main._parse_aco_wallets("   \n\n  ")
    assert valid == [] and invalid == []


# ── _parse_aco_deadline ──────────────────────────────────────────────────

def test_parses_relative_duration_shorthand():
    from datetime import datetime, timezone
    before = datetime.now(timezone.utc)
    result = main._parse_aco_deadline("6h")
    assert result is not None
    delta_hours = (result - before).total_seconds() / 3600
    assert 5.9 <= delta_hours <= 6.1


def test_parses_minutes_and_days():
    assert main._parse_aco_deadline("30m") is not None
    assert main._parse_aco_deadline("2d") is not None


def test_parses_iso_datetime():
    result = main._parse_aco_deadline("2026-12-25T18:00:00")
    assert result is not None
    assert result.year == 2026 and result.month == 12


def test_rejects_zero_or_negative_duration():
    assert main._parse_aco_deadline("0h") is None
    assert main._parse_aco_deadline("-5h") is None


def test_rejects_garbage_input():
    assert main._parse_aco_deadline("whenever") is None
    assert main._parse_aco_deadline("") is None


# ── _aco_deadline_text / _aco_deadline_passed ────────────────────────────

def test_deadline_text_shows_passed_for_past_dates():
    from datetime import datetime, timedelta, timezone
    past = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    assert "Passed" in main._aco_deadline_text(past)


def test_deadline_text_shows_upcoming_for_future_dates():
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    assert "in" in main._aco_deadline_text(future)


def test_deadline_passed_helper():
    from datetime import datetime, timedelta, timezone
    past_drop = {"deadline": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}
    future_drop = {"deadline": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}
    assert main._aco_deadline_passed(past_drop) is True
    assert main._aco_deadline_passed(future_drop) is False


# ── _is_aco_staff ─────────────────────────────────────────────────────────

def test_manage_guild_permission_counts_as_staff_even_without_the_role():
    settings.discord_aco_staff_role_id = "role123"
    payload = _payload(permissions="32", roles=[])
    assert main._is_aco_staff(payload) is True


def test_aco_staff_role_counts_without_manage_guild():
    settings.discord_aco_staff_role_id = "role123"
    payload = _payload(permissions="0", roles=["role123"])
    assert main._is_aco_staff(payload) is True


def test_neither_role_nor_permission_is_not_staff():
    settings.discord_aco_staff_role_id = "role123"
    payload = _payload(permissions="0", roles=["some_other_role"])
    assert main._is_aco_staff(payload) is False


def test_unset_staff_role_falls_back_to_manage_guild_only():
    settings.discord_aco_staff_role_id = ""
    assert main._is_aco_staff(_payload(permissions="32")) is True
    assert main._is_aco_staff(_payload(permissions="0")) is False


# ── _aco_drop_embed / _aco_drop_components ───────────────────────────────

def _drop(**overrides):
    d = {
        "id": "drop1", "title": "NTRPY FCFS", "chain": "Robinhood", "status": "open",
        "deadline": "2026-12-25T18:00:00+00:00", "profit_note": "30% Profit",
        "contract_address": "0xabc", "checker_url": "https://opensea.io/x",
    }
    d.update(overrides)
    return d


def test_embed_includes_core_fields():
    embed = main._aco_drop_embed(_drop(), ticket_count=5, member_count=3)
    names = [f["name"] for f in embed["fields"]]
    assert "Chain" in names and "Deadline" in names and "Status" in names
    wallets_field = next(f for f in embed["fields"] if "Wallets Submitted" in f["name"])
    assert "5" in wallets_field["value"]


def test_embed_carries_dash_aco_branding():
    embed = main._aco_drop_embed(_drop(), 0, 0)
    assert embed["author"]["name"] == "DASH ACO"
    assert embed["color"] == main._ACO_BLUE
    assert "DASH ACO" in embed["footer"]["text"]


def test_embed_omits_optional_fields_when_absent():
    d = _drop(profit_note=None, contract_address=None, checker_url=None)
    embed = main._aco_drop_embed(d, 0, 0)
    names = [f["name"] for f in embed["fields"]]
    assert "Profit / Notes" not in names and "Contract" not in names and "Checker" not in names


def test_open_drop_has_all_four_buttons():
    components = main._aco_drop_components("drop1", "open")
    labels = [c["label"] for c in components[0]["components"]]
    assert labels == ["Submit Wallet(s)", "See Wallets", "Mark Resolved", "Cancel Drop"]


def test_resolved_drop_only_has_see_wallets_button():
    components = main._aco_drop_components("drop1", "resolved")
    labels = [c["label"] for c in components[0]["components"]]
    assert labels == ["See Wallets"]


# ── Admin-gate enforcement - the security-critical part ──────────────────

async def test_aco_drop_command_rejects_non_staff():
    settings.discord_aco_staff_role_id = "role123"
    result = await main._handle_aco_drop_command(_payload(permissions="0", roles=[]))
    assert "staff only" in result["data"]["content"].lower()
    assert result["type"] == 4


async def test_aco_create_submit_rejects_non_staff():
    settings.discord_aco_staff_role_id = "role123"
    payload = _payload(permissions="0", roles=[], components=[])
    result = await main._handle_aco_create_submit(payload)
    assert "staff only" in result["data"]["content"].lower()


async def test_aco_resolve_button_rejects_non_staff():
    settings.discord_aco_staff_role_id = "role123"
    result = await main._handle_aco_resolve_button(_payload(permissions="0", roles=[]), "drop1")
    assert "staff only" in result["data"]["content"].lower()


async def test_aco_cancel_button_rejects_non_staff():
    settings.discord_aco_staff_role_id = "role123"
    result = await main._handle_aco_cancel_button(_payload(permissions="0", roles=[]), "drop1")
    assert "staff only" in result["data"]["content"].lower()


async def test_aco_wallets_export_rejects_non_staff():
    settings.discord_aco_staff_role_id = "role123"
    result = await main._handle_aco_wallets_button(_payload(permissions="0", roles=[]), "drop1")
    assert "staff only" in result["data"]["content"].lower()


async def test_aco_drop_command_allows_staff_role_holder():
    settings.discord_aco_staff_role_id = "role123"
    settings.discord_aco_channel_id = "chan1"
    result = await main._handle_aco_drop_command(_payload(permissions="0", roles=["role123"]))
    assert result["type"] == 9  # opens the create-drop modal
    assert result["data"]["custom_id"] == "acocreate"


def _create_submit_components():
    return [
        {"components": [{"custom_id": "title", "value": "Test Drop"}]},
        {"components": [{"custom_id": "chain", "value": "Ethereum"}]},
        {"components": [{"custom_id": "deadline", "value": "6h"}]},
        {"components": [{"custom_id": "profit", "value": "30% profit"}]},
        {"components": [{"custom_id": "contract_and_checker", "value": ""}]},
    ]


async def test_aco_create_submit_reports_real_success_when_channel_post_works():
    # Regression test for a real gap: this used to say "posted" even when
    # the Discord channel post silently failed.
    settings.discord_aco_staff_role_id = "role123"
    settings.discord_aco_channel_id = "chan1"
    settings.discord_bot_token = "tok"
    patches = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            if "aco_drops" in url:
                return FakeRes(200, [{"id": "drop1", "title": "Test Drop", "chain": "Ethereum", "status": "open",
                                       "deadline": "2026-12-25T18:00:00+00:00"}])
            if "/messages" in url:
                return FakeRes(200, {"id": "msg1"})  # the channel post itself succeeded
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        payload = _payload(permissions="32", roles=[], components=_create_submit_components())
        result = await main._handle_aco_create_submit(payload)

    assert result["type"] == 5
    body = _webhook_patch_body(patches)
    assert "posted" in body["embeds"][0]["title"].lower()
    assert "⚠️" not in body["embeds"][0]["title"]


async def test_aco_create_submit_reports_real_failure_when_channel_post_fails():
    settings.discord_aco_staff_role_id = "role123"
    settings.discord_aco_channel_id = "chan1"
    settings.discord_bot_token = "tok"
    patches = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            if "aco_drops" in url:
                return FakeRes(200, [{"id": "drop1", "title": "Test Drop", "chain": "Ethereum", "status": "open",
                                       "deadline": "2026-12-25T18:00:00+00:00"}])
            if "/messages" in url:
                return FakeRes(403, {}, text="Missing Access")  # bot lacks permission in the channel
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        payload = _payload(permissions="32", roles=[], components=_create_submit_components())
        result = await main._handle_aco_create_submit(payload)

    assert result["type"] == 5
    body = _webhook_patch_body(patches)
    assert "failed" in body["embeds"][0]["title"].lower()
    # Must NOT have PATCHed discord_message_id onto the drop row when
    # nothing was actually posted - only the interaction-reply PATCH
    # (to the webhooks endpoint) should have happened.
    assert not any("aco_drops" in u for u, _ in patches)


# ── Drop resolve/cancel can't re-finalize an already-closed drop ─────────

async def test_finalize_drop_rejects_already_resolved():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"id": "drop1", "title": "Test", "status": "resolved"}])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._aco_finalize_drop("drop1", "cancelled", "actor1")

    assert result["type"] == 4
    assert "already" in result["data"]["content"].lower()
    assert "resolved" in result["data"]["content"].lower()


# ── Wallet submission flow ────────────────────────────────────────────────

def _webhook_patch_body(patches):
    # _handle_aco_wallet_submit (and the other deferred ACO handlers) now
    # answer via a PATCH to the interaction webhook's @original message,
    # not the function's direct return value - this pulls out that final
    # payload regardless of how many other PATCH calls happened alongside it.
    return next(j for u, j in patches if "webhooks" in u)


async def test_wallet_submit_rejects_when_drop_not_found():
    patches = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        components = [{"components": [{"custom_id": "wallets", "value": "0x1234567890123456789012345678901234567890"}]}]
        result = await main._handle_aco_wallet_submit(_payload(components=components), "missingdrop")

    assert result["type"] == 5
    assert "no longer exists" in _webhook_patch_body(patches)["content"]


async def test_wallet_submit_rejects_past_deadline():
    from datetime import datetime, timedelta, timezone
    past_drop = {
        "id": "drop1", "status": "open", "title": "Test",
        "deadline": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "discord_channel_id": "chan1", "discord_message_id": "msg1",
    }
    patches = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [past_drop])

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        components = [{"components": [{"custom_id": "wallets", "value": "0x1234567890123456789012345678901234567890"}]}]
        result = await main._handle_aco_wallet_submit(_payload(components=components), "drop1")

    assert result["type"] == 5
    assert "deadline" in _webhook_patch_body(patches)["content"].lower()


async def test_wallet_submit_inserts_tickets_and_confirms():
    from datetime import datetime, timedelta, timezone
    open_drop = {
        "id": "drop1", "status": "open", "title": "Test",
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "discord_channel_id": "chan1", "discord_message_id": "msg1",
    }
    posted = {}
    patches = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "aco_drops" in url:
                return FakeRes(200, [open_drop])
            return FakeRes(200, [
                {"discord_user_id": "user1", "wallet_address": "0x1234567890123456789012345678901234567890", "submitted_at": "x"},
            ])

        async def post(self, url, headers=None, json=None):
            if "aco_tickets" in url:
                posted["tickets"] = json
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        components = [{"components": [{"custom_id": "wallets", "value": "0x1234567890123456789012345678901234567890"}]}]
        result = await main._handle_aco_wallet_submit(_payload(components=components), "drop1")

    assert result["type"] == 5
    body = _webhook_patch_body(patches)
    assert "1 wallet(s) submitted" in body["content"]
    assert posted["tickets"][0]["wallet_address"] == "0x1234567890123456789012345678901234567890"
    # The only path to support is this contextual button - confirms it's
    # actually attached, not just a public standing panel that no longer
    # exists.
    button = body["components"][0]["components"][0]
    assert button["custom_id"] == "acosupport_open"
    assert button["label"] == "Need Help?"
    # The channel-message edit for the drop's own ticket count is a
    # SEPARATE PATCH from the interaction reply above - confirms both
    # actually happened, not just whichever one the return value used to
    # carry.
    channel_patch = next(j for u, j in patches if "/channels/" in u)
    assert channel_patch["embeds"][0]["fields"][-1]["value"].startswith("**1**")


async def test_wallet_submit_reports_invalid_lines():
    from datetime import datetime, timedelta, timezone
    open_drop = {
        "id": "drop1", "status": "open", "title": "Test",
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "discord_channel_id": "chan1", "discord_message_id": "msg1",
    }
    patches = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "aco_drops" in url:
                return FakeRes(200, [open_drop])
            return FakeRes(200, [{"discord_user_id": "user1", "wallet_address": "0xaaa", "submitted_at": "x"}])

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        components = [{"components": [{"custom_id": "wallets", "value": "0x1234567890123456789012345678901234567890\nnotawallet"}]}]
        result = await main._handle_aco_wallet_submit(_payload(components=components), "drop1")

    assert result["type"] == 5
    assert "1 line(s)" in _webhook_patch_body(patches)["content"]


# ── Support ticket open - dedup against an existing open ticket ──────────

async def test_support_open_reuses_existing_open_ticket_instead_of_duplicating():
    settings.discord_bot_token = "tok"
    patches = []
    thread_created = {"called": False}

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"thread_id": "existingthread"}])

        async def post(self, url, headers=None, json=None):
            if "/threads" in url:
                thread_created["called"] = True
            return FakeRes(200, {"id": "shouldnotbeused"})

        async def put(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_aco_support_open(_payload(channel_id="chan1"))

    assert result["type"] == 5
    assert thread_created["called"] is False
    body = _webhook_patch_body(patches)
    assert "already have an open ticket" in body["content"].lower()
    assert "existingthread" in body["content"]


async def test_support_open_creates_a_new_thread_when_none_open():
    settings.discord_bot_token = "tok"
    patches = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])  # no existing open ticket

        async def post(self, url, headers=None, json=None):
            if "/threads" in url:
                return FakeRes(200, {"id": "newthread"})
            return FakeRes(200, {})

        async def put(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_aco_support_open(_payload(channel_id="chan1"))

    assert result["type"] == 5
    body = _webhook_patch_body(patches)
    assert "newthread" in body["content"]


async def test_support_open_recovers_from_a_stale_thread_reference():
    # Regression test for a real reported bug: if a thread ever gets
    # deleted some way OTHER than the bot's own Close button (manual
    # deletion, a permission change, anything), the tracking row never
    # learns it's gone and stays "open" forever - permanently blocking
    # the member from ever opening a new ticket, pointed at a dead
    # thread. The fix checks the thread actually still exists before
    # trusting the row.
    settings.discord_bot_token = "tok"
    patches = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "aco_support_tickets" in url:
                return FakeRes(200, [{"thread_id": "deletedthread"}])
            # The Discord channel-existence check - the thread is gone.
            return FakeRes(404, {"message": "Unknown Channel"})

        async def post(self, url, headers=None, json=None):
            if "/threads" in url:
                return FakeRes(200, {"id": "freshthread"})
            return FakeRes(200, {})

        async def put(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_aco_support_open(_payload(channel_id="chan1"))

    assert result["type"] == 5
    body = _webhook_patch_body(patches)
    # A real new thread got created, not blocked on the dead reference.
    assert "freshthread" in body["content"]
    # The stale row got cleaned up, not left dangling for next time.
    stale_cleanup = next(j for u, j in patches if "aco_support_tickets" in u)
    assert stale_cleanup["status"] == "closed"


async def test_support_open_always_targets_the_dedicated_support_channel():
    # Regression test for a real requirement: a ticket opened from a drop
    # in the public announcement channel must land in the (staff-role-
    # restricted) support channel, never wherever the button was clicked
    # from.
    settings.discord_bot_token = "tok"
    settings.discord_aco_channel_id = "announcement-chan"
    settings.discord_aco_support_channel_id = "support-chan"
    thread_post_urls = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])

        async def post(self, url, headers=None, json=None):
            if "/threads" in url:
                thread_post_urls.append(url)
                return FakeRes(200, {"id": "newthread"})
            return FakeRes(200, {})

        async def put(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        # The click came from the announcement channel...
        await main._handle_aco_support_open(_payload(channel_id="announcement-chan"))

    # ...but the thread must be created under the support channel instead.
    assert len(thread_post_urls) == 1
    assert "support-chan" in thread_post_urls[0]
    assert "announcement-chan" not in thread_post_urls[0]


async def test_support_open_falls_back_to_announcement_channel_when_support_channel_unset():
    settings.discord_bot_token = "tok"
    settings.discord_aco_channel_id = "announcement-chan"
    settings.discord_aco_support_channel_id = ""
    thread_post_urls = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])

        async def post(self, url, headers=None, json=None):
            if "/threads" in url:
                thread_post_urls.append(url)
                return FakeRes(200, {"id": "newthread"})
            return FakeRes(200, {})

        async def put(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main._handle_aco_support_open(_payload(channel_id="announcement-chan"))

    assert len(thread_post_urls) == 1
    assert "announcement-chan" in thread_post_urls[0]


# ── Support ticket close permission ───────────────────────────────────────

async def test_support_close_allows_ticket_opener():
    settings.discord_aco_staff_role_id = "role123"

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"discord_user_id": "opener1", "status": "open"}])

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_aco_support_close(_payload(permissions="0", roles=[], user_id="opener1"))

    assert "closed" in result["data"]["content"].lower()


async def test_support_close_rejects_unrelated_non_staff_member():
    settings.discord_aco_staff_role_id = "role123"

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"discord_user_id": "opener1", "status": "open"}])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_aco_support_close(_payload(permissions="0", roles=[], user_id="rando"))

    assert "only the ticket opener" in result["data"]["content"].lower()


async def test_support_close_allows_staff_even_if_not_opener():
    settings.discord_aco_staff_role_id = "role123"

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"discord_user_id": "opener1", "status": "open"}])

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_aco_support_close(_payload(permissions="32", roles=[], user_id="staffmember"))

    assert "closed" in result["data"]["content"].lower()


# ── ACO education content - rotating rules/guide posts ───────────────────

def _education_post(**overrides):
    p = {
        "id": 1, "title": "Test Guide", "emoji": "⛽",
        "sections": [
            {"heading": "Section One", "body": "First body."},
            {"heading": "Section Two", "body": "Second body."},
        ],
    }
    p.update(overrides)
    return p


def test_education_embeds_has_branded_cover_plus_one_per_section():
    embeds = main._aco_education_embeds(_education_post())
    assert len(embeds) == 3  # cover + 2 sections
    assert embeds[0]["author"]["name"] == "DASH ACO"
    assert "Test Guide" in embeds[0]["title"]
    assert embeds[1]["title"] == "Section One"
    assert embeds[1]["description"] == "First body."
    assert embeds[2]["title"] == "Section Two"


def test_education_embeds_footer_only_on_last_embed():
    embeds = main._aco_education_embeds(_education_post())
    assert "footer" not in embeds[0]
    assert "footer" not in embeds[1]
    assert embeds[-1]["footer"] == main.ACO_FOOTER


def test_education_embeds_caps_at_discord_limit():
    many_sections = [{"heading": f"S{i}", "body": f"body {i}"} for i in range(15)]
    embeds = main._aco_education_embeds(_education_post(sections=many_sections))
    assert len(embeds) == main._ACO_EDUCATION_MAX_EMBEDS


class FakeRequest:
    def __init__(self, auth_header=None):
        self.headers = {"authorization": auth_header} if auth_header else {}


async def test_cron_aco_education_rejects_missing_secret():
    settings.cron_secret = "realsecret"
    try:
        await main.cron_aco_education(FakeRequest())
        assert False, "should have raised"
    except main.HTTPException as exc:
        assert exc.status_code == 401


async def test_cron_aco_education_rejects_wrong_secret():
    settings.cron_secret = "realsecret"
    try:
        await main.cron_aco_education(FakeRequest("Bearer wrongsecret"))
        assert False, "should have raised"
    except main.HTTPException as exc:
        assert exc.status_code == 401


async def test_cron_aco_education_noop_when_channel_not_configured():
    settings.cron_secret = "realsecret"
    settings.discord_aco_channel_id = ""
    result = await main.cron_aco_education(FakeRequest("Bearer realsecret"))
    assert result["posted"] is False
    settings.discord_aco_channel_id = "chan1"  # restore for later tests


async def test_cron_aco_education_posts_oldest_and_updates_last_posted_at():
    settings.cron_secret = "realsecret"
    settings.discord_aco_channel_id = "chan1"
    settings.discord_bot_token = "tok"
    patched = {}

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [_education_post()])

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {"id": "msg1"})

        async def patch(self, url, headers=None, params=None, json=None):
            patched.update(json)
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main.cron_aco_education(FakeRequest("Bearer realsecret"))

    assert result["posted"] is True
    assert "last_posted_at" in patched


# ── /aco-info - self-service browse, open to everyone ────────────────────

async def test_aco_info_command_lists_active_guides():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"title": "Guide A", "emoji": "⛽"}, {"title": "Guide B", "emoji": "🎟️"}])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_aco_info_command(_payload())

    assert result["type"] == 4
    # Public, not ephemeral - a shared reference tool, not a personal lookup.
    assert "flags" not in result["data"]
    options = result["data"]["components"][0]["components"][0]["options"]
    assert {o["value"] for o in options} == {"Guide A", "Guide B"}


async def test_aco_info_command_handles_no_guides():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_aco_info_command(_payload())

    assert "no aco guides" in result["data"]["content"].lower()


async def test_aco_info_select_shows_the_picked_guide():
    post = _education_post(title="Guide A")

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if params and "title" in params:
                return FakeRes(200, [post])
            return FakeRes(200, [{"title": "Guide A"}, {"title": "Guide B"}])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        payload = _payload(custom_id="acoinfo_select", components=None)
        payload["data"]["values"] = ["Guide A"]
        result = await main._handle_aco_info_select(payload)

    assert result["type"] == 7  # UPDATE_MESSAGE
    assert "Guide A" in result["data"]["embeds"][0]["title"]
    # can still switch to a different guide afterward
    options = result["data"]["components"][0]["components"][0]["options"]
    assert {o["value"] for o in options} == {"Guide A", "Guide B"}


async def test_aco_info_select_handles_missing_guide():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        payload = _payload(custom_id="acoinfo_select", components=None)
        payload["data"]["values"] = ["Nonexistent"]
        result = await main._handle_aco_info_select(payload)

    assert "isn't available" in result["data"]["content"]


# ── Private-key handoff - orchestration only, never storage ──────────────

async def test_key_open_reuses_existing_open_handoff():
    settings.discord_bot_token = "tok"
    settings.discord_aco_support_channel_id = "support-chan"
    patches = []
    thread_created = {"called": False}

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"thread_id": "existingkeythread"}])

        async def post(self, url, headers=None, json=None):
            if "/threads" in url:
                thread_created["called"] = True
            return FakeRes(200, {"id": "shouldnotbeused"})

        async def put(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_aco_key_open(_payload(channel_id="announcement-chan"))

    assert result["type"] == 5
    assert thread_created["called"] is False
    body = _webhook_patch_body(patches)
    assert "existingkeythread" in body["content"]


async def test_key_open_recovers_from_a_stale_thread_reference():
    # Same real reported bug as support tickets: a thread deleted outside
    # the Mark Complete flow leaves the row "open" forever, permanently
    # blocking the member from ever sending a key again. Confirm the
    # existence check unblocks them and cleans up the dead row.
    settings.discord_bot_token = "tok"
    settings.discord_aco_support_channel_id = "support-chan"
    patches = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "aco_key_handoffs" in url:
                return FakeRes(200, [{"thread_id": "deletedkeythread"}])
            return FakeRes(404, {"message": "Unknown Channel"})

        async def post(self, url, headers=None, json=None):
            if "/threads" in url:
                return FakeRes(200, {"id": "freshkeythread"})
            return FakeRes(200, {})

        async def put(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_aco_key_open(_payload(channel_id="announcement-chan"))

    assert result["type"] == 5
    body = _webhook_patch_body(patches)
    assert "freshkeythread" in body["content"]
    stale_cleanup = next(j for u, j in patches if "aco_key_handoffs" in u)
    assert stale_cleanup["status"] == "expired"


async def test_key_open_creates_thread_in_support_channel_never_the_announcement_channel():
    settings.discord_bot_token = "tok"
    settings.discord_aco_channel_id = "announcement-chan"
    settings.discord_aco_support_channel_id = "support-chan"
    thread_post_urls = []
    thread_messages = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])  # no existing open handoff

        async def post(self, url, headers=None, json=None):
            if "/threads" in url:
                thread_post_urls.append(url)
                return FakeRes(200, {"id": "newkeythread"})
            if "/messages" in url:
                thread_messages.append(json)
            return FakeRes(200, {})

        async def put(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        # Clicked from the public announcement channel...
        result = await main._handle_aco_key_open(_payload(channel_id="announcement-chan"))

    assert result["type"] == 5
    # ...but the thread must land in the support channel, never the
    # announcement channel.
    assert len(thread_post_urls) == 1
    assert "support-chan" in thread_post_urls[0]
    assert "announcement-chan" not in thread_post_urls[0]
    # The instructional message must carry the "Mark Complete" button and
    # must not itself ask for or echo back any key value.
    instructions = thread_messages[0]
    assert instructions["components"][0]["components"][0]["custom_id"] == "acokey_complete"


async def test_key_complete_rejects_non_staff():
    settings.discord_aco_staff_role_id = "role123"
    result = await main._handle_aco_key_complete(_payload(permissions="0", roles=[]))
    assert "staff only" in result["data"]["content"].lower()


async def test_key_complete_rejects_already_completed():
    settings.discord_aco_staff_role_id = "role123"

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"discord_user_id": "user1", "status": "completed"}])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_aco_key_complete(_payload(permissions="32", roles=[]))

    assert "already closed" in result["data"]["content"].lower()


async def test_key_complete_deletes_thread_and_marks_completed():
    settings.discord_bot_token = "tok"
    settings.discord_aco_staff_role_id = "role123"
    patches = []
    deletes = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"discord_user_id": "user1", "status": "open"}])

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

        async def delete(self, url, headers=None):
            deletes.append(url)
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_aco_key_complete(_payload(permissions="32", roles=[], channel_id="keythread1"))

    assert result["type"] == 6
    assert any("keythread1" in u for u in deletes)  # the thread itself was deleted, not just archived
    supabase_patch = next(j for u, j in patches if "aco_key_handoffs" in u)
    assert supabase_patch["status"] == "completed"


async def test_cleanup_expired_key_handoffs_deletes_old_threads_and_marks_expired():
    from datetime import datetime, timedelta, timezone
    old_row = {"id": 1, "thread_id": "oldthread", "discord_user_id": "user1"}
    deletes = []
    patches = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [old_row])

        async def delete(self, url, headers=None):
            deletes.append(url)
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    fake_client = FakeClient()
    count = await main._aco_cleanup_expired_key_handoffs(fake_client, max_age_hours=24)

    assert count == 1
    assert any("oldthread" in u for u in deletes)
    assert patches[0][1]["status"] == "expired"


async def test_cron_aco_key_cleanup_requires_valid_secret():
    settings.cron_secret = "realsecret"
    try:
        await main.cron_aco_key_cleanup(FakeRequest())
        assert False, "should have raised"
    except main.HTTPException as exc:
        assert exc.status_code == 401
