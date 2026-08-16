"""Regression suite for NFT Scope's scoring/detection pipeline.

Every check here traces back to a real bug or a real scam call caught
during development - kept as permanent tests so a future change can't
silently reopen one of these. If you're touching _nft_scope_score,
_detect_fake_offer, _analyze_wash_trading, _detect_abnormal_turnover,
_momentum_points, or _detect_rapid_activity, run this file first.
"""
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import main


def sale(buyer, seller, token_id="1", when=None):
    return {
        "buyer": buyer,
        "seller": seller,
        "nft": {"identifier": token_id},
        "event_timestamp": when if when is not None else time.time() - 60,
        "payment": {"quantity": "1000000000000000000", "decimals": 18, "symbol": "ETH"},
    }


def sale_at_price(buyer, seller, price_eth, token_id="1"):
    return {
        "buyer": buyer, "seller": seller, "nft": {"identifier": token_id},
        "event_timestamp": time.time() - 60,
        "payment": {"quantity": str(int(price_eth * 10**18)), "decimals": 18, "symbol": "ETH"},
    }


def strong_collection(**overrides):
    data = {
        "floor": 0.05, "totalSupply": 500, "owners": 300, "sales24h": 5, "vol1d": 0.5,
        "twitter": "x", "discord": "d", "website": "w",
        "description": "A" * 50, "category": "art", "verified": True, "symbol": "ETH",
        "name": "Test Collection", "openseaUrl": "https://opensea.io/collection/test",
    }
    data.update(overrides)
    return data


def history_from(values_oldest_to_newest, key):
    """Builds a newest-first snapshot history list (matching captured_at.desc
    ordering) from a list of values given oldest->newest."""
    return [{key: v} for v in reversed(values_oldest_to_newest)]


# ── _detect_fake_offer ──────────────────────────────────────────────────

def test_fake_offer_high_ratio_zero_activity_is_flagged():
    c = strong_collection(sales24h=0, vol1d=0)
    assert main._detect_fake_offer(c, top_offer_amount=1.0) is not None


def test_fake_offer_high_ratio_with_real_sales_not_flagged():
    c = strong_collection(sales24h=3, vol1d=1.0)
    assert main._detect_fake_offer(c, top_offer_amount=1.0) is None


def test_fake_offer_modest_ratio_not_flagged():
    c = strong_collection(floor=0.5, sales24h=0, vol1d=0)
    assert main._detect_fake_offer(c, top_offer_amount=0.6) is None


def test_fake_offer_handles_missing_data():
    assert main._detect_fake_offer(strong_collection(), None) is None
    assert main._detect_fake_offer(strong_collection(floor=0), 1.0) is None
    assert main._detect_fake_offer({}, 1.0) is None


# ── _analyze_wash_trading ────────────────────────────────────────────────

def test_wash_direct_self_trade_detected():
    events = [sale("A", "A", "1"), sale("B", "C", "2"), sale("D", "E", "3"), sale("F", "G", "4")]
    result = main._analyze_wash_trading(events)
    assert result["suspicious"]
    assert any("same wallet" in r for r in result["reasons"])


def test_wash_reciprocal_ping_pong_detected():
    events = [sale("B", "A", "1"), sale("A", "B", "1"), sale("D", "C", "2"), sale("F", "E", "3")]
    result = main._analyze_wash_trading(events)
    assert result["suspicious"]
    assert any("back and forth" in r for r in result["reasons"])


def test_wash_closed_cycle_with_recirculation_detected():
    # A->B, B->C, C->A: a genuine cycle where each wallet is both a buyer
    # and a seller across the window - evades a simple pairwise check.
    events = [sale("B", "A", "1"), sale("C", "B", "2"), sale("A", "C", "3"), sale("B", "A", "4")]
    result = main._analyze_wash_trading(events)
    assert result["suspicious"]
    assert any("only ever traded with each other" in r for r in result["reasons"])


def test_wash_legit_sweep_not_falsely_flagged_as_closed_cluster():
    # Regression: one whale buying from several DISTINCT sellers who never
    # buy anything themselves must NOT trip the closed-cluster check just
    # because the wallet count is small - only recirculation should.
    events = [sale("whale", f"seller{i}", str(i)) for i in range(9)]
    result = main._analyze_wash_trading(events)
    assert not result["suspicious"], result


def test_wash_low_token_diversity_detected():
    events = [sale("A", "Z1", "1"), sale("B", "A", "1"), sale("C", "B", "1"), sale("D", "C", "1")]
    result = main._analyze_wash_trading(events)
    assert result["suspicious"]
    assert any("distinct token" in r for r in result["reasons"])


def test_wash_fully_organic_trading_not_flagged():
    events = [sale(f"buyer{i}", f"seller{i}", str(i)) for i in range(10)]
    assert not main._analyze_wash_trading(events)["suspicious"]


def test_wash_handles_empty_and_malformed_input():
    assert main._analyze_wash_trading([])["suspicious"] is False
    # Should not raise on missing/malformed fields.
    main._analyze_wash_trading([{}, {"buyer": None}, {"nft": "not-a-dict"}, {"nft": {}}])


# ── _detect_abnormal_turnover (real scam calls: CashCatForex, TRWNAT) ──

def test_turnover_blocks_real_case_cashcatforex():
    # Live false-positive: 1267 sales against 1267 total supply in 24h.
    c = {"totalSupply": 1267, "owners": 131, "sales24h": 1267}
    result = main._detect_abnormal_turnover(c)
    assert result is not None
    blocked, reason = result
    assert blocked is True
    assert "changed hands" in reason or "turned over" in reason.lower()


def test_turnover_blocks_real_case_trwnat():
    # Live false-positive: 10 sales against 10 total supply in 24h.
    c = {"totalSupply": 10, "owners": 10, "sales24h": 10}
    result = main._detect_abnormal_turnover(c)
    assert result is not None and result[0] is True


def test_turnover_catches_high_flips_per_owner_even_with_large_supply():
    # A big supply dilutes the sales/supply ratio, but sales/owners still
    # reveals churn among the actual current holder set.
    c = {"totalSupply": 100_000, "owners": 20, "sales24h": 150}
    result = main._detect_abnormal_turnover(c)
    assert result is not None and result[0] is True


def test_turnover_normal_activity_not_flagged():
    c = {"totalSupply": 5000, "owners": 3000, "sales24h": 25}
    assert main._detect_abnormal_turnover(c) is None


def test_turnover_handles_missing_and_zero_data():
    assert main._detect_abnormal_turnover({"sales24h": 0}) is None
    assert main._detect_abnormal_turnover({"sales24h": 50}) is None  # no supply/owners data at all


def test_turnover_stays_blocked_without_wash_analysis_even_at_extreme_ratio():
    # No corroborating evidence provided at all (the default, and what
    # every one of the "real scam call" tests above exercises) -> exactly
    # as conservative as before this change, regardless of how extreme.
    c = {"totalSupply": 1267, "owners": 131, "sales24h": 1267}
    result = main._detect_abnormal_turnover(c, wash_analysis=None)
    assert result is not None and result[0] is True


def test_turnover_stays_blocked_even_with_wash_analysis_if_it_says_suspicious():
    c = {"totalSupply": 1267, "owners": 131, "sales24h": 1267}
    dirty = {"suspicious": True, "unique_buyers": 40, "unique_sellers": 30}
    result = main._detect_abnormal_turnover(c, wash_analysis=dirty)
    assert result is not None and result[0] is True


def test_turnover_stays_blocked_if_corroborating_sample_has_too_few_distinct_buyers():
    c = {"totalSupply": 500, "owners": 50, "sales24h": 250}  # 50% turnover
    thin = {"suspicious": False, "unique_buyers": 1, "unique_sellers": 20}
    result = main._detect_abnormal_turnover(c, wash_analysis=thin)
    assert result is not None and result[0] is True


def test_turnover_unblocked_and_reclassified_bullish_when_corroborated_clean():
    # A real, wash-clean sweep on a small-supply collection: the same
    # elevated turnover ratio as the scam cases above, but backed by a
    # same-window wash analysis with real buyer diversity and no
    # suspicious patterns. This is the actual bug fix - this exact shape
    # (high turnover + genuine broad buying) was being silently vetoed
    # before, which is confirmed as a real cause of missed surges.
    c = {"totalSupply": 500, "owners": 50, "sales24h": 250}  # 50% turnover
    clean = {"suspicious": False, "unique_buyers": 30, "unique_sellers": 45}
    result = main._detect_abnormal_turnover(c, wash_analysis=clean)
    assert result is not None
    blocked, reason = result
    assert blocked is False
    assert "real, broad-based sweep" in reason


# ── _detect_blue_chip (NFT Scope is for secondary plays, not majors) ────

def test_blue_chip_blocks_established_majors():
    # Real owner counts in the ballpark of CryptoPunks/BAYC/Pudgy Penguins.
    for owners in (3700, 6400, 5000):
        c = {"owners": owners, "totalSupply": 10000}
        assert main._detect_blue_chip(c) is not None


def test_blue_chip_does_not_flag_small_emerging_projects():
    c = {"owners": 80, "totalSupply": 500}
    assert main._detect_blue_chip(c) is None


def test_blue_chip_threshold_boundary():
    assert main._detect_blue_chip({"owners": main._NFT_SCOPE_BLUE_CHIP_OWNERS_THRESHOLD - 1}) is None
    assert main._detect_blue_chip({"owners": main._NFT_SCOPE_BLUE_CHIP_OWNERS_THRESHOLD}) is not None


def test_blue_chip_handles_missing_data():
    assert main._detect_blue_chip({}) is None
    assert main._detect_blue_chip({"owners": None}) is None


def test_score_blocks_blue_chip_even_with_perfect_soft_signals():
    # A blue chip legitimately aces every other check (real sales, deep
    # distribution, full socials, verified) - it would otherwise be the
    # highest-scoring thing NFT Scope ever sees. The gate has to be a
    # hard block, not a deduction, or this is exactly the collection
    # that keeps winning every slot.
    bayc_shaped = strong_collection(owners=6400, totalSupply=10000, sales24h=40, vol1d=50, floor=8.0)
    score = main._nft_scope_score(bayc_shaped, 8.5)
    assert score["blocked"] is True
    assert not main._nft_scope_worth_posting(score)
    assert any("widely-held" in f for f in score["red_flags"])


def test_score_allows_small_project_below_blue_chip_threshold():
    small = strong_collection(owners=80, totalSupply=500, sales24h=3, vol1d=0.3)
    score = main._nft_scope_score(small, None)
    assert score["blocked"] is False


# ── mandatory real-activity gate accepts rapid_activity as proof too ───
# (sales24h comes from OpenSea's aggregate stats, which can lag reality
# for a collection minting fast right now - confirmed against real
# production data)

def test_real_activity_gate_accepts_rapid_activity_when_sales24h_is_stale_zero():
    rapid = {"count": 4, "unique_buyers": 4, "unique_sellers": 4, "window_minutes": 30,
             "price_surge_pct": None, "is_sharp": False, "sharp_count": 0, "sharp_window_minutes": 5}
    c = strong_collection(sales24h=0, vol1d=0, owners=50)
    score = main._nft_scope_score(c, None, rapid_activity=rapid)
    assert score["has_real_activity"] is True
    assert main._nft_scope_worth_posting(score)


def test_real_activity_gate_still_blocks_zero_sales_with_no_rapid_activity_either():
    # Without rapid_activity to back it up, a stale/zero sales24h still
    # correctly reads as "not market-tested yet" - this isn't a general
    # loosening of the gate, only rapid_activity's own independent,
    # wash-verified proof counts.
    c = strong_collection(sales24h=0, vol1d=0, owners=50)
    score = main._nft_scope_score(c, None, rapid_activity=None)
    assert score["has_real_activity"] is False
    assert not main._nft_scope_worth_posting(score)


def test_real_activity_gate_still_requires_minimum_owners_even_with_rapid_activity():
    rapid = {"count": 4, "unique_buyers": 4, "unique_sellers": 4, "window_minutes": 30,
             "price_surge_pct": None, "is_sharp": False, "sharp_count": 0, "sharp_window_minutes": 5}
    c = strong_collection(sales24h=0, vol1d=0, owners=2)
    score = main._nft_scope_score(c, None, rapid_activity=rapid)
    assert score["has_real_activity"] is False


# ── _momentum_points (floor + volume + owner-growth, aligned trends) ──

def test_momentum_all_three_signals_aligned_gives_full_points():
    n = 9
    floors = [0.05 + i * 0.003 for i in range(n)]
    vols = [1.0 + i * 0.15 for i in range(n)]
    owners = [100 + i * 8 for i in range(n)]
    c = {"floor": floors[-1] + 0.003, "vol1d": vols[-1] + 0.15, "owners": owners[-1] + 8}
    hist = [
        {"floor": f, "volume_1d": v, "owners": o}
        for f, v, o in zip(reversed(floors[:-1]), reversed(vols[:-1]), reversed(owners[:-1]))
    ]
    points, reasons = main._momentum_points(c, hist)
    assert points == 20
    assert len(reasons) == 3


def test_momentum_floor_only_gives_partial_points():
    n = 9
    floors = [0.05 + i * 0.003 for i in range(n)]
    c = {"floor": floors[-1] + 0.003, "vol1d": 2.0, "owners": 200}
    hist = [{"floor": f, "volume_1d": 2.0, "owners": 200} for f in reversed(floors[:-1])]
    points, reasons = main._momentum_points(c, hist)
    assert points == 10
    assert len(reasons) == 1 and "Floor trending up" in reasons[0]


def test_momentum_flat_everything_gives_nothing():
    c = {"floor": 0.05, "vol1d": 1.0, "owners": 100}
    hist = [{"floor": 0.05, "volume_1d": 1.0, "owners": 100} for _ in range(9)]
    points, reasons = main._momentum_points(c, hist)
    assert points == 0 and reasons == []


def test_momentum_handles_insufficient_and_empty_history():
    points, reasons = main._momentum_points({"floor": 0.06}, [{"floor": 0.05}, {"floor": 0.055}])
    assert points == 0 and reasons == []
    points, reasons = main._momentum_points({"floor": 0.05}, [])
    assert points == 0 and reasons == []


# ── mandatory real-activity gate (real bug: FUNKERS by MinimalVectors) ──

def test_zero_activity_mint_never_worth_posting_despite_good_soft_signals():
    # Live false-positive: $0 volume, 0 sales, but a decent description,
    # socials, and a listed floor were enough soft-signal points alone.
    funkers = strong_collection(sales24h=0, vol1d=0, owners=24, totalSupply=98, verified=False)
    score = main._nft_scope_score(funkers, None)
    assert not main._nft_scope_worth_posting(score)
    assert score["has_real_activity"] is False


def test_real_sales_but_too_few_owners_still_blocked():
    c = strong_collection(sales24h=3, owners=2)
    score = main._nft_scope_score(c, None)
    assert not main._nft_scope_worth_posting(score)


def test_strong_collection_with_real_activity_posts_normally():
    score = main._nft_scope_score(strong_collection(), 0.06)
    assert main._nft_scope_worth_posting(score)


# ── fake-offer and turnover blocks inside the full scoring pipeline ──

def test_score_blocks_fake_offer_regardless_of_otherwise_high_score():
    c = strong_collection(floor=0.5, sales24h=0, vol1d=0)
    score = main._nft_scope_score(c, top_offer_amount=3.0)
    assert score["blocked"] and not main._nft_scope_worth_posting(score)


def test_score_blocks_abnormal_turnover_regardless_of_otherwise_high_score():
    scam = strong_collection(floor=0.5, totalSupply=1267, owners=131, sales24h=1267, vol1d=100, symbol="USDG")
    score = main._nft_scope_score(scam, None)
    assert score["blocked"] and not main._nft_scope_worth_posting(score)


# ── rapid activity: additive bonus only, never a bypass ────────────────

async def test_rapid_activity_detects_genuine_clean_burst():
    async def fake_get(client, path, params=None):
        return {"asset_events": [sale(f"b{i}", f"s{i}", str(i)) for i in range(5)]}

    with patch.object(main, "_opensea_get", new=fake_get):
        async with main.httpx.AsyncClient() as client:
            result = await main._detect_rapid_activity(client, "test-slug")
    assert result is not None and result["count"] == 5


async def test_rapid_activity_rejects_wash_tainted_burst():
    async def fake_get(client, path, params=None):
        return {"asset_events": [sale("A", "A", str(i)) for i in range(5)]}

    with patch.object(main, "_opensea_get", new=fake_get):
        async with main.httpx.AsyncClient() as client:
            result = await main._detect_rapid_activity(client, "test-slug")
    assert result is None


def test_rapid_activity_never_bypasses_turnover_block():
    scam = strong_collection(floor=0.5, totalSupply=1267, owners=131, sales24h=1267, vol1d=100, symbol="USDG")
    fake_burst = {"count": 5, "unique_buyers": 5, "unique_sellers": 5, "window_minutes": 30}
    score = main._nft_scope_score(scam, None, rapid_activity=fake_burst)
    assert score["blocked"] and not main._nft_scope_worth_posting(score)


def test_rapid_activity_never_bypasses_mandatory_activity_gate():
    zero_activity = strong_collection(sales24h=0, owners=0)
    fake_burst = {"count": 5, "unique_buyers": 5, "unique_sellers": 5, "window_minutes": 30}
    score = main._nft_scope_score(zero_activity, None, rapid_activity=fake_burst)
    assert not main._nft_scope_worth_posting(score)


def test_rapid_activity_never_bypasses_fake_offer_block():
    fake_offer_collection = strong_collection(floor=0.5, sales24h=0, vol1d=0)
    fake_burst = {"count": 5, "unique_buyers": 5, "unique_sellers": 5, "window_minutes": 30}
    score = main._nft_scope_score(fake_offer_collection, 3.0, rapid_activity=fake_burst)
    assert score["blocked"]


def test_rapid_activity_adds_points_to_an_already_legitimate_score():
    legit = strong_collection()
    burst = {"count": 5, "unique_buyers": 5, "unique_sellers": 5, "window_minutes": 30}
    with_burst = main._nft_scope_score(legit, 0.06, rapid_activity=burst)
    without_burst = main._nft_scope_score(legit, 0.06, rapid_activity=None)
    assert with_burst["score"] > without_burst["score"]
    assert main._nft_scope_worth_posting(with_burst)


# ── 3-tier risk classification + embed rendering ────────────────────────

def test_tier_thresholds_are_internally_consistent():
    weak = main._nft_scope_score(strong_collection(owners=0, totalSupply=None, sales24h=0, description="",
                                                     category=None, twitter=None, discord=None, website=None), None)
    assert weak["tier"] == "none"

    # Every dimension maxed (distribution, trading, substance, supply,
    # and all three momentum signals aligned) should clear green (85+).
    n = 9
    floors = [0.05 + i * 0.003 for i in range(n)]
    vols = [1.0 + i * 0.15 for i in range(n)]
    owners = [100 + i * 8 for i in range(n)]
    hist = [
        {"floor": f, "volume_1d": v, "owners": o}
        for f, v, o in zip(reversed(floors[:-1]), reversed(vols[:-1]), reversed(owners[:-1]))
    ]
    c = strong_collection(verified=True, floor=floors[-1] + 0.003, vol1d=vols[-1] + 0.15, owners=owners[-1] + 8)
    strong = main._nft_scope_score(c, 0.06, history=hist)
    assert strong["tier"] == "green", strong


def test_embed_renders_safely_across_every_tier_and_edge_case():
    scenarios = [
        strong_collection(),
        strong_collection(sales24h=0, vol1d=0, verified=False),  # zero-activity
        strong_collection(floor=0.5, sales24h=0, vol1d=0),  # fake-offer-shaped
        {"name": "Empty", "openseaUrl": "x"},
    ]
    for c in scenarios:
        score = main._nft_scope_score(c, None)
        embed = main._nft_scope_embed(c, score, None, "fresh")
        assert "NFA" in embed["description"]
        embed2 = main._nft_scope_embed(c, score, None, "momentum")
        assert "NFA" in embed2["description"]


# ── _nft_scope_age_text ──────────────────────────────────────────────────

def test_age_text_buckets():
    now = datetime.now(timezone.utc)
    assert main._nft_scope_age_text(now.isoformat()) == "minted today"
    assert main._nft_scope_age_text((now - timedelta(days=5)).isoformat()) == "5 days old"
    assert "month" in main._nft_scope_age_text((now - timedelta(days=90)).isoformat())
    assert "year" in main._nft_scope_age_text((now - timedelta(days=800)).isoformat())


def test_age_text_handles_missing_and_malformed():
    assert main._nft_scope_age_text(None) is None
    assert main._nft_scope_age_text("") is None
    assert main._nft_scope_age_text("not-a-date") is None
    assert main._nft_scope_age_text("2099-01-01T00:00:00Z") is None  # future timestamp, nonsensical


# ── _nft_scope_analyst_take: genuinely distinct per real signal pattern ──

def test_analyst_take_is_distinct_per_signal_pattern():
    c = strong_collection(name="Signal Test", chain="base")
    score = main._nft_scope_score(c, None)

    sharp = {"count": 5, "unique_buyers": 4, "unique_sellers": 3, "window_minutes": 30,
             "price_surge_pct": 10.0, "is_sharp": True, "sharp_count": 3, "sharp_window_minutes": 5}
    burst = dict(sharp, is_sharp=False)

    take_sharp = main._nft_scope_analyst_take(c, score, sharp, "trending")
    take_burst = main._nft_scope_analyst_take(c, score, burst, "trending")
    take_fresh = main._nft_scope_analyst_take(c, score, None, "fresh")
    take_plain = main._nft_scope_analyst_take(c, score, None, "trending")

    takes = {take_sharp, take_burst, take_fresh, take_plain}
    assert len(takes) == 4, f"expected 4 distinct reads, got overlap: {takes}"
    for take in takes:
        assert "Signal Test" in take
        assert "Base" in take  # chain surfaced, capitalized


def test_analyst_take_handles_missing_data_without_crashing():
    assert main._nft_scope_analyst_take({}, main._nft_scope_score({}, None), None, "fresh")
    assert main._nft_scope_analyst_take({"name": "X"}, main._nft_scope_score({"name": "X"}, None), None, "momentum")


# ── Richer per-project embed content ────────────────────────────────────

def test_embed_surfaces_chain_age_category_and_full_reason_list():
    c = strong_collection(
        name="Rich Data Test", chain="arbitrum", category="art",
        createdDate=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        floorUsd=123.45, description="A" * 300,
    )
    score = main._nft_scope_score(c, 0.06)
    embed = main._nft_scope_embed(c, score, 0.06, "trending", rapid_activity=None)

    field_map = {f["name"]: f["value"] for f in embed["fields"]}
    assert field_map["Chain"] == "Arbitrum"
    assert "10 days old" == field_map["Age"]
    assert field_map["Category"] == "art"
    assert "123.45" in field_map["Floor"]
    assert "In the project's own words" in embed["description"]
    assert "…" in embed["description"]  # 300-char description gets truncated
    # Every collected reason should actually appear, not just the first few.
    assert len(score["reasons"]) > 5
    for reason in score["reasons"]:
        assert reason in embed["description"]


def test_embed_stays_within_discord_description_limit_even_with_every_signal_firing():
    # Removing the old top-5 cap on reasons means a maximal case (every
    # signal firing at once, plus red flags, plus a long description)
    # needs to be checked against Discord's real 4096-char description
    # cap instead of just trusted to fit.
    c = strong_collection(
        name="Kitchen Sink", chain="ethereum", category="art", verified=True,
        description="B" * 280, twitter="x", discord="d", website="w",
        owners=400, totalSupply=500, sales24h=20, vol1d=5.0,
    )
    history = history_from([0.02, 0.03, 0.04, 0.05, 0.06, 0.07], "floor")
    sharp = {"count": 8, "unique_buyers": 6, "unique_sellers": 5, "window_minutes": 30,
             "price_surge_pct": 40.0, "is_sharp": True, "sharp_count": 4, "sharp_window_minutes": 5}
    score = main._nft_scope_score(c, 0.08, history=history, rapid_activity=sharp)
    embed = main._nft_scope_embed(c, score, 0.08, "momentum", rapid_activity=sharp)
    assert len(embed["description"]) <= 4096


# ── rapid activity: price-surge-within-the-burst extension ─────────────

async def test_rapid_activity_computes_price_surge_from_verified_sales():
    async def fake_get(client, path, params=None):
        return {"asset_events": [
            sale_at_price(f"b{i}", f"s{i}", price, str(i))
            for i, price in enumerate([0.05, 0.055, 0.06, 0.065, 0.07])
        ]}

    with patch.object(main, "_opensea_get", new=fake_get):
        async with main.httpx.AsyncClient() as client:
            result = await main._detect_rapid_activity(client, "test-slug")
    assert result is not None
    assert 35 <= result["price_surge_pct"] <= 45


async def test_rapid_activity_flat_price_reports_near_zero_surge():
    async def fake_get(client, path, params=None):
        return {"asset_events": [sale_at_price(f"b{i}", f"s{i}", 0.05, str(i)) for i in range(5)]}

    with patch.object(main, "_opensea_get", new=fake_get):
        async with main.httpx.AsyncClient() as client:
            result = await main._detect_rapid_activity(client, "test-slug")
    assert result["price_surge_pct"] < 1


def test_genuine_price_surge_scores_higher_than_flat_price_burst():
    surge = {"count": 5, "unique_buyers": 5, "unique_sellers": 5, "window_minutes": 30, "price_surge_pct": 40.0}
    flat = {"count": 5, "unique_buyers": 5, "unique_sellers": 5, "window_minutes": 30, "price_surge_pct": 0.5}
    score_surge = main._nft_scope_score(strong_collection(), 0.06, rapid_activity=surge)
    score_flat = main._nft_scope_score(strong_collection(), 0.06, rapid_activity=flat)
    assert score_surge["score"] > score_flat["score"]
    assert any("Price climbed" in r for r in score_surge["reasons"])


def test_price_surge_never_bypasses_the_turnover_block():
    scam = strong_collection(floor=0.5, totalSupply=1267, owners=131, sales24h=1267, vol1d=100, symbol="USDG")
    surge = {"count": 5, "unique_buyers": 5, "unique_sellers": 5, "window_minutes": 30, "price_surge_pct": 40.0}
    score = main._nft_scope_score(scam, None, rapid_activity=surge)
    assert score["blocked"] and not main._nft_scope_worth_posting(score)


# ── rapid activity: sharp 5-min sub-window ("happening right now") ─────

def sale_at(buyer, seller, when, token_id="1"):
    return {
        "buyer": buyer, "seller": seller, "nft": {"identifier": token_id}, "event_timestamp": when,
        "payment": {"quantity": "1000000000000000000", "decimals": 18, "symbol": "ETH"},
    }


async def test_sharp_window_not_flagged_when_activity_is_spread_across_the_full_burst():
    now = time.time()

    async def fake_get(client, path, params=None):
        return {"asset_events": [
            sale_at("b1", "s1", now - 1700, "1"),
            sale_at("b2", "s2", now - 900, "2"),
            sale_at("b3", "s3", now - 100, "3"),  # only this one is within 5 min
        ]}

    with patch.object(main, "_opensea_get", new=fake_get):
        async with main.httpx.AsyncClient() as client:
            result = await main._detect_rapid_activity(client, "test-slug")
    assert result is not None
    assert result["is_sharp"] is False


async def test_sharp_window_flagged_when_multiple_sales_land_within_5_minutes():
    now = time.time()

    async def fake_get(client, path, params=None):
        return {"asset_events": [
            sale_at("b1", "s1", now - 1000, "1"),
            sale_at("b2", "s2", now - 200, "2"),
            sale_at("b3", "s3", now - 60, "3"),
        ]}

    with patch.object(main, "_opensea_get", new=fake_get):
        async with main.httpx.AsyncClient() as client:
            result = await main._detect_rapid_activity(client, "test-slug")
    assert result is not None
    assert result["is_sharp"] is True
    assert result["sharp_count"] == 2


async def test_sharp_window_independently_catches_a_cluster_the_diluted_outer_window_misses():
    # 10 diverse, clean trades spread across the full 30-min window,
    # plus a 4-wallet closed cycle entirely within the last 5 minutes.
    # The outer window has too many distinct wallets (24) for the
    # closed-cluster check to even apply, so it passes clean - but the
    # same 4 events, looked at on their own, are a textbook wash ring.
    now = time.time()
    diverse = [sale_at(f"b{i}", f"s{i}", now - 700 - i * 30, f"tok{i}") for i in range(10)]
    cycle = [
        sale_at("X", "W", now - 250, "c1"),
        sale_at("Y", "X", now - 180, "c2"),
        sale_at("Z", "Y", now - 100, "c3"),
        sale_at("W", "Z", now - 30, "c4"),
    ]
    events = diverse + cycle

    outer_analysis = main._analyze_wash_trading(events)
    assert outer_analysis["suspicious"] is False, "sanity check: outer window should look clean"
    inner_analysis = main._analyze_wash_trading(cycle)
    assert inner_analysis["suspicious"] is True, "sanity check: the 4-wallet cycle alone should be caught"

    async def fake_get(client, path, params=None):
        return {"asset_events": events}

    with patch.object(main, "_opensea_get", new=fake_get):
        async with main.httpx.AsyncClient() as client:
            result = await main._detect_rapid_activity(client, "test-slug")
    assert result is not None  # outer window still passes overall
    assert result["is_sharp"] is False  # but the sharp sub-window does NOT get the "happening right now" bonus


def test_sharp_signal_scores_higher_and_never_bypasses_hard_gates():
    sharp = {"count": 3, "unique_buyers": 3, "unique_sellers": 3, "window_minutes": 30,
             "price_surge_pct": 5.0, "is_sharp": True, "sharp_count": 2, "sharp_window_minutes": 5}
    not_sharp = dict(sharp, is_sharp=False)

    score_sharp = main._nft_scope_score(strong_collection(), 0.06, rapid_activity=sharp)
    score_not_sharp = main._nft_scope_score(strong_collection(), 0.06, rapid_activity=not_sharp)
    assert score_sharp["score"] > score_not_sharp["score"]
    assert any("happening right now" in r for r in score_sharp["reasons"])

    scam = strong_collection(floor=0.5, totalSupply=1267, owners=131, sales24h=1267, vol1d=100, symbol="USDG")
    score_scam = main._nft_scope_score(scam, None, rapid_activity=sharp)
    assert score_scam["blocked"] and not main._nft_scope_worth_posting(score_scam)


# ── Self-adjusting posting slots / scan budgets ──
# Real 429s (recorded by _opensea_get, not a guess) are the only signal
# allowed to force conservative mode - demand alone should never be able
# to loosen anything about WHETHER a candidate qualifies, only HOW MANY
# qualifying candidates get evaluated/posted per cycle.

async def test_opensea_healthy_with_no_recent_rate_limit_marker():
    async def fake_alert_state_get(client, slug, alert_type):
        assert (slug, alert_type) == ("__opensea__", "rate_limited")
        return None

    with patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get):
        async with main.httpx.AsyncClient() as client:
            assert await main._opensea_healthy(client) is True


async def test_opensea_unhealthy_right_after_a_real_429():
    async def fake_alert_state_get(client, slug, alert_type):
        from datetime import datetime, timezone
        return {"last_alerted_at": datetime.now(timezone.utc).isoformat()}

    with patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get):
        async with main.httpx.AsyncClient() as client:
            assert await main._opensea_healthy(client) is False


async def test_opensea_healthy_again_once_the_429_backoff_window_passes():
    from datetime import datetime, timedelta, timezone

    async def fake_alert_state_get(client, slug, alert_type):
        old = datetime.now(timezone.utc) - timedelta(seconds=main._NFT_SCOPE_RATE_LIMIT_BACKOFF_SECONDS + 60)
        return {"last_alerted_at": old.isoformat()}

    with patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get):
        async with main.httpx.AsyncClient() as client:
            assert await main._opensea_healthy(client) is True


async def test_opensea_healthy_fails_open_on_lookup_error():
    async def fake_alert_state_get(client, slug, alert_type):
        raise main.httpx.HTTPError("boom")

    with patch.object(main, "_nft_alert_state_get", new=fake_alert_state_get):
        async with main.httpx.AsyncClient() as client:
            assert await main._opensea_healthy(client) is True


def test_pass_limits_scale_up_when_healthy_and_contract_when_not():
    healthy = main._nft_scope_pass_limits(True)
    base = main._nft_scope_pass_limits(False)

    assert healthy["fresh_max"] == main._NFT_SCOPE_SURGE_MAX_POSTS
    assert healthy["trending_max"] == main._NFT_SCOPE_SURGE_MAX_POSTS
    assert healthy["momentum_max"] == main._NFT_SCOPE_SURGE_MAX_POSTS
    assert healthy["trending_scan_budget"] > base["trending_scan_budget"]
    assert healthy["momentum_scan_limit"] > base["momentum_scan_limit"]

    assert base["fresh_max"] == main._NFT_SCOPE_FRESH_MAX_POSTS
    assert base["trending_max"] == main._NFT_SCOPE_TRENDING_MAX_POSTS
    assert base["momentum_max"] == main._NFT_SCOPE_MOMENTUM_MAX_POSTS
    assert base["trending_scan_budget"] == main._NFT_SCOPE_TRENDING_SCAN_BUDGET
    assert base["momentum_scan_limit"] == main._NFT_SCOPE_MOMENTUM_SCAN_LIMIT

    # Surge only ever raises the ceiling - it must never be lower than base.
    assert healthy["fresh_max"] >= base["fresh_max"]
    assert healthy["trending_max"] >= base["trending_max"]
    assert healthy["momentum_max"] >= base["momentum_max"]


# ── _nft_scope_surge_points (floor-multiple-since-first-seen + velocity) ──
# The direct fix for "collections that ran from a near-zero floor to
# $200+ weren't getting caught" - nothing in the scoring model measured a
# floor multiple at all before this existed.

def test_surge_points_zero_with_no_signals():
    points, reasons, multiple = main._nft_scope_surge_points(strong_collection(floor=0.05), None, None)
    assert points == 0
    assert reasons == []
    assert multiple is None


def test_surge_points_rewards_a_big_floor_multiple_since_first_seen():
    c = strong_collection(floor=0.5)  # 50x a 0.01 first-seen floor
    first_snapshot = {"floor": 0.01, "captured_at": "2026-08-01T00:00:00+00:00"}
    points, reasons, multiple = main._nft_scope_surge_points(c, None, first_snapshot)
    assert points == 25  # top tier: 50x+
    assert multiple == 50.0
    assert any("50.0x" in r and "2026-08-01" in r for r in reasons)


def test_surge_points_tiers_scale_with_multiple_size():
    first_snapshot = {"floor": 1.0, "captured_at": "2026-08-01"}
    p_2x, _, m_2x = main._nft_scope_surge_points(strong_collection(floor=2.0), None, first_snapshot)
    p_10x, _, m_10x = main._nft_scope_surge_points(strong_collection(floor=10.0), None, first_snapshot)
    assert 0 < p_2x < p_10x
    assert m_2x == 2.0 and m_10x == 10.0


def test_surge_points_ignores_a_multiple_below_the_smallest_tier():
    c = strong_collection(floor=0.011)  # only 1.1x - real but not call-worthy on its own
    first_snapshot = {"floor": 0.01, "captured_at": "2026-08-01"}
    points, reasons, multiple = main._nft_scope_surge_points(c, None, first_snapshot)
    assert points == 0
    assert multiple is None


def test_surge_points_rewards_a_sharp_jump_since_the_last_poll():
    c = strong_collection(floor=0.12)  # +20% vs the immediately-prior snapshot
    history = [{"floor": 0.10}, {"floor": 0.09}]  # newest-first
    points, reasons, multiple = main._nft_scope_surge_points(c, history, None)
    assert points == main._NFT_SCOPE_VELOCITY_POINTS
    assert any("jumped" in r and "20%" in r for r in reasons)
    assert multiple is None  # velocity and multiple are independent signals


def test_surge_points_ignores_a_small_move_since_the_last_poll():
    c = strong_collection(floor=0.101)  # +1% - noise, not a signal
    history = [{"floor": 0.10}]
    points, reasons, multiple = main._nft_scope_surge_points(c, history, None)
    assert points == 0
    assert reasons == []


def test_surge_points_stack_multiple_and_velocity_together():
    c = strong_collection(floor=0.6)  # 60x first-seen AND +20% vs last poll
    first_snapshot = {"floor": 0.01, "captured_at": "2026-08-01"}
    history = [{"floor": 0.5}]
    points, reasons, multiple = main._nft_scope_surge_points(c, history, first_snapshot)
    assert points == 25 + main._NFT_SCOPE_VELOCITY_POINTS
    assert len(reasons) == 2
    assert multiple == 60.0


def test_nft_scope_score_surfaces_floor_multiple_and_boosts_score():
    c = strong_collection(floor=0.5)
    first_snapshot = {"floor": 0.01, "captured_at": "2026-08-01"}
    with_surge = main._nft_scope_score(c, None, first_snapshot=first_snapshot)
    without_surge = main._nft_scope_score(c, None, first_snapshot=None)
    assert with_surge["floor_multiple"] == 50.0
    assert without_surge["floor_multiple"] is None
    assert with_surge["score"] > without_surge["score"]
    assert main._nft_scope_worth_posting(with_surge)


def test_nft_scope_score_caps_displayed_score_at_100_even_when_everything_stacks():
    # A genuinely huge mover can clear distribution + trading + substance +
    # momentum + surge + rapid-activity all at once - the embed shows this
    # as "X/100", so the returned score must never exceed that regardless
    # of how many dimensions stack.
    c = strong_collection(floor=0.5, owners=400, totalSupply=500, sales24h=20, vol1d=5)
    first_snapshot = {"floor": 0.005, "captured_at": "2026-08-01"}  # 100x
    history = history_from([0.3, 0.35, 0.4], "floor")
    rapid = {"count": 8, "unique_buyers": 6, "unique_sellers": 5, "window_minutes": 30, "price_surge_pct": 40, "is_sharp": True, "sharp_count": 3, "sharp_window_minutes": 5}
    score = main._nft_scope_score(c, 0.6, history=history, rapid_activity=rapid, first_snapshot=first_snapshot)
    assert score["score"] <= 100


# ── Analyst-take headline leads with the floor multiple when present ────

def test_analyst_take_leads_with_floor_multiple_when_present():
    c = strong_collection(floor=0.5)
    score = main._nft_scope_score(c, None, first_snapshot={"floor": 0.01, "captured_at": "2026-08-01"})
    take = main._nft_scope_analyst_take(c, score, None, "trending")
    assert "50.0x" in take


def test_analyst_take_combines_multiple_and_sharp_activity():
    c = strong_collection(floor=0.5)
    score = main._nft_scope_score(c, None, first_snapshot={"floor": 0.01, "captured_at": "2026-08-01"})
    sharp = {"count": 5, "unique_buyers": 4, "unique_sellers": 3, "window_minutes": 30, "is_sharp": True, "sharp_count": 2, "sharp_window_minutes": 5}
    take = main._nft_scope_analyst_take(c, score, sharp, "trending")
    assert "50.0x" in take
    assert "accelerating" in take.lower() or "still" in take.lower()


def test_embed_gets_a_dedicated_field_for_the_floor_multiple():
    c = strong_collection(floor=0.5)
    with_multiple = main._nft_scope_score(c, None, first_snapshot={"floor": 0.01, "captured_at": "2026-08-01"})
    without_multiple = main._nft_scope_score(c, None, first_snapshot=None)

    embed_with = main._nft_scope_embed(c, with_multiple, None, "trending")
    embed_without = main._nft_scope_embed(c, without_multiple, None, "trending")

    field_names_with = [f["name"] for f in embed_with["fields"]]
    field_names_without = [f["name"] for f in embed_without["fields"]]
    assert "Since First Seen" in field_names_with
    assert "Since First Seen" not in field_names_without
    multiple_field = next(f for f in embed_with["fields"] if f["name"] == "Since First Seen")
    assert "50.0x" in multiple_field["value"]


# ── _nft_scope_accumulation_signal (leading indicator, not confirmation) ──
# Research-grounded: real whale accumulation builds slowly over
# days/weeks BEFORE it moves price - volume/sales climbing while the
# floor is still quiet is a distinct, earlier signal than momentum
# (which needs the floor itself already trending) or surge (which needs
# a move that's already happened).

def test_accumulation_signal_needs_history():
    points, reasons = main._nft_scope_accumulation_signal(strong_collection(), None)
    assert points == 0 and reasons == []


def test_accumulation_signal_needs_the_same_minimum_snapshots_as_momentum():
    short_history = history_from([1, 2, 3], "volume_1d")  # fewer than _NFT_SCOPE_MOMENTUM_MIN_SNAPSHOTS
    points, reasons = main._nft_scope_accumulation_signal(strong_collection(vol1d=4, sales24h=10, floor=0.05), short_history)
    assert points == 0


def test_accumulation_signal_fires_on_rising_volume_and_sales_with_a_quiet_floor():
    n = main._NFT_SCOPE_MOMENTUM_MIN_SNAPSHOTS
    history = [
        {"volume_1d": 1 + i * 0.5, "sales_1d": 2 + i, "floor": 0.05} for i in range(n)
    ]  # oldest-first construction, reversed below to match newest-first storage
    history = list(reversed(history))
    c = strong_collection(vol1d=1 + n * 0.5, sales24h=2 + n, floor=0.051)  # floor barely moved
    points, reasons = main._nft_scope_accumulation_signal(c, history)
    assert points == main._NFT_SCOPE_ACCUMULATION_POINTS
    assert any("accumulation" in r.lower() for r in reasons)


def test_accumulation_signal_does_not_fire_once_the_floor_has_already_broken_out():
    # Same rising volume/sales, but the floor has ALREADY moved a lot -
    # this is a confirmed move now (Surge's job), not a leading signal.
    n = main._NFT_SCOPE_MOMENTUM_MIN_SNAPSHOTS
    history = list(reversed([{"volume_1d": 1 + i * 0.5, "sales_1d": 2 + i, "floor": 0.05 + i * 0.02} for i in range(n)]))
    c = strong_collection(vol1d=1 + n * 0.5, sales24h=2 + n, floor=0.3)  # floor already up ~500%
    points, _ = main._nft_scope_accumulation_signal(c, history)
    assert points == 0


def test_accumulation_signal_requires_both_volume_and_sales_trending_up():
    n = main._NFT_SCOPE_MOMENTUM_MIN_SNAPSHOTS
    # Volume rising, sales flat - not aligned, shouldn't fire.
    history = list(reversed([{"volume_1d": 1 + i * 0.5, "sales_1d": 5, "floor": 0.05} for i in range(n)]))
    c = strong_collection(vol1d=1 + n * 0.5, sales24h=5, floor=0.05)
    points, _ = main._nft_scope_accumulation_signal(c, history)
    assert points == 0


def test_nft_scope_score_includes_accumulation_points():
    n = main._NFT_SCOPE_MOMENTUM_MIN_SNAPSHOTS
    history = list(reversed([{"volume_1d": 1 + i * 0.5, "sales_1d": 2 + i, "floor": 0.05, "owners": 300} for i in range(n)]))
    c = strong_collection(vol1d=1 + n * 0.5, sales24h=2 + n, floor=0.051)
    with_history = main._nft_scope_score(c, None, history=history)
    without_history = main._nft_scope_score(c, None, history=None)
    assert with_history["score"] > without_history["score"]
    assert any("accumulation" in r.lower() for r in with_history["reasons"])
