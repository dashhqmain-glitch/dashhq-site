"""Tests for Alert Tracker v2: Alchemy Address Activity webhooks (push
detection replacing most of the old constant polling), the per-wallet
win-rate/conviction-badge/convergence-window additions to the convergence
embed, and the Alert Tracker's own self-audited track record
(alert_tracker_calls)."""
import hashlib
import hmac
import json
import os
import re
import time
from unittest.mock import patch

import main
from config import settings


def test_vercel_json_actually_routes_the_webhook_path_to_the_api():
    # Real bug, confirmed live: the FastAPI route for
    # /webhooks/alchemy-address-activity/{chain} existed and worked
    # perfectly in every test, but nothing in vercel.json forwarded that
    # URL prefix to /api/index.py - so in production Vercel's own static
    # router 404'd every single delivery before Python ever saw it, and
    # every test here (which calls the handler function directly) had no
    # way to catch that. This is the one guard against that class of bug
    # ever silently recurring for this route.
    repo_root = os.path.join(os.path.dirname(__file__), "..", "..")
    with open(os.path.join(repo_root, "vercel.json")) as f:
        config = json.load(f)
    routes = config["routes"]
    sample_path = "/webhooks/alchemy-address-activity/ethereum"
    matched = next((r for r in routes if re.fullmatch(r["src"], sample_path)), None)
    assert matched is not None, "no vercel.json route matches a real webhook URL at all"
    assert matched["dest"] == "/api/index.py", f"webhook path routes to {matched['dest']!r}, not the Python API"


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
    def __init__(self, body: bytes, signature: str | None):
        self._body = body
        self.headers = {"x-alchemy-signature": signature} if signature is not None else {}

    async def body(self):
        return self._body


# ── _format_convergence_window ────────────────────────────────────────────

def test_format_convergence_window_seconds():
    assert main._format_convergence_window(45) == "45s"


def test_format_convergence_window_minutes():
    assert main._format_convergence_window(8 * 60) == "8 min"


def test_format_convergence_window_hours():
    assert main._format_convergence_window(3 * 3600) == "3h"


def test_format_convergence_window_days():
    assert main._format_convergence_window(50 * 3600) == "2d"


# ── _verify_alchemy_webhook_signature ─────────────────────────────────────

def test_verify_webhook_signature_accepts_correct_hmac():
    settings.alchemy_webhook_signing_key_ethereum = "topsecretkey"
    body = b'{"hello":"world"}'
    sig = hmac.new(b"topsecretkey", body, hashlib.sha256).hexdigest()
    assert main._verify_alchemy_webhook_signature("ethereum", sig, body) is True


def test_verify_webhook_signature_rejects_wrong_signature():
    settings.alchemy_webhook_signing_key_ethereum = "topsecretkey"
    body = b'{"hello":"world"}'
    assert main._verify_alchemy_webhook_signature("ethereum", "0" * 64, body) is False


def test_verify_webhook_signature_rejects_when_key_not_configured():
    settings.alchemy_webhook_signing_key_polygon = ""
    body = b'{"hello":"world"}'
    sig = hmac.new(b"anything", body, hashlib.sha256).hexdigest()
    assert main._verify_alchemy_webhook_signature("polygon", sig, body) is False


def test_verify_webhook_signature_rejects_missing_signature():
    settings.alchemy_webhook_signing_key_ethereum = "topsecretkey"
    assert main._verify_alchemy_webhook_signature("ethereum", "", b"body") is False


# ── _alchemy_webhook_activity_to_mint ─────────────────────────────────────

def test_activity_to_mint_parses_erc721():
    activity = {
        "fromAddress": main._TRACKED_WALLET_NULL_ADDRESS, "toAddress": "0xBUYER",
        "category": "erc721", "erc721TokenId": "0x2a",
        "rawContract": {"address": "0xCONTRACT"},
    }
    mint = main._alchemy_webhook_activity_to_mint(activity)
    assert mint == {"buyer": "0xbuyer", "contract": "0xcontract", "token_id": "42", "event_at": mint["event_at"]}


def test_activity_to_mint_parses_erc1155():
    activity = {
        "fromAddress": main._TRACKED_WALLET_NULL_ADDRESS, "toAddress": "0xBUYER",
        "category": "erc1155", "erc1155Metadata": [{"tokenId": "0x5", "value": "0x1"}],
        "rawContract": {"address": "0xCONTRACT"},
    }
    mint = main._alchemy_webhook_activity_to_mint(activity)
    assert mint["token_id"] == "5"


def test_activity_to_mint_ignores_non_mint_transfers():
    # fromAddress isn't the null address - a secondary sale/transfer, not a mint.
    activity = {
        "fromAddress": "0xSOMEONE", "toAddress": "0xBUYER",
        "category": "erc721", "erc721TokenId": "0x2a",
        "rawContract": {"address": "0xCONTRACT"},
    }
    assert main._alchemy_webhook_activity_to_mint(activity) is None


def test_activity_to_mint_returns_none_without_contract():
    activity = {
        "fromAddress": main._TRACKED_WALLET_NULL_ADDRESS, "toAddress": "0xBUYER",
        "category": "erc721", "erc721TokenId": "0x2a", "rawContract": {},
    }
    assert main._alchemy_webhook_activity_to_mint(activity) is None


def test_activity_to_mint_recognizes_an_erc721_mint_tagged_category_token():
    # Real-world gap, not hypothetical: Alchemy's own documented examples
    # show an ERC721 transfer arriving with category "token" rather than
    # "erc721" (erc721TokenId is still present either way). Detection here
    # is structural (a real token-id field present), not a category
    # string match, specifically so this can't silently drop a real mint.
    activity = {
        "fromAddress": main._TRACKED_WALLET_NULL_ADDRESS, "toAddress": "0xBUYER",
        "category": "token", "erc721TokenId": "0x2a",
        "rawContract": {"address": "0xCONTRACT"},
    }
    mint = main._alchemy_webhook_activity_to_mint(activity)
    assert mint["token_id"] == "42"
    assert mint["contract"] == "0xcontract"


# ── /webhooks/alchemy-address-activity/{chain} ────────────────────────────

def _signed_body(chain: str, payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    key = getattr(settings, f"alchemy_webhook_signing_key_{chain}")
    sig = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    return body, sig


async def test_webhook_rejects_bad_signature():
    settings.alchemy_webhook_signing_key_ethereum = "key"
    body = json.dumps({"event": {"activity": []}}).encode()
    try:
        await main.alchemy_address_activity_webhook("ethereum", FakeRequest(body, "wrong"))
        assert False, "should have raised"
    except main.HTTPException as e:
        assert e.status_code == 401


async def test_webhook_rejects_unconfigured_chain():
    body, sig = _signed_body("ethereum", {"event": {"activity": []}})
    try:
        await main.alchemy_address_activity_webhook("optimism", FakeRequest(body, sig))
        assert False, "should have raised"
    except main.HTTPException as e:
        assert e.status_code == 401


async def test_webhook_logs_mint_and_triggers_convergence_check():
    settings.alchemy_webhook_signing_key_ethereum = "key"
    payload = {
        "event": {
            "activity": [{
                "fromAddress": main._TRACKED_WALLET_NULL_ADDRESS, "toAddress": "0xBUYER",
                "category": "erc721", "erc721TokenId": "0x1",
                "rawContract": {"address": "0xCONTRACT"},
            }]
        }
    }
    body, sig = _signed_body("ethereum", payload)
    logged = []

    async def fake_resolve(client, contract):
        assert contract == "0xcontract"
        return {"slug": "test-slug"}

    async def fake_maybe_post(client, slug):
        assert slug == "test-slug"
        return True

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            logged.append((url, json))
            return FakeRes(200)

    with patch.object(main, "_nft_resolve_by_contract", new=fake_resolve), \
         patch.object(main, "_nft_scope_maybe_post_from_slug_direct", new=fake_maybe_post), \
         patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main.alchemy_address_activity_webhook("ethereum", FakeRequest(body, sig))

    assert result["mints_logged"] == 1
    assert result["posted"] == 1
    assert result["slugs_touched"] == ["test-slug"]
    url, row = logged[0]
    assert url.endswith("/nft_sale_events_log")
    assert row["slug"] == "test-slug"
    assert row["buyer"] == "0xbuyer"
    assert row["seller"] == main._TRACKED_WALLET_NULL_ADDRESS


async def test_webhook_no_op_when_no_mints_in_payload():
    settings.alchemy_webhook_signing_key_ethereum = "key"
    payload = {"event": {"activity": [{"fromAddress": "0xSOMEONE", "toAddress": "0xB", "category": "external"}]}}
    body, sig = _signed_body("ethereum", payload)
    result = await main.alchemy_address_activity_webhook("ethereum", FakeRequest(body, sig))
    assert result == {"ok": True, "mints_logged": 0, "posted": 0}


# ── _alchemy_webhook_sync_addresses ───────────────────────────────────────

async def test_webhook_sync_skips_without_auth_token():
    settings.alchemy_webhook_auth_token = ""
    async with main.httpx.AsyncClient() as client:
        result = await main._alchemy_webhook_sync_addresses(client)
    assert result == {"skipped": "alchemy_webhook_auth_token not configured"}


def _set_webhook_sync_settings(webhook_id_ethereum="wh_eth"):
    settings.alchemy_webhook_auth_token = "token"
    settings.alchemy_webhook_id_ethereum = webhook_id_ethereum
    for chain in main._ALCHEMY_WEBHOOK_CHAINS:
        if chain != "ethereum":
            setattr(settings, f"alchemy_webhook_id_{chain}", "")


def _clear_webhook_sync_settings():
    # settings is a shared module-level singleton with no per-test reset in
    # conftest.py - leaving these set would leak into later tests, which is
    # a real hazard here specifically: unlike the placeholder Supabase URL
    # (fails fast on DNS), dashboard.alchemy.com is a real, resolvable host,
    # so a leaked auth token could send some later, unrelated test's
    # un-mocked client into a genuine outbound call instead of failing fast.
    settings.alchemy_webhook_auth_token = ""
    settings.alchemy_webhook_id_ethereum = ""


async def test_add_single_address_skips_without_auth_token():
    settings.alchemy_webhook_auth_token = ""

    class ExplodingClient:
        async def patch(self, *a, **k):
            raise AssertionError("should never make a network call")

    await main._alchemy_webhook_add_single_address(ExplodingClient(), "0xa")


async def test_add_single_address_patches_each_configured_webhook_immediately():
    # The fast path for the Approve button - direct request that a newly
    # approved wallet gets covered right away, not on the next 5-minute
    # cycle. Must stay cheap (no GET/diff, just one small PATCH per
    # configured webhook) since this runs inside a Discord interaction's
    # ~3s response window.
    _set_webhook_sync_settings()
    settings.alchemy_webhook_id_robinhood = "wh_rh"
    calls = []

    class FakeClient:
        async def patch(self, url, headers=None, json=None):
            calls.append(json)
            return FakeRes(200, {})

    try:
        await main._alchemy_webhook_add_single_address(FakeClient(), "0xnew")
    finally:
        _clear_webhook_sync_settings()
        settings.alchemy_webhook_id_robinhood = ""

    assert {"webhook_id": "wh_eth", "addresses_to_add": ["0xnew"], "addresses_to_remove": []} in calls
    assert {"webhook_id": "wh_rh", "addresses_to_add": ["0xnew"], "addresses_to_remove": []} in calls
    assert len(calls) == 2  # only the two configured webhooks, not ink/base/polygon


async def test_webhook_sync_adds_and_removes_to_match_tracked_list():
    # Add and remove go out as SEPARATE calls (not one combined body) -
    # both work fine independently against Alchemy's real API, and
    # keeping them separate is what makes per-side chunking below simple.
    _set_webhook_sync_settings()
    updates = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "smart_wallet_tags" in url:
                return FakeRes(200, [{"address": "0xa"}, {"address": "0xb"}])
            if "webhook-addresses" in url:
                return FakeRes(200, {"data": ["0xb", "0xc"], "pagination": {"cursors": {}}})
            return FakeRes(200, {})

        async def patch(self, url, headers=None, json=None):
            updates.append(json)
            return FakeRes(200, {})

    try:
        result = await main._alchemy_webhook_sync_addresses(FakeClient())
    finally:
        _clear_webhook_sync_settings()
    assert result["ethereum"] == "added 1, removed 1"
    assert updates == [
        {"webhook_id": "wh_eth", "addresses_to_add": ["0xa"], "addresses_to_remove": []},
        {"webhook_id": "wh_eth", "addresses_to_add": [], "addresses_to_remove": ["0xc"]},
    ]


async def test_webhook_sync_chunks_a_batch_over_alchemys_500_address_cap():
    # Real bug, confirmed live against Alchemy's actual API: a single call
    # with more than 500 addresses gets a 400 ("A maximum of 500 addresses
    # can be added at once"). This must split into multiple calls, not
    # send one oversized request.
    _set_webhook_sync_settings()
    tracked = [f"0x{i:040x}" for i in range(600)]
    calls = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "smart_wallet_tags" in url:
                return FakeRes(200, [{"address": a} for a in tracked])
            if "webhook-addresses" in url:
                return FakeRes(200, {"data": [], "pagination": {"cursors": {}}})
            return FakeRes(200, {})

        async def patch(self, url, headers=None, json=None):
            calls.append(json)
            return FakeRes(200, {})

    try:
        result = await main._alchemy_webhook_sync_addresses(FakeClient())
    finally:
        _clear_webhook_sync_settings()
    assert result["ethereum"] == "added 600, removed 0"
    assert len(calls) == 2  # 500 + 100, not one 600-address call
    assert len(calls[0]["addresses_to_add"]) == 500
    assert len(calls[1]["addresses_to_add"]) == 100


async def test_webhook_sync_reports_a_real_failure_instead_of_a_false_success():
    # The actual fix: _alchemy_webhook_update_addresses used to swallow a
    # failed PATCH (e.g. that same 500-cap 400) and the caller reported
    # "added N" regardless of whether it actually landed. Must now say so.
    _set_webhook_sync_settings()

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "smart_wallet_tags" in url:
                return FakeRes(200, [{"address": "0xa"}])
            if "webhook-addresses" in url:
                return FakeRes(200, {"data": [], "pagination": {"cursors": {}}})
            return FakeRes(200, {})

        async def patch(self, url, headers=None, json=None):
            return FakeRes(400, {"message": "A maximum of 500 addresses can be added at once.", "name": "ValidationError"})

    try:
        result = await main._alchemy_webhook_sync_addresses(FakeClient())
    finally:
        _clear_webhook_sync_settings()
    assert result["ethereum"] == "FAILED to add 1, removed 0"


async def test_webhook_sync_reports_in_sync_when_lists_match():
    _set_webhook_sync_settings()

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "smart_wallet_tags" in url:
                return FakeRes(200, [{"address": "0xa"}])
            if "webhook-addresses" in url:
                return FakeRes(200, {"data": ["0xa"], "pagination": {"cursors": {}}})
            return FakeRes(200, {})

        async def patch(self, url, headers=None, json=None):
            raise AssertionError("should not PATCH when already in sync")

    try:
        result = await main._alchemy_webhook_sync_addresses(FakeClient())
    finally:
        _clear_webhook_sync_settings()
    assert result["ethereum"] == "in_sync"


async def test_webhook_current_addresses_follows_pagination_cursor():
    pages = [
        {"data": ["0xa"], "pagination": {"cursors": {"after": "cursor1"}}},
        {"data": ["0xb"], "pagination": {"cursors": {}}},
    ]

    class FakeClient:
        def __init__(self):
            self.calls = 0

        async def get(self, url, headers=None, params=None):
            res = FakeRes(200, pages[self.calls])
            self.calls += 1
            return res

    settings.alchemy_webhook_auth_token = "token"
    try:
        result = await main._alchemy_webhook_current_addresses(FakeClient(), "wh1")
    finally:
        settings.alchemy_webhook_auth_token = ""
    assert result == {"0xa", "0xb"}


# ── Convergence embed: win-rate, conviction badge, time window ───────────

def _fake_collection(**overrides):
    data = {"name": "Test Collection", "slug": "test-collection", "floor": 0.05, "symbol": "ETH", "chain": "ethereum", "openseaUrl": "https://opensea.io/collection/test-collection", "image": None}
    data.update(overrides)
    return data


def _hits(*addresses):
    return [{"address": a, "tag": "T", "rank": None, "pnl": None, "category": None} for a in addresses]


def test_embed_shows_win_rate_suffix_when_track_record_present():
    hits = _hits("0xa")
    track_records = {"0xa": {"address": "0xa", "sample": 12, "wins": 8, "win_rate": 0.667, "best_pct": 4.0}}
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits, track_records=track_records)
    assert "67%" in embed["description"]
    assert "(12 calls)" in embed["description"]


def test_embed_omits_win_rate_suffix_without_a_track_record():
    hits = _hits("0xa")
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits, track_records={})
    assert "calls)" not in embed["description"]


def test_embed_conviction_badge_default_no_track_records():
    hits = _hits("0xa", "0xb")
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits)
    assert embed["title"].startswith("🌱")


def test_embed_conviction_badge_moderate_with_one_proven_wallet():
    hits = _hits("0xa", "0xb")
    track_records = {"0xa": {"address": "0xa", "sample": 6, "wins": 3, "win_rate": 0.5, "best_pct": 2.0}}
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits, track_records=track_records)
    assert embed["title"].startswith("⚡")


def test_embed_conviction_badge_strong_with_two_proven_wallets():
    hits = _hits("0xa", "0xb")
    track_records = {
        "0xa": {"address": "0xa", "sample": 6, "wins": 3, "win_rate": 0.5, "best_pct": 2.0},
        "0xb": {"address": "0xb", "sample": 8, "wins": 5, "win_rate": 0.6, "best_pct": 3.0},
    }
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits, track_records=track_records)
    assert embed["title"].startswith("🔥")


def test_embed_shows_convergence_window_with_two_or_more_event_times():
    hits = _hits("0xa", "0xb")
    event_times = {"0xa": 1_700_000_000.0, "0xb": 1_700_000_480.0}  # 8 minutes apart
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits, event_times=event_times)
    assert "Converged within 8 min" in embed["description"]


def test_embed_omits_convergence_window_with_fewer_than_two_times():
    hits = _hits("0xa", "0xb")
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits, event_times={"0xa": 1_700_000_000.0})
    assert "Converged within" not in embed["description"]


def test_embed_footer_shows_record_stat_above_minimum_sample():
    hits = _hits("0xa")
    stats = {"checked_calls": 20, "proved_calls": 12, "hit_rate": 0.6, "median_multiple": 3.0, "best_multiple": 9.0}
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits, record_stats=stats)
    assert "Alert Tracker record: 60% up on floor (20 calls)" in embed["footer"]["text"]


def test_embed_footer_omits_record_stat_below_minimum_sample():
    hits = _hits("0xa")
    stats = {"checked_calls": 2, "proved_calls": 1, "hit_rate": 0.5}
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits, record_stats=stats)
    assert "Alert Tracker record" not in embed["footer"]["text"]
    assert embed["footer"]["text"] == "Smart Wallet Convergence · NFA"


# ── alert_tracker_calls: write + prove ────────────────────────────────────

async def test_alert_tracker_record_call_posts_expected_row():
    posted = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            posted.append((url, json))
            return FakeRes(200)

    await main._alert_tracker_record_call(FakeClient(), "test-slug", 0.05, ["0xB", "0xA"])
    url, row = posted[0]
    assert url.endswith("/alert_tracker_calls")
    assert row == {"slug": "test-slug", "wallets": ["0xA", "0xB"], "floor_at_call": 0.05}


async def test_maybe_post_convergence_records_an_alert_tracker_call():
    hits = _hits("0xa", "0xb")
    calls = {}

    async def fake_recently_posted(client, slug):
        return False

    async def fake_clears_wash(client, slug):
        return True

    async def fake_post(client, channel_id, embed, content=None, components=None):
        return True

    async def noop(*a, **k):
        pass

    async def fake_record_call(client, slug, floor, wallets):
        calls["recorded"] = (slug, floor, sorted(wallets))

    with patch.object(main, "_nft_scope_recently_posted", new=fake_recently_posted), \
         patch.object(main, "_nft_scope_clears_wash_check", new=fake_clears_wash), \
         patch.object(main, "_post_channel_message", new=fake_post), \
         patch.object(main, "_nft_scope_mark_posted", new=noop), \
         patch.object(main, "_nft_scope_record_call_buyers", new=noop), \
         patch.object(main, "_alert_tracker_record_call", new=fake_record_call):
        result = await main._nft_scope_maybe_post_tracked_convergence(main.httpx.AsyncClient(), "test-slug", _fake_collection(), hits, _good_score())

    assert result is True
    assert calls["recorded"] == ("test-slug", 0.05, ["0xa", "0xb"])


def _good_score(**overrides):
    data = {"tier": "red", "blocked": False, "has_real_activity": True, "has_timeliness_signal": True}
    data.update(overrides)
    return data


async def test_prove_due_calls_marks_proved_above_threshold():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"id": 1, "slug": "winner-slug", "floor_at_call": 1.0}])

        async def patch(self, url, headers=None, params=None, json=None):
            self.patched = json
            return FakeRes(200)

    async def fake_collection_core(slug):
        assert slug == "winner-slug"
        return {"floor": 1.0 * main._NFT_SCOPE_PROVED_MULTIPLE_THRESHOLD}

    client = FakeClient()
    with patch.object(main, "_nft_collection_core", new=fake_collection_core):
        result = await main._alert_tracker_prove_due_calls(client)

    assert result == {"checked": 1, "proved": 1}
    assert client.patched["proved"] is True
    assert client.patched["multiple"] == main._NFT_SCOPE_PROVED_MULTIPLE_THRESHOLD


async def test_prove_due_calls_marks_not_proved_below_threshold():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"id": 1, "slug": "flat-slug", "floor_at_call": 1.0}])

        async def patch(self, url, headers=None, params=None, json=None):
            self.patched = json
            return FakeRes(200)

    async def fake_collection_core(slug):
        return {"floor": 1.2}  # barely moved - nowhere near the proved threshold

    client = FakeClient()
    with patch.object(main, "_nft_collection_core", new=fake_collection_core):
        result = await main._alert_tracker_prove_due_calls(client)

    assert result == {"checked": 1, "proved": 0}
    assert client.patched["proved"] is False


async def test_prove_due_calls_handles_a_collection_lookup_failure_gracefully():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"id": 1, "slug": "gone-slug", "floor_at_call": 1.0}])

        async def patch(self, url, headers=None, params=None, json=None):
            self.patched = json
            return FakeRes(200)

    async def fake_collection_core(slug):
        raise main.HTTPException(status_code=404, detail="Collection not found")

    client = FakeClient()
    with patch.object(main, "_nft_collection_core", new=fake_collection_core):
        result = await main._alert_tracker_prove_due_calls(client)

    assert result == {"checked": 1, "proved": 0}
    assert client.patched["multiple"] is None
    assert client.patched["proved"] is False


# ── /alert-tracker-record ─────────────────────────────────────────────────

def test_alert_tracker_record_embed_below_minimum_sample():
    embed = main._alert_tracker_record_embed({"checked_calls": 2, "proved_calls": 1, "hit_rate": 0.5})
    assert "Not enough resolved calls" in embed["description"]


def test_alert_tracker_record_embed_none_stats_reads_as_zero_calls():
    embed = main._alert_tracker_record_embed(None)
    assert "0/" in embed["description"]


def test_alert_tracker_record_embed_full_stats():
    stats = {"checked_calls": 30, "proved_calls": 18, "hit_rate": 0.6, "median_multiple": 4.5, "best_multiple": 15.0}
    embed = main._alert_tracker_record_embed(stats)
    assert "60%" in embed["description"]
    assert "30 calls resolved, 18 proved out" in embed["description"]
    assert "4.5x" in embed["description"]
    assert "15.0x" in embed["description"]


async def test_handle_alert_tracker_record_command_replies_publicly():
    async def fake_stats(client):
        return {"checked_calls": 30, "proved_calls": 18, "hit_rate": 0.6, "median_multiple": 4.5, "best_multiple": 15.0}

    with patch.object(main, "_alert_tracker_stats", new=fake_stats):
        result = await main._handle_alert_tracker_record_command({})

    assert result["type"] == 4
    assert "flags" not in result["data"]  # public, not ephemeral - the whole point is visibility
    assert result["data"]["embeds"][0]["title"] == "Alert Tracker Record"


# ── Weekly digest ──────────────────────────────────────────────────────────

async def test_digest_skips_while_on_cooldown():
    settings.discord_smart_wallet_channel_id = "chan1"

    async def fake_state_get(client, slug, alert_type):
        assert slug == main._ALERT_TRACKER_DIGEST_SLUG
        return {"last_alerted_at": main.datetime.now(main.timezone.utc).isoformat()}

    async def fail_if_called(*a, **k):
        raise AssertionError("should never post while on cooldown")

    with patch.object(main, "_nft_alert_state_get", new=fake_state_get), \
         patch.object(main, "_post_channel_message", new=fail_if_called):
        result = await main._alert_tracker_maybe_post_digest(main.httpx.AsyncClient())
    assert result is False


async def test_digest_skips_below_minimum_sample():
    settings.discord_smart_wallet_channel_id = "chan1"

    async def fake_state_get(client, slug, alert_type):
        return None  # never posted before - no cooldown

    async def fake_stats(client):
        return {"checked_calls": 2, "proved_calls": 1, "hit_rate": 0.5}

    async def fail_if_called(*a, **k):
        raise AssertionError("should never post an almost-empty digest")

    with patch.object(main, "_nft_alert_state_get", new=fake_state_get), \
         patch.object(main, "_alert_tracker_stats", new=fake_stats), \
         patch.object(main, "_post_channel_message", new=fail_if_called):
        result = await main._alert_tracker_maybe_post_digest(main.httpx.AsyncClient())
    assert result is False


async def test_digest_posts_and_marks_cooldown_when_due():
    settings.discord_smart_wallet_channel_id = "chan1"
    calls = {}

    async def fake_state_get(client, slug, alert_type):
        return None

    async def fake_stats(client):
        return {"checked_calls": 20, "proved_calls": 12, "hit_rate": 0.6, "median_multiple": 3.0, "best_multiple": 9.0}

    async def fake_top_calls(client, limit=3):
        return [{"slug": "winner-slug", "multiple": 9.0}]

    async def fake_post(client, channel_id, embed, content=None, components=None):
        calls["channel_id"] = channel_id
        calls["embed"] = embed
        return True

    async def fake_state_set(client, slug, alert_type, value):
        calls["marked"] = (slug, alert_type)

    with patch.object(main, "_nft_alert_state_get", new=fake_state_get), \
         patch.object(main, "_alert_tracker_stats", new=fake_stats), \
         patch.object(main, "_alert_tracker_top_proved_calls", new=fake_top_calls), \
         patch.object(main, "_post_channel_message", new=fake_post), \
         patch.object(main, "_nft_alert_state_set", new=fake_state_set):
        result = await main._alert_tracker_maybe_post_digest(main.httpx.AsyncClient())

    assert result is True
    assert calls["channel_id"] == "chan1"
    assert "winner-slug" in calls["embed"]["description"]
    assert calls["marked"] == (main._ALERT_TRACKER_DIGEST_SLUG, "posted")


async def test_digest_skips_when_channel_not_configured():
    settings.discord_smart_wallet_channel_id = ""
    result = await main._alert_tracker_maybe_post_digest(main.httpx.AsyncClient())
    assert result is False
    settings.discord_smart_wallet_channel_id = "chan1"
