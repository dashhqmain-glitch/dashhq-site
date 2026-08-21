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
    assert any("5" in f["value"] for f in embed["fields"] if f["name"] == "Wallets Submitted")


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


async def test_aco_setup_support_rejects_non_staff():
    settings.discord_aco_staff_role_id = "role123"
    result = await main._handle_aco_setup_support_command(_payload(permissions="0", roles=[]))
    assert "staff only" in result["data"]["content"].lower()


async def test_aco_drop_command_allows_staff_role_holder():
    settings.discord_aco_staff_role_id = "role123"
    settings.discord_aco_channel_id = "chan1"
    result = await main._handle_aco_drop_command(_payload(permissions="0", roles=["role123"]))
    assert result["type"] == 9  # opens the create-drop modal
    assert result["data"]["custom_id"] == "acocreate"


# ── Wallet submission flow ────────────────────────────────────────────────

async def test_wallet_submit_rejects_when_drop_not_found():
    async def fake_get(url, headers=None, params=None):
        return FakeRes(200, [])

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return await fake_get(url, headers, params)

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        components = [{"components": [{"custom_id": "wallets", "value": "0x1234567890123456789012345678901234567890"}]}]
        result = await main._handle_aco_wallet_submit(_payload(components=components), "missingdrop")

    assert "no longer exists" in result["data"]["content"]


async def test_wallet_submit_rejects_past_deadline():
    from datetime import datetime, timedelta, timezone
    past_drop = {
        "id": "drop1", "status": "open", "title": "Test",
        "deadline": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "discord_channel_id": "chan1", "discord_message_id": "msg1",
    }

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [past_drop])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        components = [{"components": [{"custom_id": "wallets", "value": "0x1234567890123456789012345678901234567890"}]}]
        result = await main._handle_aco_wallet_submit(_payload(components=components), "drop1")

    assert "deadline" in result["data"]["content"].lower()


async def test_wallet_submit_inserts_tickets_and_confirms():
    from datetime import datetime, timedelta, timezone
    open_drop = {
        "id": "drop1", "status": "open", "title": "Test",
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "discord_channel_id": "chan1", "discord_message_id": "msg1",
    }
    posted = {}

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "aco_drops" in url:
                return FakeRes(200, [open_drop])
            return FakeRes(200, [
                {"discord_user_id": "user1", "wallet_address": "0x1234567890123456789012345678901234567890", "submitted_at": "x"},
            ])

        async def post(self, url, headers=None, json=None):
            posted["tickets"] = json
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        components = [{"components": [{"custom_id": "wallets", "value": "0x1234567890123456789012345678901234567890"}]}]
        result = await main._handle_aco_wallet_submit(_payload(components=components), "drop1")

    assert "1 wallet(s) submitted" in result["data"]["content"]
    assert posted["tickets"][0]["wallet_address"] == "0x1234567890123456789012345678901234567890"


async def test_wallet_submit_reports_invalid_lines():
    from datetime import datetime, timedelta, timezone
    open_drop = {
        "id": "drop1", "status": "open", "title": "Test",
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "discord_channel_id": "chan1", "discord_message_id": "msg1",
    }

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "aco_drops" in url:
                return FakeRes(200, [open_drop])
            return FakeRes(200, [{"discord_user_id": "user1", "wallet_address": "0xaaa", "submitted_at": "x"}])

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        components = [{"components": [{"custom_id": "wallets", "value": "0x1234567890123456789012345678901234567890\nnotawallet"}]}]
        result = await main._handle_aco_wallet_submit(_payload(components=components), "drop1")

    assert "1 line(s)" in result["data"]["content"]


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
