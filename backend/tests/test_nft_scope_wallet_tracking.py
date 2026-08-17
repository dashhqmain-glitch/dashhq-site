"""Tests for NFT Scope's in-house wallet tracker: the always-on sale-event
log (built from data already being fetched, no new API cost), the two
win-rate sources it feeds (nft_scope_call_buyers/nft_scope_proved_slugs for
NFT-Scope's-own-calls, nft_wallet_pnl_stats for realized buy-then-sell
profit on anything observed), the merged smart-wallet scoring signal, the
multi-wallet convergence bonus, and the holdings-scan discovery pass."""
from unittest.mock import patch

import main


class FakeRes:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.httpx.HTTPStatusError("boom", request=None, response=self)


def sale_event(buyer, seller, token_id="1", price_eth=1.0, when=1_700_000_000):
    return {
        "buyer": buyer, "seller": seller, "nft": {"identifier": token_id},
        "event_timestamp": when,
        "payment": {"quantity": str(int(price_eth * 10**18)), "decimals": 18, "symbol": "ETH"},
    }


# ── _nft_log_sale_events - the passive, always-on half ──────────────────

async def test_log_sale_events_persists_well_formed_rows():
    posted = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            posted.append((url, json))
            return FakeRes(200)

    events = [sale_event("buyer1", "seller1", token_id="42", price_eth=0.5, when=1_700_000_000)]
    await main._nft_log_sale_events(FakeClient(), "azuki", events)

    assert len(posted) == 1
    url, rows = posted[0]
    assert url.endswith("/nft_sale_events_log")
    assert rows == [{
        "slug": "azuki", "token_id": "42", "buyer": "buyer1", "seller": "seller1",
        "price": 0.5, "symbol": "ETH", "event_at": "2023-11-14T22:13:20+00:00",
    }]


async def test_log_sale_events_skips_events_missing_required_fields():
    posted = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            posted.append(json)
            return FakeRes(200)

    events = [
        {"buyer": "b1", "nft": {"identifier": "1"}, "event_timestamp": 100},  # no seller
        {"seller": "s1", "nft": {"identifier": "1"}, "event_timestamp": 100},  # no buyer
        {"buyer": "b1", "seller": "s1", "event_timestamp": 100},  # no nft/token_id
    ]
    await main._nft_log_sale_events(FakeClient(), "azuki", events)
    assert posted == []


async def test_log_sale_events_excludes_self_trades():
    # A self-sale (same wallet as buyer and seller) is never a real trade -
    # excluded at the source so it can never contaminate a wallet's
    # realized-PnL win rate downstream, not just wherever a check happens
    # to remember to filter for it.
    posted = []

    class FakeClient:
        async def post(self, url, headers=None, json=None):
            posted.append(json)
            return FakeRes(200)

    events = [
        sale_event("same-wallet", "same-wallet", token_id="1"),  # self-trade - must be dropped
        sale_event("real-buyer", "real-seller", token_id="2"),   # genuine trade - must survive
    ]
    await main._nft_log_sale_events(FakeClient(), "azuki", events)

    assert len(posted) == 1
    assert len(posted[0]) == 1
    assert posted[0][0]["token_id"] == "2"


async def test_log_sale_events_is_a_noop_on_no_valid_rows():
    class FakeClient:
        async def post(self, url, headers=None, json=None):
            raise AssertionError("should never POST when there's nothing to log")

    await main._nft_log_sale_events(FakeClient(), "azuki", [])


async def test_log_sale_events_never_raises_on_a_failed_write():
    class FakeClient:
        async def post(self, url, headers=None, json=None):
            raise main.httpx.HTTPError("boom")

    events = [sale_event("b1", "s1")]
    await main._nft_log_sale_events(FakeClient(), "azuki", events)  # must not raise


# ── _nft_scope_smart_wallet_hits - merges both sources, dedups ──────────

async def test_smart_wallet_hits_returns_empty_with_no_buyer_addresses():
    result = await main._nft_scope_smart_wallet_hits(main.httpx.AsyncClient(), {"buyer_addresses": []})
    assert result == []
    result = await main._nft_scope_smart_wallet_hits(main.httpx.AsyncClient(), None)
    assert result == []


async def test_smart_wallet_hits_merges_both_sources():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if url.endswith("/nft_smart_wallets"):
                return FakeRes(200, [{"address": "0xaaa", "total_calls": 4, "proved_calls": 2, "win_rate": 0.5, "best_multiple": 8.0}])
            if url.endswith("/nft_wallet_pnl_stats"):
                return FakeRes(200, [{"address": "0xbbb", "total_trades": 5, "winning_trades": 3, "win_rate": 0.6, "best_trade_pct": 2.5}])
            raise AssertionError(f"unexpected {url}")

    rapid = {"buyer_addresses": ["0xaaa", "0xbbb"]}
    hits = await main._nft_scope_smart_wallet_hits(FakeClient(), rapid)
    addresses = {h["address"] for h in hits}
    assert addresses == {"0xaaa", "0xbbb"}
    aaa = next(h for h in hits if h["address"] == "0xaaa")
    assert aaa == {"address": "0xaaa", "sample": 4, "wins": 2, "win_rate": 0.5, "best_pct": 7.0}  # best_multiple 8x -> +700%
    bbb = next(h for h in hits if h["address"] == "0xbbb")
    assert bbb == {"address": "0xbbb", "sample": 5, "wins": 3, "win_rate": 0.6, "best_pct": 2.5}


async def test_smart_wallet_hits_dedups_keeping_the_stronger_win_rate():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if url.endswith("/nft_smart_wallets"):
                return FakeRes(200, [{"address": "0xaaa", "total_calls": 4, "proved_calls": 1, "win_rate": 0.25, "best_multiple": 6.0}])
            if url.endswith("/nft_wallet_pnl_stats"):
                return FakeRes(200, [{"address": "0xaaa", "total_trades": 10, "winning_trades": 7, "win_rate": 0.7, "best_trade_pct": 3.0}])
            raise AssertionError(f"unexpected {url}")

    hits = await main._nft_scope_smart_wallet_hits(FakeClient(), {"buyer_addresses": ["0xaaa"]})
    assert len(hits) == 1
    assert hits[0]["win_rate"] == 0.7  # the stronger of the two sources


async def test_smart_wallet_hits_fails_safe_on_query_error():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            raise main.httpx.HTTPError("boom")

    hits = await main._nft_scope_smart_wallet_hits(FakeClient(), {"buyer_addresses": ["0xaaa"]})
    assert hits == []


# ── _nft_scope_smart_wallet_points - scoring + convergence ──────────────

def _hit(address, win_rate=0.5, sample=4, wins=2, best_pct=5.0):
    return {"address": address, "sample": sample, "wins": wins, "win_rate": win_rate, "best_pct": best_pct}


def test_smart_wallet_points_zero_with_no_hits():
    points, reasons = main._nft_scope_smart_wallet_points([])
    assert points == 0 and reasons == []
    points, reasons = main._nft_scope_smart_wallet_points(None)
    assert points == 0 and reasons == []


def test_smart_wallet_points_single_hit_never_names_a_wallet_or_address():
    points, reasons = main._nft_scope_smart_wallet_points([_hit("0xdeadbeef")])
    assert points == main._NFT_SCOPE_SMART_WALLET_BONUS_POINTS
    assert len(reasons) == 1
    assert "0xdeadbeef" not in reasons[0]
    assert "0x" not in reasons[0]  # no raw address ever surfaced, internal signal only


def test_smart_wallet_points_convergence_bonus_scales_with_distinct_wallets():
    single = main._nft_scope_smart_wallet_points([_hit("0xa")])
    double = main._nft_scope_smart_wallet_points([_hit("0xa"), _hit("0xb")])
    triple = main._nft_scope_smart_wallet_points([_hit("0xa"), _hit("0xb"), _hit("0xc")])
    assert single[0] < double[0] < triple[0]
    assert any("converging" in r.lower() for r in double[1])
    assert len(single[1]) == 1  # no convergence line for a single hit


def test_smart_wallet_points_convergence_bonus_is_capped():
    many = [_hit(f"0x{i}") for i in range(20)]
    points, _ = main._nft_scope_smart_wallet_points(many)
    convergence_component = points - main._NFT_SCOPE_SMART_WALLET_BONUS_POINTS
    assert convergence_component == main._NFT_SCOPE_CONVERGENCE_POINTS_CAP


# ── _nft_scope_score integration ─────────────────────────────────────────

def test_score_includes_smart_wallet_points_and_never_names_addresses():
    from test_nft_scope import strong_collection
    c = strong_collection()
    with_hits = main._nft_scope_score(c, None, smart_wallet_hits=[_hit("0xa"), _hit("0xb")])
    without_hits = main._nft_scope_score(c, None, smart_wallet_hits=None)
    assert with_hits["score"] > without_hits["score"]
    assert any("converging" in r.lower() for r in with_hits["reasons"])
    assert not any("0x" in r for r in with_hits["reasons"])


# ── Analyst-take headline surfaces convergence ───────────────────────────

def test_analyst_take_leads_with_convergence_when_present():
    from test_nft_scope import strong_collection
    c = strong_collection()
    score = main._nft_scope_score(c, None, smart_wallet_hits=[_hit("0xa"), _hit("0xb"), _hit("0xc")])
    take = main._nft_scope_analyst_take(c, score, None, "trending")
    assert "3 separate wallets" in take
    assert "0x" not in take


# ── _nft_scope_top_wallets / _nft_scope_wallet_holdings ─────────────────

async def test_top_wallets_merges_and_dedups_across_both_tables():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if url.endswith("/nft_wallet_pnl_stats"):
                return FakeRes(200, [{"address": "0xa"}, {"address": "0xb"}])
            if url.endswith("/nft_smart_wallets"):
                return FakeRes(200, [{"address": "0xb"}, {"address": "0xc"}])
            if url.endswith("/nft_wallet_recent_activity"):
                return FakeRes(200, [])
            raise AssertionError(f"unexpected {url}")

    wallets = await main._nft_scope_top_wallets(FakeClient(), 10)
    assert set(wallets) == {"0xa", "0xb", "0xc"}


async def test_top_wallets_fails_safe_per_table():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if url.endswith("/nft_wallet_pnl_stats"):
                raise main.httpx.HTTPError("boom")
            if url.endswith("/nft_wallet_recent_activity"):
                return FakeRes(200, [])
            return FakeRes(200, [{"address": "0xc"}])

    wallets = await main._nft_scope_top_wallets(FakeClient(), 10)
    assert wallets == ["0xc"]


async def test_top_wallets_includes_currently_spiking_wallets_alongside_proven_ones():
    # A wallet with no win-rate history yet but a real, diversity-verified
    # activity spike must still be sampled - proven win rate alone would
    # structurally never surface a wallet that's hot right now but hasn't
    # built a track record.
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if url.endswith("/nft_wallet_pnl_stats") or url.endswith("/nft_smart_wallets"):
                return FakeRes(200, [{"address": "0xproven"}])
            if url.endswith("/nft_wallet_recent_activity"):
                return FakeRes(200, [{"address": "0xhot", "recent_buys": 9, "recent_unique_sellers": 9, "baseline_buys": 0, "recent_volume": 3, "baseline_volume": 0}])
            raise AssertionError(f"unexpected {url}")

    wallets = await main._nft_scope_top_wallets(FakeClient(), 10)
    assert set(wallets) == {"0xproven", "0xhot"}


async def test_top_wallets_caps_the_merged_result_at_limit():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if url.endswith("/nft_wallet_pnl_stats"):
                return FakeRes(200, [{"address": f"0x{i}"} for i in range(5)])
            if url.endswith("/nft_smart_wallets"):
                return FakeRes(200, [])
            if url.endswith("/nft_wallet_recent_activity"):
                return FakeRes(200, [])
            raise AssertionError(f"unexpected {url}")

    wallets = await main._nft_scope_top_wallets(FakeClient(), 3)
    assert len(wallets) == 3


async def test_wallet_holdings_returns_sorted_distinct_collections():
    async def fake_get_key(client):
        return "fake-key"

    class FakeClient:
        async def get(self, url, params=None, headers=None):
            assert url == "https://api.opensea.io/api/v2/chain/ethereum/account/0xabc/nfts"
            return FakeRes(200, {"nfts": [
                {"collection": "azuki"}, {"collection": "milady"}, {"collection": "azuki"}, {}
            ]})

    with patch.object(main, "_get_opensea_key", new=fake_get_key):
        held = await main._nft_scope_wallet_holdings(FakeClient(), "0xabc")
    assert held == ["azuki", "milady"]


async def test_wallet_holdings_empty_without_a_key():
    async def fake_get_key(client):
        return None

    class FakeClient:
        async def get(self, url, params=None, headers=None):
            raise AssertionError("should not call OpenSea without a key")

    with patch.object(main, "_get_opensea_key", new=fake_get_key):
        held = await main._nft_scope_wallet_holdings(FakeClient(), "0xabc")
    assert held == []


# ── nft_sale_events_log pruning - unbounded growth would otherwise be the ──
# one thing in this whole tracker that grows forever, same failure mode
# nft_snapshot_history already had before it got its own pruning job.

async def test_prune_old_sale_events_deletes_with_the_right_cutoff_column():
    calls = []

    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            calls.append((url, params))
            return FakeRes(200)

    result = await main._prune_old_sale_events(FakeClient())
    assert result is True
    assert len(calls) == 1
    url, params = calls[0]
    assert url.endswith("/nft_sale_events_log")
    assert "event_at" in params
    assert params["event_at"].startswith("lt.")


async def test_prune_old_sale_events_fails_safe_on_error():
    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            raise main.httpx.HTTPError("boom")

    result = await main._prune_old_sale_events(FakeClient())
    assert result is False


# ── nft_scope_call_buyers pruning - same unbounded-growth shape, slower ────
# growth rate (only writes on an actual post, not every wash-check fetch)
# but still nothing else was cleaning it up.

async def test_prune_old_call_buyers_deletes_with_the_right_cutoff_column():
    calls = []

    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            calls.append((url, params))
            return FakeRes(200)

    result = await main._prune_old_call_buyers(FakeClient())
    assert result is True
    assert len(calls) == 1
    url, params = calls[0]
    assert url.endswith("/nft_scope_call_buyers")
    assert "called_at" in params
    assert params["called_at"].startswith("lt.")


async def test_prune_old_call_buyers_fails_safe_on_error():
    class FakeClient:
        async def delete(self, url, headers=None, params=None):
            raise main.httpx.HTTPError("boom")

    result = await main._prune_old_call_buyers(FakeClient())
    assert result is False


# ── _nft_scope_wallet_activity_spike_hits - "suddenly buying a lot" ─────
# Distinct from win rate: fires on a wallet's own buying PACE changing,
# even with zero resolved trades yet.

async def test_activity_spike_hits_empty_with_no_buyer_addresses():
    result = await main._nft_scope_wallet_activity_spike_hits(main.httpx.AsyncClient(), {"buyer_addresses": []})
    assert result == []
    result = await main._nft_scope_wallet_activity_spike_hits(main.httpx.AsyncClient(), None)
    assert result == []


async def test_activity_spike_hits_flags_a_real_spike_over_baseline():
    # recent: 9 buys / 3 days = 3.0/day. baseline: 27 buys / 27 days = 1.0/day. ratio = 3.0 - right at the bar.
    # 9 unique sellers for 9 buys = fully diverse, clears the counterparty check easily.
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            assert url.endswith("/nft_wallet_recent_activity")
            return FakeRes(200, [{"address": "0xa", "recent_buys": 9, "recent_unique_sellers": 9, "baseline_buys": 27, "recent_volume": 5.0, "baseline_volume": 10.0}])

    hits = await main._nft_scope_wallet_activity_spike_hits(FakeClient(), {"buyer_addresses": ["0xa"]})
    assert len(hits) == 1
    assert hits[0]["ratio"] == 3.0


async def test_activity_spike_hits_excludes_a_ratio_below_the_bar():
    # recent: 3 buys / 3 days = 1.0/day. baseline: 27 buys / 27 days = 1.0/day. ratio = 1.0 - no spike, steady pace.
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"address": "0xa", "recent_buys": 3, "recent_unique_sellers": 3, "baseline_buys": 27, "recent_volume": 1.0, "baseline_volume": 9.0}])

    hits = await main._nft_scope_wallet_activity_spike_hits(FakeClient(), {"buyer_addresses": ["0xa"]})
    assert hits == []


async def test_activity_spike_hits_treats_zero_baseline_as_a_spike_without_a_ratio():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"address": "0xa", "recent_buys": 5, "recent_unique_sellers": 5, "baseline_buys": 0, "recent_volume": 2.0, "baseline_volume": 0}])

    hits = await main._nft_scope_wallet_activity_spike_hits(FakeClient(), {"buyer_addresses": ["0xa"]})
    assert len(hits) == 1
    assert hits[0]["ratio"] is None
    assert hits[0]["baseline_buys"] == 0


async def test_activity_spike_hits_fails_safe_on_query_error():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            raise main.httpx.HTTPError("boom")

    hits = await main._nft_scope_wallet_activity_spike_hits(FakeClient(), {"buyer_addresses": ["0xa"]})
    assert hits == []


# ── Counterparty diversity - the wash-trading guard the spike detector ──
# needs, since raw buy count alone can't distinguish genuine broad buying
# from a ring cycling trades among a few colluding wallets.

async def test_activity_spike_hits_excludes_low_counterparty_diversity():
    # 9 recent buys but only 2 distinct sellers - classic wash-ring shape
    # (a handful of wallets cycling trades), not genuine broad buying.
    # Would otherwise clear the ratio bar easily (9/3=3.0 vs 0 baseline).
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"address": "0xa", "recent_buys": 9, "recent_unique_sellers": 2, "baseline_buys": 0, "recent_volume": 5.0, "baseline_volume": 0}])

    hits = await main._nft_scope_wallet_activity_spike_hits(FakeClient(), {"buyer_addresses": ["0xa"]})
    assert hits == []


async def test_activity_spike_hits_allows_borderline_diversity_at_the_bar():
    # 6 buys, 3 distinct sellers = exactly 0.5 ratio, right at the minimum.
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes(200, [{"address": "0xa", "recent_buys": 6, "recent_unique_sellers": 3, "baseline_buys": 0, "recent_volume": 3.0, "baseline_volume": 0}])

    hits = await main._nft_scope_wallet_activity_spike_hits(FakeClient(), {"buyer_addresses": ["0xa"]})
    assert len(hits) == 1


def test_activity_spike_points_zero_with_no_hits():
    points, reasons = main._nft_scope_activity_spike_points([])
    assert points == 0 and reasons == []
    points, reasons = main._nft_scope_activity_spike_points(None)
    assert points == 0 and reasons == []


def test_activity_spike_points_awards_bonus_and_never_names_a_wallet():
    hits = [{"address": "0xdeadbeef", "recent_buys": 6, "unique_sellers": 6, "baseline_buys": 20, "ratio": 4.5}]
    points, reasons = main._nft_scope_activity_spike_points(hits)
    assert points == main._NFT_SCOPE_ACTIVITY_SPIKE_BONUS_POINTS
    assert len(reasons) == 1
    assert "0x" not in reasons[0]
    assert "6 purchases" in reasons[0]


def test_activity_spike_points_calls_out_zero_baseline_distinctly():
    hits = [{"address": "0xa", "recent_buys": 4, "unique_sellers": 4, "baseline_buys": 0, "ratio": None}]
    points, reasons = main._nft_scope_activity_spike_points(hits)
    assert points == main._NFT_SCOPE_ACTIVITY_SPIKE_BONUS_POINTS
    assert "no prior buying history" in reasons[0]


# ── _nft_scope_wallet_signals - combined, parallel fetch ─────────────────

async def test_wallet_signals_empty_with_no_rapid_activity():
    result = await main._nft_scope_wallet_signals(main.httpx.AsyncClient(), None)
    assert result == ([], [])
    result = await main._nft_scope_wallet_signals(main.httpx.AsyncClient(), {"buyer_addresses": []})
    assert result == ([], [])


async def test_wallet_signals_fetches_both_and_returns_them_combined():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if url.endswith("/nft_smart_wallets"):
                return FakeRes(200, [{"address": "0xa", "total_calls": 4, "proved_calls": 2, "win_rate": 0.5, "best_multiple": 6.0}])
            if url.endswith("/nft_wallet_pnl_stats"):
                return FakeRes(200, [])
            if url.endswith("/nft_wallet_recent_activity"):
                return FakeRes(200, [{"address": "0xb", "recent_buys": 6, "recent_unique_sellers": 6, "baseline_buys": 0, "recent_volume": 1, "baseline_volume": 0}])
            raise AssertionError(f"unexpected {url}")

    smart_hits, spike_hits = await main._nft_scope_wallet_signals(FakeClient(), {"buyer_addresses": ["0xa", "0xb"]})
    assert len(smart_hits) == 1 and smart_hits[0]["address"] == "0xa"
    assert len(spike_hits) == 1 and spike_hits[0]["address"] == "0xb"


# ── _nft_scope_score integration ──────────────────────────────────────────

def test_score_includes_activity_spike_points():
    from test_nft_scope import strong_collection
    c = strong_collection()
    spike = [{"address": "0xa", "recent_buys": 6, "unique_sellers": 6, "baseline_buys": 20, "ratio": 5.0}]
    with_spike = main._nft_scope_score(c, None, activity_spike_hits=spike)
    without_spike = main._nft_scope_score(c, None, activity_spike_hits=None)
    assert with_spike["score"] > without_spike["score"]
    assert any("accelerating" in r.lower() for r in with_spike["reasons"])
    assert not any("0x" in r for r in with_spike["reasons"])
