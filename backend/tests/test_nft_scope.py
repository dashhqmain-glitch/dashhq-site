"""Regression suite for NFT Scope's scoring/detection pipeline.

Every check here traces back to a real bug or a real scam call caught
during development - kept as permanent tests so a future change can't
silently reopen one of these. If you're touching _nft_scope_score,
_detect_fake_offer, _analyze_wash_trading, _detect_abnormal_turnover,
_momentum_points, or _detect_rapid_activity, run this file first.
"""
import time
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
    reason = main._detect_abnormal_turnover(c)
    assert reason is not None
    assert "changed hands" in reason or "turned over" in reason.lower()


def test_turnover_blocks_real_case_trwnat():
    # Live false-positive: 10 sales against 10 total supply in 24h.
    c = {"totalSupply": 10, "owners": 10, "sales24h": 10}
    assert main._detect_abnormal_turnover(c) is not None


def test_turnover_catches_high_flips_per_owner_even_with_large_supply():
    # A big supply dilutes the sales/supply ratio, but sales/owners still
    # reveals churn among the actual current holder set.
    c = {"totalSupply": 100_000, "owners": 20, "sales24h": 150}
    assert main._detect_abnormal_turnover(c) is not None


def test_turnover_normal_activity_not_flagged():
    c = {"totalSupply": 5000, "owners": 3000, "sales24h": 25}
    assert main._detect_abnormal_turnover(c) is None


def test_turnover_handles_missing_and_zero_data():
    assert main._detect_abnormal_turnover({"sales24h": 0}) is None
    assert main._detect_abnormal_turnover({"sales24h": 50}) is None  # no supply/owners data at all


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
