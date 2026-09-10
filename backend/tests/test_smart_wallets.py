"""Tests for Smart Wallet Tags - the staff-imported, credentialed wallet
leaderboard that replaced NFT Intel. Covers the multi-shape import parser,
the NFT Scope cross-reference signal (hits/points/wallet_signals/score
integration), the /xray enrichment, and the command handlers' team-only
gates."""
from unittest.mock import patch

import main
from config import settings


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
    assert rows == [{"address": "0x014d22878be05d5ab76237de4650fec467206d3d", "tag": "Hype Brokers", "rank": 59, "pnl": None, "category": None}]


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
    assert rows[0] == {"address": "0x00179a311d6b239f2d367372b0dd7799946e6ab6", "tag": "RH MACHINES", "rank": 1, "pnl": None, "category": None}
    assert rows[1] == {"address": "0xabc000000000000000000000000000000000000a", "tag": "COOL", "rank": 3, "pnl": None, "category": None}


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


def test_mixed_notion_blocks_and_tsv_rows_in_one_file_both_parse():
    # Real bug, confirmed live: the parser used to branch ONCE on "does a
    # Notion header appear anywhere in the file," so a file starting with
    # Notion blocks and ending with pasted TSV rows silently dropped every
    # TSV row - only the Notion wallets survived. A real staff upload
    # combines sources exactly like this.
    text = (
        "# 0xabc000000000000000000000000000000000000a\n"
        "Explorer: https://example.com/a\n"
        "Rank: 1\n"
        "Tag: RH MACHINES\n"
        "\n"
        "# 0xdef000000000000000000000000000000000000b\n"
        "Rank: 2\n"
        "Tag: RH MACHINES\n"
        "\n"
        "0xfcc6899d35ef5682899378a01fd1de9111f2a394\tEarly REALCOIN @$139k\t10.19\tREALCOIN\n"
    )
    rows, skipped = main._parse_smart_wallet_import(text)
    assert skipped == 0
    addresses_and_tags = {(r["address"], r["tag"]) for r in rows}
    assert ("0xabc000000000000000000000000000000000000a", "RH MACHINES") in addresses_and_tags
    assert ("0xdef000000000000000000000000000000000000b", "RH MACHINES") in addresses_and_tags
    assert ("0xfcc6899d35ef5682899378a01fd1de9111f2a394", "REALCOIN") in addresses_and_tags
    assert len(rows) == 3


def test_a_wallet_with_two_credential_phrases_for_the_same_tag_collapses_to_one_row():
    # Real bug, confirmed live: "Top 4 Architects | Early Architects @$12k"
    # extracts to the tag "Architects" from BOTH segments, producing two
    # rows with an identical (address, tag) key - Postgres's upsert
    # rejects an ON CONFLICT update applied twice to the same key within
    # one batch, which failed an entire real import (853 rows) over just
    # two duplicated wallets. Must collapse to exactly one row, keeping
    # whichever duplicate actually carries a rank.
    line = "0xefdc7784e1e8d070399bde563521e5953d8de2fe\tTop 4 Architects | Early Architects @$12k\t5440\thttps://gmgn.ai/arc/address/0xefdc..."
    rows, skipped = main._parse_smart_wallet_import(line)
    assert skipped == 0
    assert len(rows) == 1
    assert rows[0]["address"] == "0xefdc7784e1e8d070399bde563521e5953d8de2fe"
    assert rows[0]["tag"] == "Architects"
    assert rows[0]["rank"] == 4  # kept from the "Top 4" segment, not overwritten by the rank-less "Early" one


def test_dedupe_prefers_a_rank_or_pnl_value_over_a_missing_one():
    rows = main._dedupe_smart_wallet_rows([
        {"address": "0xa", "tag": "T", "rank": None, "pnl": None},
        {"address": "0xa", "tag": "T", "rank": 3, "pnl": 12.5},
    ])
    assert len(rows) == 1
    assert rows[0]["rank"] == 3
    assert rows[0]["pnl"] == 12.5


# ── _parse_smart_wallet_import: TSV with explicit comma tag list ────────

def test_parses_tsv_with_explicit_tag_list():
    line = "0xFCC6899d35ef5682899378a01fd1de9111f2a394\tEarly REALCOIN @$139k\t10.19\tREALCOIN"
    rows, skipped = main._parse_smart_wallet_import(line)
    assert skipped == 0
    assert rows == [{"address": "0xfcc6899d35ef5682899378a01fd1de9111f2a394", "tag": "REALCOIN", "rank": None, "pnl": 10.19, "category": None}]


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
    assert rows == [{"address": "0xb70a3be7d9af9a4121cfd80e66c392674f7adbf5", "tag": "COOL", "rank": 6, "pnl": 0.0, "category": None}]


def test_tsv_url_shape_derives_tag_from_early_at_price_pattern():
    line = "0xd70e9bfaaa0d2f81acb48670129578f8e82e3ab3\tEarly ARCANINE @$12k\t1286\thttps://gmgn.ai/arc/address/0xd70e..."
    rows, _ = main._parse_smart_wallet_import(line)
    assert rows == [{"address": "0xd70e9bfaaa0d2f81acb48670129578f8e82e3ab3", "tag": "ARCANINE", "rank": None, "pnl": 1286.0, "category": None}]


def test_tsv_url_shape_derives_tag_from_multiplier_pattern():
    line = "0x9dafaa41c493fda22195c86fb77d75eb62a901bc\tTop 4 FEFER • 3.1x FEFER +$28.3K\t28279\thttps://x.example/a"
    rows, _ = main._parse_smart_wallet_import(line)
    # "Top 4 FEFER" and "3.1x FEFER +$28.3K" both resolve to the same tag
    # "FEFER" - deduped to one row (see _dedupe_smart_wallet_rows), keeping
    # the rank the "Top 4" segment carried.
    assert len(rows) == 1
    assert rows[0]["tag"] == "FEFER"
    assert rows[0]["rank"] == 4


def test_tsv_url_shape_handles_pipe_separated_multi_tag_label():
    line = "0x3c7423c3f5392a8e9f28a6234732e45fee6d5c5a\tTop 13 ARCANINE | Top 23 Architects\t0\thttps://gmgn.ai/arc/address/0x3c74..."
    rows, _ = main._parse_smart_wallet_import(line)
    assert {r["tag"] for r in rows} == {"ARCANINE", "Architects"}


def test_tsv_url_shape_falls_back_to_raw_segment_when_no_pattern_matches():
    line = "0x3c7423c3f5392a8e9f28a6234732e45fee6d5c5a\tUnrecognized Credential Text\t0\thttps://gmgn.ai/arc/address/0x3c74..."
    rows, _ = main._parse_smart_wallet_import(line)
    assert rows == [{"address": "0x3c7423c3f5392a8e9f28a6234732e45fee6d5c5a", "tag": "Unrecognized Credential Text", "rank": None, "pnl": 0.0, "category": None}]


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
    assert rows == [{"address": "0x5ab2d1f5069dd2f9aeec3b0a8e923b1cdbe7fc44", "tag": "Project Mars Land", "rank": 1, "pnl": None, "category": None}]


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
    assert rows == [{"address": "0xfcc6899d35ef5682899378a01fd1de9111f2a394", "tag": "REALCOIN", "rank": None, "pnl": 10.19, "category": None}]


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


# ── Standalone tracked-wallet convergence alert ──────────────────────────

def _fake_collection(**overrides):
    data = {"name": "Test Collection", "slug": "test-collection", "floor": 0.05, "symbol": "ETH", "chain": "ethereum", "openseaUrl": "https://opensea.io/collection/test-collection", "image": None}
    data.update(overrides)
    return data


def test_convergence_embed_is_terse_and_names_wallet_count():
    hits = [
        {"address": "0x1111111111111111111111111111111111111a", "tag": "REALCOIN", "rank": 6, "pnl": 10.19, "category": "KOL"},
        {"address": "0x2222222222222222222222222222222222222b", "tag": "RH MACHINES", "rank": 1, "pnl": None, "category": None},
    ]
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits)
    assert "2 Wallet Minting" in embed["title"]
    assert embed["author"]["name"] == "🔔 Alert Tracker"
    assert "`KOL`" in embed["description"]  # category badge shown when set
    assert "`Tracked`" in embed["description"]  # falls back to a generic badge, never blank, when category is unset
    # Each wallet's historical credential tag ("REALCOIN", "RH MACHINES")
    # is deliberately NOT shown here - it's about whatever project it was
    # imported for, unrelated to the collection actually minting right
    # now, which is what the title already names.
    assert "REALCOIN" not in embed["description"]
    assert "RH MACHINES" not in embed["description"]
    # A shortened, LINKED address is intentional here (unlike the self-
    # computed smart-wallet signal) - this list is externally curated,
    # not proprietary internal scoring, and showing which wallet matched
    # is the whole point of the alert.
    assert "opensea.io/0x1111111111111111111111111111111111111a" in embed["description"]
    assert "[View Collection]" in embed["description"]
    assert embed["color"] == main._NFT_SCOPE_TRACKED_ALERT_COLOR
    assert embed["url"] == _fake_collection()["openseaUrl"]


def test_convergence_embed_caps_wallet_rows_and_notes_the_overflow():
    hits = [{"address": f"0x{i:040x}", "tag": "T", "rank": None, "pnl": None, "category": None} for i in range(15)]
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits)
    assert "+5 more" in embed["description"]


def test_convergence_embed_shows_an_estimated_category_prefixed_with_tilde():
    hits = [{"address": "0xa", "tag": "T1", "rank": None, "pnl": None, "category": None}]
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits, estimated={"0xa": "Whale"})
    assert "`~Whale`" in embed["description"]
    assert "`Tracked`" not in embed["description"]


def test_convergence_embed_confirmed_category_wins_over_an_estimate():
    hits = [{"address": "0xa", "tag": "T1", "rank": None, "pnl": None, "category": "KOL"}]
    embed = main._nft_scope_tracked_convergence_embed(_fake_collection(), hits, estimated={"0xa": "Whale"})
    assert "`KOL`" in embed["description"]
    assert "~Whale" not in embed["description"]


# ── _estimate_wallet_categories ───────────────────────────────────────────

async def test_estimate_categories_returns_empty_for_no_addresses():
    assert await main._estimate_wallet_categories(main.httpx.AsyncClient(), []) == {}


async def test_estimate_categories_needs_a_minimum_sample():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "nft_sale_events_log" in url and "buyer" in (params or {}):
                return FakeRes(200, [{"buyer": "0xa", "slug": "s1", "price": 5.0, "event_at": "2026-01-01T00:00:00Z"}])
            return FakeRes(200, [])

    result = await main._estimate_wallet_categories(FakeClient(), ["0xa"])
    assert result == {}  # only 1 observed buy - not enough to estimate anything


async def test_estimate_categories_classifies_a_whale_by_volume():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "buyer" in (params or {}):
                return FakeRes(200, [
                    {"buyer": "0xa", "slug": "s1", "price": 1.5, "event_at": "2026-01-01T00:00:00Z"},
                    {"buyer": "0xa", "slug": "s2", "price": 1.0, "event_at": "2026-01-02T00:00:00Z"},
                ])
            return FakeRes(200, [
                {"slug": "s1", "event_at": "2026-01-01T00:00:00Z"},
                {"slug": "s2", "event_at": "2026-01-02T00:00:00Z"},
            ])

    result = await main._estimate_wallet_categories(FakeClient(), ["0xa"])
    assert result == {"0xa": "Whale"}  # 2.5 ETH total, over the whale threshold


async def test_estimate_categories_classifies_a_sniper_by_early_entry_timing():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "buyer" in (params or {}):
                return FakeRes(200, [
                    {"buyer": "0xa", "slug": "s1", "price": None, "event_at": "2026-01-01T00:00:30Z"},
                    {"buyer": "0xa", "slug": "s2", "price": None, "event_at": "2026-01-02T00:01:00Z"},
                ])
            # Every buyer's earliest recorded event per slug - 0xa bought
            # within a minute of each, well inside the 10-minute window.
            return FakeRes(200, [
                {"slug": "s1", "event_at": "2026-01-01T00:00:00Z"},
                {"slug": "s1", "event_at": "2026-01-01T00:15:00Z"},
                {"slug": "s2", "event_at": "2026-01-02T00:00:00Z"},
            ])

    result = await main._estimate_wallet_categories(FakeClient(), ["0xa"])
    assert result == {"0xa": "Sniper"}


async def test_estimate_categories_classifies_a_degen_by_distinct_collection_count():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "buyer" in (params or {}):
                return FakeRes(200, [
                    {"buyer": "0xa", "slug": f"s{i}", "price": None, "event_at": "2026-01-01T00:00:00Z"}
                    for i in range(4)
                ])
            # No early-entry signal (buys land hours after each slug's
            # earliest logged event) and no price data, so this can only
            # resolve via the distinct-collection-count path.
            return FakeRes(200, [{"slug": f"s{i}", "event_at": "2025-01-01T00:00:00Z"} for i in range(4)])

    result = await main._estimate_wallet_categories(FakeClient(), ["0xa"])
    assert result == {"0xa": "Degen"}


def test_convergence_components_link_to_opensea():
    components = main._nft_scope_tracked_convergence_components(_fake_collection())
    button = components[0]["components"][0]
    assert button["style"] == 5  # LINK style
    assert button["url"] == _fake_collection()["openseaUrl"]
    assert "custom_id" not in button


def test_convergence_components_empty_without_an_opensea_url():
    assert main._nft_scope_tracked_convergence_components({"openseaUrl": None}) == []


async def test_maybe_post_convergence_skips_below_minimum_wallets():
    async def fail_if_called(*a, **k):
        raise AssertionError("should never post below the minimum")

    with patch.object(main, "_post_channel_message", new=fail_if_called):
        result = await main._nft_scope_maybe_post_tracked_convergence(
            main.httpx.AsyncClient(), "slug", _fake_collection(),
            [{"address": "0xa", "tag": "REALCOIN", "rank": None, "pnl": None}],
        )
    assert result is False


async def test_maybe_post_convergence_skips_if_recently_posted():
    hits = [{"address": "0xa", "tag": "T1", "rank": None, "pnl": None}, {"address": "0xb", "tag": "T2", "rank": None, "pnl": None}]

    async def fake_recently_posted(client, slug):
        return True

    async def fail_if_called(*a, **k):
        raise AssertionError("should never post while on cooldown")

    with patch.object(main, "_nft_scope_recently_posted", new=fake_recently_posted), \
         patch.object(main, "_post_channel_message", new=fail_if_called):
        result = await main._nft_scope_maybe_post_tracked_convergence(main.httpx.AsyncClient(), "slug", _fake_collection(), hits)
    assert result is False


async def test_maybe_post_convergence_skips_if_wash_dirty():
    hits = [{"address": "0xa", "tag": "T1", "rank": None, "pnl": None}, {"address": "0xb", "tag": "T2", "rank": None, "pnl": None}]

    async def fake_recently_posted(client, slug):
        return False

    async def fake_clears_wash(client, slug):
        return False

    async def fail_if_called(*a, **k):
        raise AssertionError("should never post through a failed wash-check")

    with patch.object(main, "_nft_scope_recently_posted", new=fake_recently_posted), \
         patch.object(main, "_nft_scope_clears_wash_check", new=fake_clears_wash), \
         patch.object(main, "_post_channel_message", new=fail_if_called):
        result = await main._nft_scope_maybe_post_tracked_convergence(main.httpx.AsyncClient(), "slug", _fake_collection(), hits)
    assert result is False


async def test_maybe_post_convergence_posts_and_marks_shared_cooldown():
    hits = [{"address": "0xa", "tag": "T1", "rank": None, "pnl": None}, {"address": "0xb", "tag": "T2", "rank": None, "pnl": None}]
    calls = {}

    async def fake_recently_posted(client, slug):
        return False

    async def fake_clears_wash(client, slug):
        return True

    async def fake_post(client, channel_id, embed, content=None, components=None):
        calls["channel_id"] = channel_id
        calls["components"] = components
        calls["content"] = content
        return True

    async def fake_mark_posted(client, slug, value):
        calls["marked_posted"] = (slug, value)

    async def fake_record_buyers(client, slug, floor, rapid_activity):
        calls["recorded_buyers"] = (slug, floor, rapid_activity)

    with patch.object(main, "_nft_scope_recently_posted", new=fake_recently_posted), \
         patch.object(main, "_nft_scope_clears_wash_check", new=fake_clears_wash), \
         patch.object(main, "_post_channel_message", new=fake_post), \
         patch.object(main, "_nft_scope_mark_posted", new=fake_mark_posted), \
         patch.object(main, "_nft_scope_record_call_buyers", new=fake_record_buyers):
        result = await main._nft_scope_maybe_post_tracked_convergence(main.httpx.AsyncClient(), "test-slug", _fake_collection(), hits)

    assert result is True
    assert calls["channel_id"] == main.settings.discord_smart_wallet_channel_id
    assert calls["components"][0]["components"][0]["url"] == _fake_collection()["openseaUrl"]
    assert calls["marked_posted"][0] == "test-slug"
    assert calls["content"] is None  # no role configured by default - no ping
    recorded_slug, recorded_floor, recorded_rapid = calls["recorded_buyers"]
    assert recorded_slug == "test-slug"
    assert set(recorded_rapid["buyer_addresses"]) == {"0xa", "0xb"}


async def test_maybe_post_convergence_pings_the_minting_now_role_when_configured():
    hits = [{"address": "0xa", "tag": "T1", "rank": None, "pnl": None}, {"address": "0xb", "tag": "T2", "rank": None, "pnl": None}]
    calls = {}
    settings.discord_minting_now_role_id = "role999"

    async def fake_recently_posted(client, slug):
        return False

    async def fake_clears_wash(client, slug):
        return True

    async def fake_post(client, channel_id, embed, content=None, components=None):
        calls["content"] = content
        return True

    async def noop(*a, **k):
        pass

    try:
        with patch.object(main, "_nft_scope_recently_posted", new=fake_recently_posted), \
             patch.object(main, "_nft_scope_clears_wash_check", new=fake_clears_wash), \
             patch.object(main, "_post_channel_message", new=fake_post), \
             patch.object(main, "_nft_scope_mark_posted", new=noop), \
             patch.object(main, "_nft_scope_record_call_buyers", new=noop):
            await main._nft_scope_maybe_post_tracked_convergence(main.httpx.AsyncClient(), "test-slug", _fake_collection(), hits)
    finally:
        settings.discord_minting_now_role_id = ""

    assert calls["content"] == "<@&role999> 🌱 **Minting now**"


# ── Co-minter auto-discovery ──────────────────────────────────────────────

async def test_untracked_and_unsubmitted_filters_out_known_addresses():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "smart_wallet_tags" in url:
                return FakeRes(200, [{"address": "0xa"}])
            if "smart_wallet_submissions" in url:
                return FakeRes(200, [{"address": "0xb"}])
            return FakeRes(200, [])

    result = await main._nft_scope_untracked_and_unsubmitted(FakeClient(), ["0xa", "0xb", "0xc"])
    assert result == ["0xc"]


async def test_untracked_and_unsubmitted_short_circuits_on_no_addresses():
    async def fail_if_called(*a, **k):
        raise AssertionError("should never query with an empty candidate list")

    class FakeClient:
        get = fail_if_called

    assert await main._nft_scope_untracked_and_unsubmitted(FakeClient(), []) == []


async def test_auto_discover_co_minters_excludes_tracked_wallets_caps_and_posts_pending_submissions():
    settings.discord_bot_token = "tok"
    settings.discord_wallet_review_channel_id = "modchan1"
    tracked_hits = [{"address": "0xa", "tag": "REALCOIN", "category": "KOL"}]
    # 0xa is already the tracked wallet that triggered convergence, 0xb is
    # already known (tagged or pending elsewhere) - neither should get a
    # fresh submission. 7 fresh candidates remain, capped down to 5.
    rapid_activity = {"buyer_addresses": ["0xa", "0xb"] + [f"0x{i:040x}" for i in range(7)]}
    submitted_addresses = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "smart_wallet_tags" in url:
                return FakeRes(200, [])
            if "smart_wallet_submissions" in url:
                return FakeRes(200, [{"address": "0xb"}])
            return FakeRes(200, [])

        async def post(self, url, headers=None, json=None):
            if "smart_wallet_submissions" in url:
                submitted_addresses.append(json["address"])
                return FakeRes(200, [{**json, "id": f"sub-{json['address']}"}])
            if "/messages" in url:
                return FakeRes(200, {"id": "msg1"})
            return FakeRes(200, {})

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, {})

    await main._nft_scope_auto_discover_co_minters(FakeClient(), "slug", _fake_collection(), tracked_hits, rapid_activity)

    assert "0xa" not in submitted_addresses  # the tracked wallet itself
    assert "0xb" not in submitted_addresses  # already known/pending
    assert len(submitted_addresses) == main._SWT_CO_MINTER_DISCOVERY_MAX_PER_EVENT


def test_auto_discovered_submission_shows_a_bot_badge_instead_of_a_broken_mention():
    embed = main._wallet_submission_review_embed(
        {"address": "0xabc", "tag": "Co-minted Test with a tracked wallet", "category": "Degen", "submitted_by": "system"},
        "No self-computed track record yet.",
    )
    assert "🤖 Auto-discovered" in embed["description"]
    assert "<@system>" not in embed["description"]


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

async def test_import_command_without_a_file_opens_the_submission_modal_for_any_citizen():
    # No file attached is the member-facing path now (/smart-wallets import
    # with the file left blank) - open to any citizen, not staff-gated.
    result = await main._handle_smart_wallets_import_command(_payload(permissions="0"))
    assert result["type"] == 9  # MODAL
    assert result["data"]["custom_id"] == "smartwallets_submit"


async def test_import_command_with_a_file_still_rejects_non_team_members():
    payload = _payload(
        permissions="0",
        options=[{"name": "import", "options": [{"name": "file", "value": "att1"}]}],
        resolved={"attachments": {"att1": {"url": "https://example.com/f.csv"}}},
    )
    result = await main._handle_smart_wallets_import_command(payload)
    assert "team members only" in result["data"]["content"].lower()


async def test_import_command_with_a_file_still_dispatches_bulk_import_for_staff():
    dispatched = {}

    async def fake_ack(interaction_id, token, ephemeral=False):
        pass

    async def fake_dispatch(**kwargs):
        dispatched["kwargs"] = kwargs

    payload = _payload(
        permissions="32",
        options=[{"name": "import", "options": [{"name": "file", "value": "att1"}]}],
        resolved={"attachments": {"att1": {"url": "https://example.com/f.csv"}}},
    )
    with patch.object(main, "_discord_deferred_ack", new=fake_ack), \
         patch.object(main, "_dispatch_smart_wallets_worker", new=fake_dispatch):
        result = await main._handle_smart_wallets_import_command(payload)

    assert result == {"type": 5}
    assert dispatched["kwargs"]["action"] == "import"
    assert dispatched["kwargs"]["file_url"] == "https://example.com/f.csv"


async def test_list_command_rejects_non_team_members():
    result = await main._handle_smart_wallets_list_command(_payload(permissions="0"))
    assert "team members only" in result["data"]["content"]


async def test_clear_command_rejects_non_team_members():
    result = await main._handle_smart_wallets_clear_command(_payload(permissions="0"))
    assert "team members only" in result["data"]["content"]


async def test_import_command_without_a_file_opens_modal_even_for_staff():
    # Staff can use the same simple form too - only an ATTACHED file routes
    # to the bulk-import/team-only path.
    payload = _payload(permissions="32", options=[{"name": "import", "options": []}])
    result = await main._handle_smart_wallets_import_command(payload)
    assert result["type"] == 9
    assert result["data"]["custom_id"] == "smartwallets_submit"


async def test_handle_smart_wallets_command_routes_by_subcommand():
    payload = _payload(permissions="0", options=[{"name": "list"}])
    result = await main._handle_smart_wallets_command(payload)
    assert "team members only" in result["data"]["content"]  # proves it reached the list handler's own gate


# ── Member wallet submissions (the modal /smart-wallets import opens) ────

def _modal_payload(address="0xc0d1ff953a6147556dc0c309509a2b15ea13a68a", tag="Called PVP early", category="Degen", user_id="u1"):
    return {
        "id": "int1", "token": "tok1",
        "member": {"permissions": "0", "user": {"id": user_id, "username": "someone"}},
        "data": {"custom_id": "smartwallets_submit", "components": [
            {"type": 1, "components": [{"custom_id": "address", "value": address}]},
            {"type": 1, "components": [{"custom_id": "tag", "value": tag}]},
            {"type": 1, "components": [{"custom_id": "category", "value": category}]},
        ]},
    }


async def test_submit_modal_rejects_a_bad_address():
    result = await main._handle_smart_wallet_submit_modal(_modal_payload(address="not-a-wallet"))
    assert "doesn't look like a wallet address" in result["data"]["content"]


async def test_submit_modal_rejects_an_unrecognized_category():
    result = await main._handle_smart_wallet_submit_modal(_modal_payload(category="Legend"))
    assert "must be one of" in result["data"]["content"]


async def test_submit_modal_inserts_pending_row_and_posts_to_the_review_channel():
    settings.discord_bot_token = "tok"
    settings.discord_wallet_review_channel_id = "modchan1"
    inserted = {}
    posts = []
    patches = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            if "smart_wallet_submissions" in url:
                inserted["body"] = json
                return FakeRes(200, [{**json, "id": "sub1"}])
            if "nft_smart_wallets" in url or "nft_wallet_pnl_stats" in url:
                return FakeRes(200, [])
            if "/messages" in url:
                posts.append((url, json))
                return FakeRes(200, {"id": "msg1"})
            return FakeRes(200, {})

        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [])

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_smart_wallet_submit_modal(_modal_payload())

    assert result == {"type": 5}
    assert inserted["body"] == {
        "address": "0xc0d1ff953a6147556dc0c309509a2b15ea13a68a",
        "tag": "Called PVP early", "category": "Degen", "submitted_by": "u1",
    }
    review_url, review_body = posts[0]
    assert "modchan1" in review_url
    assert review_body["components"][0]["components"][0]["custom_id"] == "walletsubmit_approve:sub1"
    # The submission row gets patched with where the review embed landed,
    # so the Approve/Reject buttons later know which message to edit.
    sub_patch = next(j for u, j in patches if "smart_wallet_submissions" in u)
    assert sub_patch["discord_message_id"] == "msg1"


# ── Staff Approve/Reject on a submission ──────────────────────────────────

def _review_button_payload(custom_id, permissions="32", embeds=None):
    return {
        "member": {"permissions": permissions, "user": {"id": "staff1", "username": "mod"}},
        "data": {"custom_id": custom_id},
        "message": {"embeds": embeds or [{"title": "0xc0d1…3a68", "description": "x"}]},
    }


async def test_review_button_rejects_non_team_members():
    result = await main._handle_wallet_submission_review_button(
        _review_button_payload("walletsubmit_approve:sub1", permissions="0"), "sub1", approve=True,
    )
    assert "Team members only" in result["data"]["content"]


async def test_review_button_approve_writes_to_smart_wallet_tags_and_edits_the_message():
    patches = []
    tag_writes = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{
                "id": "sub1", "address": "0xc0d1ff953a6147556dc0c309509a2b15ea13a68a",
                "tag": "Called PVP early", "category": "Degen", "status": "pending",
            }])

        async def patch(self, url, headers=None, params=None, json=None):
            patches.append((url, json))
            return FakeRes(200, {})

        async def post(self, url, headers=None, json=None):
            tag_writes.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_wallet_submission_review_button(
            _review_button_payload("walletsubmit_approve:sub1"), "sub1", approve=True,
        )

    assert result["type"] == 7
    assert "Approved" in result["data"]["embeds"][0]["author"]["name"]
    assert result["data"]["components"] == []
    assert result["data"]["embeds"][0]["color"] == main.EMBED_COLOR_GOOD
    sub_patch = next(j for u, j in patches if "smart_wallet_submissions" in u)
    assert sub_patch["status"] == "approved"
    assert sub_patch["reviewed_by"] == "staff1"
    tags_url, tags_body = tag_writes[0]
    assert "smart_wallet_tags" in tags_url
    assert tags_body == {
        "address": "0xc0d1ff953a6147556dc0c309509a2b15ea13a68a",
        "tag": "Called PVP early", "category": "Degen", "source": "smart-wallets-submit",
    }


async def test_review_button_reject_does_not_write_to_smart_wallet_tags():
    tag_writes = []

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{
                "id": "sub1", "address": "0xc0d1ff953a6147556dc0c309509a2b15ea13a68a",
                "tag": "Called PVP early", "category": "Degen", "status": "pending",
            }])

        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, {})

        async def post(self, url, headers=None, json=None):
            tag_writes.append((url, json))
            return FakeRes(200, {})

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_wallet_submission_review_button(
            _review_button_payload("walletsubmit_reject:sub1"), "sub1", approve=False,
        )

    assert "Rejected" in result["data"]["embeds"][0]["author"]["name"]
    assert result["data"]["embeds"][0]["color"] == main.EMBED_COLOR_BAD
    assert tag_writes == []


async def test_review_button_guards_against_double_review():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"id": "sub1", "status": "approved"}])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_wallet_submission_review_button(
            _review_button_payload("walletsubmit_approve:sub1"), "sub1", approve=True,
        )

    assert "already **approved**" in result["data"]["content"].lower()


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
            return {"action": "import", "token": "tok1", "file_url": "https://cdn.discordapp.com/attachments/x/y/z.txt"}

    with patch("main.httpx.AsyncClient") as MockClient, \
         patch.object(main, "_discord_followup_patch", new=fake_followup):
        MockClient.return_value.__aenter__.return_value = FakeClient()
        await main.discord_smart_wallets_worker(FakeRequest())

    assert patched["upserted"] == [{"address": "0xabc000000000000000000000000000000000000a", "tag": "COOL", "rank": None, "pnl": 1.0, "category": None, "source": "discord-import"}]
    assert "Imported 1 row" in patched["followup"]["content"]


async def test_worker_endpoint_list_action_posts_a_followup():
    class FakeRequest:
        headers = {"X-Internal-Secret": main.settings.cron_secret}

        async def json(self):
            return {"action": "list", "token": "tok1"}

    async def fake_list_response():
        return {"embeds": [{"title": "🏷️ Smart Wallet Tags"}]}

    patched = {}

    async def fake_followup(token, data):
        patched["token"] = token
        patched["data"] = data

    with patch.object(main, "_smart_wallets_list_response", new=fake_list_response), \
         patch.object(main, "_discord_followup_patch", new=fake_followup):
        await main.discord_smart_wallets_worker(FakeRequest())

    assert patched["token"] == "tok1"
    assert patched["data"]["embeds"][0]["title"] == "🏷️ Smart Wallet Tags"


async def test_worker_endpoint_clear_action_passes_the_tag_through():
    class FakeRequest:
        headers = {"X-Internal-Secret": main.settings.cron_secret}

        async def json(self):
            return {"action": "clear", "token": "tok1", "tag": "REALCOIN"}

    seen = {}

    async def fake_clear_run(tag):
        seen["tag"] = tag
        return {"embeds": [{"title": "🗑️ Cleared"}]}

    patched = {}

    async def fake_followup(token, data):
        patched["data"] = data

    with patch.object(main, "_smart_wallets_clear_run", new=fake_clear_run), \
         patch.object(main, "_discord_followup_patch", new=fake_followup):
        await main.discord_smart_wallets_worker(FakeRequest())

    assert seen["tag"] == "REALCOIN"
    assert patched["data"]["embeds"][0]["title"] == "🗑️ Cleared"


async def test_worker_endpoint_unrecognized_action_reports_gracefully():
    class FakeRequest:
        headers = {"X-Internal-Secret": main.settings.cron_secret}

        async def json(self):
            return {"token": "tok1"}  # no action key at all

    patched = {}

    async def fake_followup(token, data):
        patched["data"] = data

    with patch.object(main, "_discord_followup_patch", new=fake_followup):
        await main.discord_smart_wallets_worker(FakeRequest())

    assert "Unrecognized" in patched["data"]["content"]


# ── /smart-wallets list and clear are now deferred, like import ─────────

async def test_list_command_defers_instead_of_answering_directly():
    dispatched = {}

    async def fake_ack(interaction_id, token, ephemeral=False):
        dispatched["acked"] = True

    async def fake_dispatch(**kwargs):
        dispatched["kwargs"] = kwargs

    with patch.object(main, "_discord_deferred_ack", new=fake_ack), \
         patch.object(main, "_dispatch_smart_wallets_worker", new=fake_dispatch):
        result = await main._handle_smart_wallets_list_command(_payload(permissions="32", options=[{"name": "list"}]))

    assert result == {"type": 5}
    assert dispatched["acked"] is True
    assert dispatched["kwargs"]["action"] == "list"


async def test_clear_command_defers_and_passes_the_tag():
    dispatched = {}

    async def fake_ack(interaction_id, token, ephemeral=False):
        pass

    async def fake_dispatch(**kwargs):
        dispatched["kwargs"] = kwargs

    payload = _payload(permissions="32", options=[{"name": "clear", "options": [{"name": "tag", "value": "REALCOIN"}]}])
    with patch.object(main, "_discord_deferred_ack", new=fake_ack), \
         patch.object(main, "_dispatch_smart_wallets_worker", new=fake_dispatch):
        result = await main._handle_smart_wallets_clear_command(payload)

    assert result == {"type": 5}
    assert dispatched["kwargs"]["action"] == "clear"
    assert dispatched["kwargs"]["tag"] == "REALCOIN"


# ── /smart-wallets set-category ──────────────────────────────────────────

def _set_category_payload(permissions, address="0xabc000000000000000000000000000000000000a", category="KOL"):
    return _payload(permissions=permissions, options=[{
        "name": "set-category",
        "options": [{"name": "address", "value": address}, {"name": "category", "value": category}],
    }])


async def test_set_category_command_rejects_non_team_members():
    result = await main._handle_smart_wallets_set_category_command(_set_category_payload(permissions="0"))
    assert "team members only" in result["data"]["content"]


async def test_set_category_command_rejects_an_invalid_address():
    result = await main._handle_smart_wallets_set_category_command(_set_category_payload(permissions="32", address="not-an-address"))
    assert "valid wallet address" in result["data"]["content"]


async def test_set_category_command_rejects_an_empty_category():
    result = await main._handle_smart_wallets_set_category_command(_set_category_payload(permissions="32", category=""))
    assert "can't be empty" in result["data"]["content"]


async def test_set_category_command_defers_and_dispatches_with_lowercased_address():
    dispatched = {}

    async def fake_ack(interaction_id, token, ephemeral=False):
        pass

    async def fake_dispatch(**kwargs):
        dispatched["kwargs"] = kwargs

    payload = _set_category_payload(permissions="32", address="0xABC000000000000000000000000000000000000A", category="Degen")
    with patch.object(main, "_discord_deferred_ack", new=fake_ack), \
         patch.object(main, "_dispatch_smart_wallets_worker", new=fake_dispatch):
        result = await main._handle_smart_wallets_set_category_command(payload)

    assert result == {"type": 5}
    assert dispatched["kwargs"]["action"] == "set_category"
    assert dispatched["kwargs"]["address"] == "0xABC000000000000000000000000000000000000A"  # case normalization happens in the run function, not here
    assert dispatched["kwargs"]["category"] == "Degen"


async def test_handle_smart_wallets_command_routes_set_category_by_subcommand_name():
    result = await main._handle_smart_wallets_command(_set_category_payload(permissions="0"))
    assert "team members only" in result["data"]["content"]  # proves it reached set-category's own gate


async def test_set_category_run_updates_matching_rows_and_reports_tags():
    class FakeClient:
        async def patch(self, url, headers=None, params=None, json=None):
            assert url.endswith("/smart_wallet_tags")
            assert params["address"] == "eq.0xabc000000000000000000000000000000000000a"
            assert json == {"category": "KOL"}
            return FakeRes(200, [
                {"address": "0xabc000000000000000000000000000000000000a", "tag": "REALCOIN", "category": "KOL"},
                {"address": "0xabc000000000000000000000000000000000000a", "tag": "FEFER", "category": "KOL"},
            ])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._smart_wallets_set_category_run("0xABC000000000000000000000000000000000000A", "KOL")

    embed = result["embeds"][0]
    assert "Category set" in embed["title"]
    assert "FEFER" in embed["description"] and "REALCOIN" in embed["description"]
    assert "KOL" in embed["description"]


async def test_set_category_run_reports_when_the_wallet_is_not_tracked():
    class FakeClient:
        async def patch(self, url, headers=None, params=None, json=None):
            return FakeRes(200, [])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._smart_wallets_set_category_run("0xabc000000000000000000000000000000000000a", "KOL")

    embed = result["embeds"][0]
    assert "Not tracked yet" in embed["title"]
    assert "import it first" in embed["description"]


async def test_worker_endpoint_set_category_action_passes_args_through():
    seen = {}

    async def fake_run(address, category):
        seen["address"] = address
        seen["category"] = category
        return {"embeds": [{"title": "✅ Category set"}]}

    patched = {}

    async def fake_followup(token, data):
        patched["data"] = data

    class FakeRequest:
        headers = {"X-Internal-Secret": main.settings.cron_secret}

        async def json(self):
            return {"action": "set_category", "token": "tok1", "address": "0xabc", "category": "KOL"}

    with patch.object(main, "_smart_wallets_set_category_run", new=fake_run), \
         patch.object(main, "_discord_followup_patch", new=fake_followup):
        await main.discord_smart_wallets_worker(FakeRequest())

    assert seen == {"address": "0xabc", "category": "KOL"}
    assert patched["data"]["embeds"][0]["title"] == "✅ Category set"
