"""Tests for /cron/wallet-stats - a read-only rollup for health checks,
reusing existing queries (smart_wallet_tags, smart_wallet_submissions,
alert_tracker_stats) rather than adding any new table or logic.
"""
from unittest import mock

import main
from config import settings


class FakeRes:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else []
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.httpx.HTTPStatusError("boom", request=None, response=self)


class FakeRequest:
    def __init__(self, auth=None):
        self.headers = {"authorization": auth} if auth is not None else {}


async def test_wallet_stats_requires_cron_secret():
    settings.cron_secret = ""
    try:
        try:
            await main.wallet_stats(FakeRequest("Bearer anything"))
            assert False, "should have raised"
        except main.HTTPException as e:
            assert e.status_code == 401
    finally:
        settings.cron_secret = "test-cron-secret"


async def test_wallet_stats_reports_distinct_wallets_tags_submissions_and_alert_stats():
    settings.cron_secret = "s"

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            if "smart_wallet_tags" in url:
                return FakeRes(200, [
                    {"address": "0xa", "tag": "Degen"},
                    {"address": "0xb", "tag": "Degen"},
                    {"address": "0xa", "tag": "Whale"},  # same wallet, second tag - counted once in distinct total
                ])
            if "smart_wallet_submissions" in url:
                status = params["status"]
                counts = {"eq.pending": 3, "eq.approved": 40, "eq.rejected": 5}
                return FakeRes(200, [{"address": "0xa"}], headers={"content-range": f"0-0/{counts[status]}"})
            if "alert_tracker_stats" in url:
                return FakeRes(200, [{"checked_calls": 34, "proved_calls": 21, "hit_rate": 0.61, "median_multiple": 1.8, "best_multiple": 5.2}])
            return FakeRes(200, [])

    try:
        with mock.patch.object(main.httpx, "AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value = FakeClient()
            result = await main.wallet_stats(FakeRequest("Bearer s"))
    finally:
        settings.cron_secret = "test-cron-secret"

    assert result["tracked_wallets_total"] == 2  # 0xa and 0xb, not 3 rows
    assert result["tags"] == {"Degen": 2, "Whale": 1}
    assert result["submissions"] == {"pending": 3, "approved": 40, "rejected": 5}
    assert result["alert_tracker_stats"]["hit_rate"] == 0.61
