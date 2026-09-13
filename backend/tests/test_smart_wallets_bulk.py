"""Tests for the bulk wallet-review system (/smart-wallets pending -
Approve All / Decline All / Review & Select) and the .xlsx bulk-import
path. Everything here funnels through the exact same insert-into-
smart_wallet_tags + status-update logic the original one-at-a-time
Approve/Reject button already used and already has its own tests -
these cover the NEW bulk surface only.
"""
import io
from unittest.mock import patch

import openpyxl

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


def _payload(permissions="32", channel_id=None, values=None, message=None):
    p = {
        "id": "int1", "token": "tok1", "channel_id": channel_id,
        "member": {"permissions": permissions, "user": {"id": "staff1", "username": "mod"}},
        "data": {},
    }
    if values is not None:
        p["data"]["values"] = values
    if message is not None:
        p["message"] = message
    return p


def _xlsx_bytes(rows, headers=("Address", "Tag", "Category", "Rank", "PNL")):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(headers))
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _submission(id_="s1", address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", tag="Degen", category="Degen", status="pending"):
    return {
        "id": id_, "address": address, "tag": tag, "category": category,
        "submitted_by": "u1", "status": status, "submitted_at": "2026-01-01T00:00:00+00:00",
    }


# ── .xlsx bulk-import parser ──────────────────────────────────────────────

def test_parse_xlsx_reads_address_and_tag_columns():
    data = _xlsx_bytes([["0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "Degen", "Degen", "5", "12.5"]])
    rows, skipped = main._parse_smart_wallet_xlsx(data)
    assert skipped == 0
    assert rows == [{"address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "tag": "Degen", "rank": 5, "pnl": 12.5, "category": "Degen"}]


def test_parse_xlsx_matches_headers_case_insensitively_and_out_of_order():
    data = _xlsx_bytes(
        [["Degen", "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"]],
        headers=("tag", "WALLET ADDRESS"),
    )
    rows, skipped = main._parse_smart_wallet_xlsx(data)
    assert skipped == 0
    assert rows[0]["address"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert rows[0]["tag"] == "Degen"


def test_parse_xlsx_splits_multiple_comma_separated_tags():
    data = _xlsx_bytes([["0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "Degen, Whale"]], headers=("Address", "Tag"))
    rows, skipped = main._parse_smart_wallet_xlsx(data)
    assert {r["tag"] for r in rows} == {"Degen", "Whale"}


def test_parse_xlsx_skips_rows_with_invalid_or_missing_address():
    data = _xlsx_bytes([
        ["not-an-address", "Degen"],
        ["", "Degen"],
        ["0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", ""],
    ], headers=("Address", "Tag"))
    rows, skipped = main._parse_smart_wallet_xlsx(data)
    assert rows == []
    assert skipped == 3


def test_parse_xlsx_returns_empty_with_no_recognizable_header():
    data = _xlsx_bytes([["foo", "bar"]], headers=("Whatever", "Nonsense"))
    rows, skipped = main._parse_smart_wallet_xlsx(data)
    assert rows == []
    assert skipped == 0


def test_parse_xlsx_ignores_trailing_blank_rows():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Address", "Tag"])
    ws.append(["0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "Degen"])
    ws.append([None, None])
    buf = io.BytesIO()
    wb.save(buf)
    rows, skipped = main._parse_smart_wallet_xlsx(buf.getvalue())
    assert len(rows) == 1
    assert skipped == 0


def test_parse_xlsx_handles_a_corrupt_file_without_raising():
    rows, skipped = main._parse_smart_wallet_xlsx(b"this is not a real xlsx file")
    assert rows == []
    assert skipped == 0


class _FileRes:
    def __init__(self, content):
        self.content = content
        self.status_code = 200

    def raise_for_status(self):
        pass


async def test_import_run_routes_xlsx_filename_to_the_xlsx_parser():
    data = _xlsx_bytes([["0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "Degen"]], headers=("Address", "Tag"))
    posted_chunks = []
    followups = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return _FileRes(data)

        async def post(self, url, headers=None, json=None):
            posted_chunks.append(json)
            return FakeRes(200, {})

    async def fake_sync(client, force=False):
        assert force is True
        return {}

    async def fake_followup(token, data):
        followups.append(data)

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main, "_alchemy_webhook_sync_addresses", new=fake_sync), \
         patch.object(main, "_discord_followup_patch", new=fake_followup):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main._smart_wallets_import_run("tok", "http://file", filename="wallets.xlsx")

    assert posted_chunks[0][0]["tag"] == "Degen"
    assert "Imported 1 row" in followups[0]["content"]


# ── _smart_wallet_bulk_apply ───────────────────────────────────────────────

async def test_bulk_apply_approve_updates_status_and_inserts_tags():
    patched = []
    posted = []

    class FakeClient:
        async def patch(self, url, headers=None, params=None, json=None):
            patched.append((params, json))
            return FakeRes(200, {})

        async def post(self, url, headers=None, json=None):
            posted.append(json)
            return FakeRes(200, {})

    rows = [_submission(id_="s1"), _submission(id_="s2", address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")]
    count = await main._smart_wallet_bulk_apply(FakeClient(), rows, approve=True, actor_id="staff1")

    assert count == 2
    assert patched[0][1]["status"] == "approved"
    assert patched[0][1]["review_batch_id"] is None
    assert "s1" in patched[0][0]["id"] and "s2" in patched[0][0]["id"]
    assert len(posted[0]) == 2
    assert posted[0][0]["address"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


async def test_bulk_apply_decline_updates_status_without_inserting_tags():
    patched = []

    class FakeClient:
        async def patch(self, url, headers=None, params=None, json=None):
            patched.append(json)
            return FakeRes(200, {})

        async def post(self, url, headers=None, json=None):
            raise AssertionError("should not insert into smart_wallet_tags on decline")

    count = await main._smart_wallet_bulk_apply(FakeClient(), [_submission()], approve=False, actor_id="staff1")
    assert count == 1
    assert patched[0]["status"] == "rejected"


async def test_bulk_apply_with_no_rows_does_nothing():
    class FakeClient:
        async def patch(self, url, headers=None, params=None, json=None):
            raise AssertionError("should not patch with zero rows")

    count = await main._smart_wallet_bulk_apply(FakeClient(), [], approve=True, actor_id="staff1")
    assert count == 0


# ── overview embed ─────────────────────────────────────────────────────────

def test_pending_overview_empty_shows_no_action_buttons():
    embed, components = main._smart_wallets_pending_overview_embed_and_components([])
    assert "clear" in embed["description"].lower()
    assert components == []


def test_pending_overview_lists_a_preview_and_action_buttons():
    rows = [_submission(id_=f"s{i}", address=f"0x{i:040x}") for i in range(3)]
    embed, components = main._smart_wallets_pending_overview_embed_and_components(rows)
    assert "3 pending" in embed["footer"]["text"]
    assert len(components[0]["components"]) == 3
    labels = {c["label"] for c in components[0]["components"]}
    assert labels == {"✅ Approve All", "❌ Decline All", "🔍 Review & Select"}


def test_pending_overview_truncates_a_long_preview():
    rows = [_submission(id_=f"s{i}", address=f"0x{i:040x}") for i in range(15)]
    embed, _ = main._smart_wallets_pending_overview_embed_and_components(rows)
    assert "…and 5 more" in embed["description"]


# ── Approve All / Decline All prompt + confirm + execute ──────────────────

async def test_all_prompt_button_requires_team_member():
    result = await main._handle_smart_wallets_pending_all_prompt_button(_payload(permissions="0"), approve=True)
    assert result["type"] == 4
    assert "team members only" in result["data"]["content"].lower()


async def test_all_prompt_button_shows_confirmation_with_real_count():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [_submission(id_="s1"), _submission(id_="s2")])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_smart_wallets_pending_all_prompt_button(_payload(), approve=True)

    assert result["type"] == 7
    embed = result["data"]["embeds"][0]
    assert "2" in embed["description"]
    buttons = result["data"]["components"][0]["components"]
    assert buttons[0]["custom_id"] == "walletpending_confirm:approve"


async def test_all_prompt_button_with_zero_pending_shows_overview_instead():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_smart_wallets_pending_all_prompt_button(_payload(), approve=False)

    assert result["data"]["components"] == []


async def test_confirm_button_dispatches_worker_and_defers():
    dispatched = {}

    async def fake_dispatch(**kwargs):
        dispatched.update(kwargs)

    with patch.object(main, "_dispatch_smart_wallets_worker", new=fake_dispatch):
        result = await main._handle_smart_wallets_pending_confirm_button(_payload(), approve=True)

    assert result == {"type": 6}
    assert dispatched["action"] == "pending_bulk_all"
    assert dispatched["approve"] is True
    assert dispatched["actor_id"] == "staff1"


async def test_bulk_all_run_syncs_once_when_approving_with_results():
    sync_calls = []
    edits = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [_submission(id_="s1"), _submission(id_="s2", address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")])

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, {})

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

    async def fake_sync(client, force=False):
        sync_calls.append(force)

    async def fake_edit(token, body):
        edits.append(body)

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main, "_alchemy_webhook_sync_addresses", new=fake_sync), \
         patch.object(main, "_discord_edit_original_raw", new=fake_edit):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main._smart_wallets_pending_bulk_all_run("tok", approve=True, actor_id="staff1")

    assert sync_calls == [True]
    assert "Approved 2" in edits[0]["embeds"][0]["description"]


async def test_bulk_all_run_does_not_sync_on_decline():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [_submission()])

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, {})

    async def fake_sync(client, force=False):
        raise AssertionError("should not sync on decline")

    async def fake_edit(token, body):
        pass

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main, "_alchemy_webhook_sync_addresses", new=fake_sync), \
         patch.object(main, "_discord_edit_original_raw", new=fake_edit):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main._smart_wallets_pending_bulk_all_run("tok", approve=False, actor_id="staff1")


async def test_bulk_all_run_reports_nothing_pending_gracefully():
    edits = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])

    async def fake_edit(token, body):
        edits.append(body)

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main, "_discord_edit_original_raw", new=fake_edit):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main._smart_wallets_pending_bulk_all_run("tok", approve=True, actor_id="staff1")

    assert "Nothing was pending" in edits[0]["embeds"][0]["description"]


async def test_cancel_button_reverts_to_the_overview():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [_submission()])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_smart_wallets_pending_cancel_button(_payload())

    assert result["type"] == 7
    assert "Pending Wallet Submissions" in result["data"]["embeds"][0]["title"]


# ── Review & Select ─────────────────────────────────────────────────────────

def test_review_select_component_shape():
    rows = [_submission(id_="s1"), _submission(id_="s2", address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")]
    components = main._smart_wallets_review_select_component(rows)
    select = components[0]["components"][0]
    assert select["custom_id"] == "walletpending_select"
    assert select["min_values"] == 1
    assert select["max_values"] == 2
    assert {o["value"] for o in select["options"]} == {"s1", "s2"}


def test_review_select_component_caps_at_25_options():
    rows = [_submission(id_=f"s{i}", address=f"0x{i:040x}") for i in range(40)]
    components = main._smart_wallets_review_select_component(rows)
    assert len(components[0]["components"][0]["options"]) == 25


async def test_review_button_requires_team_member():
    result = await main._handle_smart_wallets_pending_review_button(_payload(permissions="0"))
    assert result["type"] == 4


async def test_select_submission_requires_a_value():
    result = await main._handle_smart_wallets_pending_select(_payload(values=[]))
    assert result["type"] == 4


async def test_select_submission_dispatches_worker_with_chosen_ids():
    dispatched = {}

    async def fake_dispatch(**kwargs):
        dispatched.update(kwargs)

    with patch.object(main, "_dispatch_smart_wallets_worker", new=fake_dispatch):
        result = await main._handle_smart_wallets_pending_select(_payload(values=["s1", "s2"]))

    assert result == {"type": 6}
    assert dispatched["action"] == "pending_batch_detail"
    assert dispatched["submission_ids"] == ["s1", "s2"]


async def test_batch_detail_run_builds_embeds_and_stamps_batch_token():
    patched = []
    edits = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [_submission(id_="s1"), _submission(id_="s2", address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")])

        async def patch(self, url, headers=None, params=None, json=None):
            patched.append((params, json))
            return FakeRes(200, {})

    async def fake_assessment(client, address):
        return "🤔 test assessment"

    async def fake_edit(token, body):
        edits.append(body)

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main, "_wallet_assessment", new=fake_assessment), \
         patch.object(main, "_discord_edit_original_raw", new=fake_edit):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main._smart_wallets_pending_batch_detail_run("tok", ["s1", "s2"], "staff1")

    assert len(edits[0]["embeds"]) == 2
    assert "s1" in patched[0][0]["id"] and "s2" in patched[0][0]["id"]
    stamped_token = patched[0][1]["review_batch_id"]
    assert len(stamped_token) == 8  # secrets.token_hex(4)
    buttons = edits[0]["components"][0]["components"]
    assert buttons[0]["custom_id"] == f"walletpending_batch_accept:{stamped_token}"
    assert buttons[1]["custom_id"] == f"walletpending_batch_decline:{stamped_token}"


async def test_batch_detail_run_handles_a_race_where_nothing_is_pending_anymore():
    edits = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])  # already reviewed by someone else in the meantime

    async def fake_edit(token, body):
        edits.append(body)

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main, "_discord_edit_original_raw", new=fake_edit):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main._smart_wallets_pending_batch_detail_run("tok", ["s1"], "staff1")

    assert "no longer pending" in edits[0]["embeds"][0]["description"].lower()
    assert edits[0]["components"] == []


async def test_batch_button_requires_a_token():
    result = await main._handle_smart_wallets_pending_batch_button(_payload(), "", approve=True)
    assert result["type"] == 4


async def test_batch_button_dispatches_worker():
    dispatched = {}

    async def fake_dispatch(**kwargs):
        dispatched.update(kwargs)

    with patch.object(main, "_dispatch_smart_wallets_worker", new=fake_dispatch):
        result = await main._handle_smart_wallets_pending_batch_button(_payload(), "abcd1234", approve=False)

    assert result == {"type": 6}
    assert dispatched == {"action": "pending_batch_execute", "token": "tok1", "batch_token": "abcd1234", "approve": False, "actor_id": "staff1"}


async def test_batch_execute_run_only_processes_matching_batch_token():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            assert params["review_batch_id"] == "eq.abcd1234"
            return FakeRes(200, [_submission(id_="s1")])

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, {})

        async def post(self, url, headers=None, json=None):
            return FakeRes(200, {})

    edits = []

    async def fake_sync(client, force=False):
        pass

    async def fake_edit(token, body):
        edits.append(body)

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main, "_alchemy_webhook_sync_addresses", new=fake_sync), \
         patch.object(main, "_discord_edit_original_raw", new=fake_edit):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main._smart_wallets_pending_batch_execute_run("tok", "abcd1234", approve=True, actor_id="staff1")

    assert "Accepted 1" in edits[0]["embeds"][0]["description"]


# ── /smart-wallets pending command ─────────────────────────────────────────

async def test_pending_command_requires_team_member():
    result = await main._handle_smart_wallets_pending_command(_payload(permissions="0"))
    assert result["type"] == 4


async def test_pending_command_requires_the_review_channel_when_configured():
    with patch.object(settings, "discord_wallet_review_channel_id", "review-chan"):
        result = await main._handle_smart_wallets_pending_command(_payload(channel_id="somewhere-else"))
    assert result["type"] == 4
    assert "review-chan" in result["data"]["content"]


async def test_pending_command_defers_and_dispatches():
    dispatched = {}
    acked = []

    async def fake_dispatch(**kwargs):
        dispatched.update(kwargs)

    async def fake_ack(interaction_id, token, ephemeral=False):
        acked.append((interaction_id, token, ephemeral))

    with patch.object(settings, "discord_wallet_review_channel_id", ""), \
         patch.object(main, "_dispatch_smart_wallets_worker", new=fake_dispatch), \
         patch.object(main, "_discord_deferred_ack", new=fake_ack):
        result = await main._handle_smart_wallets_pending_command(_payload())

    assert result == {"type": 5}
    assert dispatched["action"] == "pending"
    assert acked[0][2] is True
