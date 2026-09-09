"""Tests for Smart Wallet Tags - the staff-imported, credentialed wallet
leaderboard that replaced NFT Intel. Covers the multi-shape import parser,
the NFT Scope cross-reference signal (hits/points/wallet_signals/score
integration), the /xray enrichment, and the command handlers' team-only
gates."""
from unittest.mock import patch

import main


class FakeRes:
    def __init__(self, status_code=200, json_data=None, text_data=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []
        self.text = text_data

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.httpx.HTTPStatusError("boom", request=None, response=self)


def _payload(permissions="0", options=None, resolved=None):
    return {
        "id": "int1", "token": "tok1",
        "member": {"permissions": permissions, "user": {"id": "u1", "username": "someone"}},
        "data": {"name": "smart-wallets", "options": options or [], "resolved": resolved or {}},
    }


# ── _parse_smart_wallet_import: Notion-block shape ──────────────────────

def test_parses_a_single_notion_block():
    text = (
        "# 0x014D22878be05d5ab76237de4650fec467206D3D\n"
        "Explorer: https://hyperevmscan.io/address/0x014d...\n"
        "OpenSea: https://opensea.io/0x014d...\n"
        "Rank: 59\n"
        "Tag: Hype Brokers"
    )
    rows, skipped = main._parse_smart_wallet_import(text)
    assert skipped == 0
    assert rows == [{"address": "0x014d22878be05d5ab76237de4650fec467206d3d", "tag": "Hype Brokers", "rank": 59, "pnl": None}]


def test_parses_multiple_concatenated_notion_blocks():
    text = (
        "# 0x00179a311d6b239f2d367372b0dd7799946e6ab6\n"
        "Explorer: https://robinhoodchain.blockscout.com/address/...\n"
        "OpenSea: https://opensea.io/...\n"
        "Rank: 1\n"
        "Tag: RH MACHINES\n"
        "\n"
        "# 0xabc000000000000000000000000000000000000a\n"
        "Rank: 3\n"
        "Tag: COOL\n"
    )
    rows, skipped = main._parse_smart_wallet_import(text)
    assert skipped == 0
    assert len(rows) == 2
    assert rows[0] == {"address": "0x00179a311d6b239f2d367372b0dd7799946e6ab6", "tag": "RH MACHINES", "rank": 1, "pnl": None}
    assert rows[1] == {"address": "0xabc000000000000000000000000000000000000a", "tag": "COOL", "rank": 3, "pnl": None}


def test_notion_block_with_multi_tag_line_explodes_into_multiple_rows():
    text = "# 0xabc000000000000000000000000000000000000a\nRank: 5\nTag: Top 4 COOL | Top 13 Architects\n"
    rows, _ = main._parse_smart_wallet_import(text)
    tags = {r["tag"] for r in rows}
    assert tags == {"Top 4 COOL", "Top 13 Architects"}
    assert all(r["rank"] == 5 for r in rows)  # the block's own Rank: applies to every exploded tag


def test_notion_block_missing_tag_line_is_skipped():
    text = "# 0xabc000000000000000000000000000000000000a\nRank: 5\n"
    rows, skipped = main._parse_smart_wallet_import(text)
    assert rows == []
    assert skipped == 1


# ── _parse_smart_wallet_import: TSV with explicit comma tag list ────────

def test_parses_tsv_with_explicit_tag_list():
    line = "0xFCC6899d35ef5682899378a01fd1de9111f2a394\tEarly REALCOIN @$139k\t10.19\tREALCOIN"
    rows, skipped = main._parse_smart_wallet_import(line)
    assert skipped == 0
    assert rows == [{"address": "0xfcc6899d35ef5682899378a01fd1de9111f2a394", "tag": "REALCOIN", "rank": None, "pnl": 10.19}]


def test_tsv_multi_tag_row_explodes_one_row_per_tag_sharing_pnl():
    line = "0xe849b046cd3de4b619a2a1b90c1bb6ca35ecff7c\tsome label\t21254.11\t1$,FEFER,REALCOIN"
    rows, _ = main._parse_smart_wallet_import(line)
    assert len(rows) == 3
    assert {r["tag"] for r in rows} == {"1$", "FEFER", "REALCOIN"}
    assert all(r["pnl"] == 21254.11 for r in rows)
    assert all(r["rank"] is None for r in rows)


def test_tsv_unparseable_pnl_becomes_none_not_a_skip():
    line = "0xe849b046cd3de4b619a2a1b90c1bb6ca35ecff7c\tlabel\tn/a\tREALCOIN"
    rows, skipped = main._parse_smart_wallet_import(line)
    assert skipped == 0
    assert rows[0]["pnl"] is None


# ── _parse_smart_wallet_import: TSV with a trailing URL (label-derived) ──

def test_tsv_url_shape_derives_tag_from_top_n_pattern():
    line = "0xb70a3be7d9af9a4121cfd80e66c392674f7adbf5\tTop 6 COOL\t0\thttps://gmgn.ai/arc/address/0xb70a..."
    rows, _ = main._parse_smart_wallet_import(line)
    assert rows == [{"address": "0xb70a3be7d9af9a4121cfd80e66c392674f7adbf5", "tag": "COOL", "rank": 6, "pnl": 0.0}]


def test_tsv_url_shape_derives_tag_from_early_at_price_pattern():
    line = "0xd70e9bfaaa0d2f81acb48670129578f8e82e3ab3\tEarly ARCANINE @$12k\t1286\thttps://gmgn.ai/arc/address/0xd70e..."
    rows, _ = main._parse_smart_wallet_import(line)
    assert rows == [{"address": "0xd70e9bfaaa0d2f81acb48670129578f8e82e3ab3", "tag": "ARCANINE", "rank": None, "pnl": 1286.0}]


def test_tsv_url_shape_derives_tag_from_multiplier_pattern():
    line = "0x9dafaa41c493fda22195c86fb77d75eb62a901bc\tTop 4 FEFER • 3.1x FEFER +$28.3K\t28279\thttps://x.example/a"
    rows, _ = main._parse_smart_wallet_import(line)
    tags = [r["tag"] for r in rows]
    assert tags == ["FEFER", "FEFER"]  # "Top 4 FEFER" and "3.1x FEFER +$28.3K" both resolve to FEFER
    assert rows[0]["rank"] == 4
    assert rows[1]["rank"] is None


def test_tsv_url_shape_handles_pipe_separated_multi_tag_label():
    line = "0x3c7423c3f5392a8e9f28a6234732e45fee6d5c5a\tTop 13 ARCANINE | Top 23 Architects\t0\thttps://gmgn.ai/arc/address/0x3c74..."
    rows, _ = main._parse_smart_wallet_import(line)
    assert {r["tag"] for r in rows} == {"ARCANINE", "Architects"}


def test_tsv_url_shape_falls_back_to_raw_segment_when_no_pattern_matches():
    line = "0x3c7423c3f5392a8e9f28a6234732e45fee6d5c5a\tUnrecognized Credential Text\t0\thttps://gmgn.ai/arc/address/0x3c74..."
    rows, _ = main._parse_smart_wallet_import(line)
    assert rows == [{"address": "0x3c7423c3f5392a8e9f28a6234732e45fee6d5c5a", "tag": "Unrecognized Credential Text", "rank": None, "pnl": 0.0}]


# ── _parse_smart_wallet_import: malformed input ──────────────────────────

def test_invalid_address_is_skipped_not_raised():
    rows, skipped = main._parse_smart_wallet_import("not-an-address\tlabel\t0\tTAG")
    assert rows == []
    assert skipped == 1


def test_too_few_fields_is_skipped():
    rows, skipped = main._parse_smart_wallet_import("0xabc000000000000000000000000000000000000a\tonly two")
    assert rows == []
    assert skipped == 1


def test_blank_lines_are_ignored_without_counting_as_skipped():
    rows, skipped = main._parse_smart_wallet_import("\n\n   \n")
    assert rows == []
    assert skipped == 0


# ── _parse_smart_wallet_import: 5-column "rank, address, urls, project" ──

def test_parses_rank_leading_five_column_shape():
    line = (
        "1\t0x5ab2d1f5069dd2f9aeec3b0a8e923b1cdbe7fc44\t"
        "https://robinhoodchain.blockscout.com/address/0x5ab2...\t"
        "https://opensea.io/0x5ab2...\tProject Mars Land"
    )
    rows, skipped = main._parse_smart_wallet_import(line)
    assert skipped == 0
    assert rows == [{"address": "0x5ab2d1f5069dd2f9aeec3b0a8e923b1cdbe7fc44", "tag": "Project Mars Land", "rank": 1, "pnl": None}]


def test_five_column_shape_parses_across_multiple_lines():
    text = (
        "1\t0x5ab2d1f5069dd2f9aeec3b0a8e923b1cdbe7fc44\thttps://x/a\thttps://y/a\tProject Mars Land\n"
        "2\t0x01e2db782eee4d164217129922446786d73cd4c1\thttps://x/b\thttps://y/b\tProject Mars Land\n"
    )
    rows, skipped = main._parse_smart_wallet_import(text)
    assert skipped == 0
    assert len(rows) == 2
    assert [r["rank"] for r in rows] == [1, 2]
    assert all(r["tag"] == "Project Mars Land" for r in rows)


def test_five_column_shape_does_not_misfire_on_the_four_column_gmgn_shape():
    # Guards against the new detection swallowing the existing 4-column
    # shapes - "0xfcc..." is not numeric, so this must fall through to the
    # ordinary address-first TSV branch, not the rank-first one.
    line = "0xfcc6899d35ef5682899378a01fd1de9111f2a394\tEarly REALCOIN @$139k\t10.19\tREALCOIN"
    rows, _ = main._parse_smart_wallet_import(line)
    assert rows == [{"address": "0xfcc6899d35ef5682899378a01fd1de9111f2a394", "tag": "REALCOIN", "rank": None, "pnl": 10.19}]


# ── _smart_wallet_tags_for_address ───────────────────────────────────────

async def test_tags_for_address_returns_rows():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            assert params["address"] == "eq.0xabc"
            return FakeRes(200, [{"tag": "REALCOIN", "rank": 6, "pnl": 10.19}])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        rows = await main._smart_wallet_tags_for_address("0xABC")
    assert rows == [{"tag": "REALCOIN", "rank": 6, "pnl": 10.19}]


async def test_tags_for_address_fails_safe_on_error():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            raise main.httpx.HTTPError("boom")

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        rows = await main._smart_wallet_tags_for_address("0xabc")
    assert rows == []


# ── _nft_scope_tracked_wallet_hits ────────────────────────────────────────

async def test_tracked_wallet_hits_empty_with_no_buyer_addresses():
    assert await main._nft_scope_tracked_wallet_hits(main.httpx.AsyncClient(), {"buyer_addresses": []}) == []
    assert await main._nft_scope_tracked_wallet_hits(main.httpx.AsyncClient(), None) == []


async def test_tracked_wallet_hits_queries_and_returns_rows():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            assert url.endswith("/smart_wallet_tags")
            assert params["address"] == "in.(0xaaa,0xbbb)"
            return FakeRes(200, [{"address": "0xaaa", "tag": "REALCOIN", "rank": 6, "pnl": 10.19}])

    hits = await main._nft_scope_tracked_wallet_hits(FakeClient(), {"buyer_addresses": ["0xAAA", "0xBBB"]})
    assert hits == [{"address": "0xaaa", "tag": "REALCOIN", "rank": 6, "pnl": 10.19}]


async def test_tracked_wallet_hits_fails_safe_on_query_error():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            raise main.httpx.HTTPError("boom")

    hits = await main._nft_scope_tracked_wallet_hits(FakeClient(), {"buyer_addresses": ["0xaaa"]})
    assert hits == []


# ── _nft_scope_tracked_wallet_points ──────────────────────────────────────

def test_tracked_wallet_points_zero_with_no_hits():
    points, reasons = main._nft_scope_tracked_wallet_points([])
    assert points == 0 and reasons == []
    points, reasons = main._nft_scope_tracked_wallet_points(None)
    assert points == 0 and reasons == []


def test_tracked_wallet_points_single_wallet_names_the_tag_not_the_address():
    hits = [{"address": "0xdeadbeef", "tag": "RH MACHINES", "rank": 1, "pnl": None}]
    points, reasons = main._nft_scope_tracked_wallet_points(hits)
    assert points == main._NFT_SCOPE_TRACKED_WALLET_BONUS_POINTS
    assert len(reasons) == 1
    assert "RH MACHINES" in reasons[0]
    assert "0xdeadbeef" not in reasons[0]


def test_tracked_wallet_points_convergence_bonus_scales_and_caps():
    single = main._nft_scope_tracked_wallet_points([{"address": "0xa", "tag": "T", "rank": None, "pnl": None}])
    double = main._nft_scope_tracked_wallet_points([
        {"address": "0xa", "tag": "T1", "rank": None, "pnl": None},
        {"address": "0xb", "tag": "T2", "rank": None, "pnl": None},
    ])
    many = main._nft_scope_tracked_wallet_points([{"address": f"0x{i}", "tag": f"T{i}", "rank": None, "pnl": None} for i in range(20)])
    assert single[0] < double[0]
    assert any("converging" in r.lower() for r in double[1])
    convergence_component = many[0] - main._NFT_SCOPE_TRACKED_WALLET_BONUS_POINTS
    assert convergence_component == main._NFT_SCOPE_TRACKED_CONVERGENCE_POINTS_CAP


def test_tracked_wallet_points_dedupes_convergence_by_distinct_address_not_row_count():
    # One wallet with two tags must count as ONE wallet converging, not two.
    hits = [
        {"address": "0xa", "tag": "T1", "rank": None, "pnl": None},
        {"address": "0xa", "tag": "T2", "rank": None, "pnl": None},
    ]
    points, reasons = main._nft_scope_tracked_wallet_points(hits)
    assert points == main._NFT_SCOPE_TRACKED_WALLET_BONUS_POINTS  # no convergence bonus
    assert not any("converging" in r.lower() for r in reasons)


# ── _nft_scope_score integration ─────────────────────────────────────────

def test_score_includes_tracked_wallet_points():
    from test_nft_scope import strong_collection
    c = strong_collection()
    hits = [{"address": "0xa", "tag": "REALCOIN", "rank": 6, "pnl": 10.19}]
    with_hits = main._nft_scope_score(c, None, tracked_wallet_hits=hits)
    without_hits = main._nft_scope_score(c, None, tracked_wallet_hits=None)
    assert with_hits["score"] > without_hits["score"]
    assert any("REALCOIN" in r for r in with_hits["reasons"])


# ── _nft_scope_wallet_signals now returns a 3-tuple ──────────────────────

async def test_wallet_signals_returns_three_way_empty_with_no_rapid_activity():
    assert await main._nft_scope_wallet_signals(main.httpx.AsyncClient(), None) == ([], [], [])
    assert await main._nft_scope_wallet_signals(main.httpx.AsyncClient(), {"buyer_addresses": []}) == ([], [], [])


async def test_wallet_signals_fetches_all_three_concurrently():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if url.endswith("/nft_smart_wallets"):
                return FakeRes(200, [])
            if url.endswith("/nft_wallet_pnl_stats"):
                return FakeRes(200, [])
            if url.endswith("/nft_wallet_recent_activity"):
                return FakeRes(200, [])
            if url.endswith("/smart_wallet_tags"):
                return FakeRes(200, [{"address": "0xa", "tag": "COOL", "rank": 1, "pnl": None}])
            raise AssertionError(f"unexpected {url}")

    smart_hits, spike_hits, tracked_hits = await main._nft_scope_wallet_signals(FakeClient(), {"buyer_addresses": ["0xa"]})
    assert smart_hits == [] and spike_hits == []
    assert tracked_hits == [{"address": "0xa", "tag": "COOL", "rank": 1, "pnl": None}]


# ── /xray enrichment ──────────────────────────────────────────────────────

async def test_cmd_xray_adds_tracked_field_when_matched():
    async def fake_core(address):
        return {
            "address": "0xabc", "ensName": None, "composite": 50, "archetype": "The Fresh Signal", "tier": {"emoji": "x", "name": "T", "color": "#000000", "flavor": "f"},
            "crypto": {"netWorthUsd": 0, "ethBalance": 0, "distinctTokens": 0, "otherChains": [], "unpricedTokens": 0, "tokenDataOk": True},
            "nft": {"collections": 0},
        }

    async def fake_tags(address):
        return [{"tag": "RH MACHINES", "rank": 1, "pnl": None}]

    with patch.object(main, "_wallet_xray_core", new=fake_core), \
         patch.object(main, "_smart_wallet_tags_for_address", new=fake_tags):
        embed = await main._cmd_xray("0xabc")
    tracked_field = next((f for f in embed["fields"] if f["name"] == "🏷️ Tracked As"), None)
    assert tracked_field is not None
    assert "RH MACHINES" in tracked_field["value"]
    assert "Rank 1" in tracked_field["value"]


async def test_cmd_xray_omits_tracked_field_when_not_matched():
    async def fake_core(address):
        return {
            "address": "0xabc", "ensName": None, "composite": 50, "archetype": "The Fresh Signal", "tier": {"emoji": "x", "name": "T", "color": "#000000", "flavor": "f"},
            "crypto": {"netWorthUsd": 0, "ethBalance": 0, "distinctTokens": 0, "otherChains": [], "unpricedTokens": 0, "tokenDataOk": True},
            "nft": {"collections": 0},
        }

    async def fake_tags(address):
        return []

    with patch.object(main, "_wallet_xray_core", new=fake_core), \
         patch.object(main, "_smart_wallet_tags_for_address", new=fake_tags):
        embed = await main._cmd_xray("0xabc")
    assert not any(f["name"] == "🏷️ Tracked As" for f in embed["fields"])


# ── Command handlers: team-only gates ─────────────────────────────────────

async def test_import_command_rejects_non_team_members():
    result = await main._handle_smart_wallets_import_command(_payload(permissions="0"))
    assert "team members only" in result["data"]["content"]


async def test_list_command_rejects_non_team_members():
    result = await main._handle_smart_wallets_list_command(_payload(permissions="0"))
    assert "team members only" in result["data"]["content"]


async def test_clear_command_rejects_non_team_members():
    result = await main._handle_smart_wallets_clear_command(_payload(permissions="0"))
    assert "team members only" in result["data"]["content"]


async def test_import_command_requires_an_attachment():
    payload = _payload(permissions="32", options=[{"name": "import", "options": []}])
    result = await main._handle_smart_wallets_import_command(payload)
    assert "No file attached" in result["data"]["content"]


async def test_handle_smart_wallets_command_routes_by_subcommand():
    payload = _payload(permissions="0", options=[{"name": "list"}])
    result = await main._handle_smart_wallets_command(payload)
    assert "team members only" in result["data"]["content"]  # proves it reached the list handler's own gate


# ── /discord/smart-wallets-worker ─────────────────────────────────────────

async def test_worker_endpoint_rejects_missing_secret():
    class FakeRequest:
        headers = {}

        async def json(self):
            return {}

    try:
        await main.discord_smart_wallets_worker(FakeRequest())
        assert False, "should have raised"
    except main.HTTPException as exc:
        assert exc.status_code == 401


async def test_worker_endpoint_imports_and_reports_a_summary():
    patched = {}

    class FakeClient:
        async def get(self, url, *args, **kwargs):
            return FakeRes(200, text_data="0xabc000000000000000000000000000000000000a\tlabel\t1.0\tCOOL")

        async def post(self, url, headers=None, json=None):
            patched["upserted"] = json
            return FakeRes(200)

    async def fake_followup(token, data):
        patched["followup"] = data

    class FakeRequest:
        headers = {"X-Internal-Secret": main.settings.cron_secret}

        async def json(self):
            return {"token": "tok1", "file_url": "https://cdn.discordapp.com/attachments/x/y/z.txt"}

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main, "_discord_followup_patch", new=fake_followup):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main.discord_smart_wallets_worker(FakeRequest())

    assert patched["upserted"] == [{"address": "0xabc000000000000000000000000000000000000a", "tag": "COOL", "rank": None, "pnl": 1.0, "source": "discord-import"}]
    assert "Imported 1 row" in patched["followup"]["content"]
