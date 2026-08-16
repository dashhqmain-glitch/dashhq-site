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
            raise AssertionError(f"unexpected {url}")

    wallets = await main._nft_scope_top_wallets(FakeClient(), 10)
    assert set(wallets) == {"0xa", "0xb", "0xc"}


async def test_top_wallets_fails_safe_per_table():
    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if url.endswith("/nft_wallet_pnl_stats"):
                raise main.httpx.HTTPError("boom")
            return FakeRes(200, [{"address": "0xc"}])

    wallets = await main._nft_scope_top_wallets(FakeClient(), 10)
    assert wallets == ["0xc"]


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
