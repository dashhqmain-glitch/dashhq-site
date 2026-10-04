"""Tests for the table cleanup windows.

Measured against the real database: nft_sale_events_log settles at ~4,700
rows/day and ~495 bytes/row (table plus its three indexes), so the old
180-day window sat at ~415 MB by itself - most of the Supabase Free Plan's
500 MB cap, which is what pushed the project past it (0.54 / 0.5 GB). It is
now 90 days. nft_scope_call_buyers is tiny and is the raw evidence behind
each wallet's track record, so it keeps its original 180.
"""
import logging

import main


class FakeRes:
    def __init__(self, status_code=204, text=""):
        self.status_code = status_code
        self.text = text


class FakeGetRes:
    def __init__(self, rows):
        self.rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return self.rows


def _iso_days_ago(n):
    return (main.datetime.now(main.timezone.utc) - main.timedelta(days=n)).isoformat()


def _days_ago(cutoff_param):
    assert cutoff_param.startswith("lt.")
    cutoff = main.datetime.fromisoformat(cutoff_param[len("lt."):])
    return (main.datetime.now(main.timezone.utc) - cutoff).total_seconds() / 86400


class RecordingClient:
    # get_res defaults to "there's a real old row, 100 days old" so every
    # existing test exercising _prune_old_sale_events's DELETE-response
    # handling (rejected/successful/no-body) proceeds straight past the
    # new oldest-row lookup without having to know it exists, unless a
    # test specifically wants to exercise that lookup itself.
    def __init__(self, res=None, get_res=None):
        self.res = res or FakeRes()
        self.get_res = get_res if get_res is not None else FakeGetRes([{"event_at": _iso_days_ago(100)}])
        self.calls = []
        self.get_calls = []

    async def delete(self, url, headers=None, params=None):
        self.calls.append((url, params))
        return self.res

    async def get(self, url, headers=None, params=None):
        self.get_calls.append((url, params))
        return self.get_res


def test_retention_windows_are_what_the_size_budget_assumes():
    assert main._SALE_EVENTS_LOG_RETENTION_DAYS == 90
    assert main._CALL_BUYERS_RETENTION_DAYS == 180
    assert main._SNAPSHOT_RETENTION_DAYS == 30


async def test_sale_events_prune_deletes_a_bounded_window_from_the_oldest_row():
    # Confirmed live: a single unbounded "everything older than 90 days"
    # DELETE is exactly what let this table grow to 900k+ rows - Supabase's
    # own Postgres canceled that statement outright against a real
    # backlog. Each call now only ever deletes one _SALE_EVENTS_PRUNE_
    # BATCH_DAYS-wide window starting from the real oldest row, not the
    # whole backlog at once.
    oldest_iso = _iso_days_ago(100)
    client = RecordingClient(get_res=FakeGetRes([{"event_at": oldest_iso}]))
    assert await main._prune_old_sale_events(client) is True
    url, params = client.calls[0]
    assert url.endswith("/nft_sale_events_log")
    gte_param, lt_param = params["event_at"]
    assert gte_param == f"gte.{oldest_iso}"
    # Window is bounded to _SALE_EVENTS_PRUNE_BATCH_DAYS (3), not the full
    # distance to the 90-day cutoff (10 days away here) - a 97-day-old
    # upper bound, not a 90-day-old one.
    lt_days_ago = (main.datetime.now(main.timezone.utc) - main.datetime.fromisoformat(lt_param[len("lt."):])).total_seconds() / 86400
    assert abs(lt_days_ago - (100 - main._SALE_EVENTS_PRUNE_BATCH_DAYS)) < 0.01


async def test_sale_events_prune_window_clamps_to_the_retention_cutoff():
    # The oldest row is only 1 day past the 90-day cutoff - less than a
    # full batch window remains. The delete's upper bound must clamp to
    # the cutoff itself (90 days ago), not overshoot into data that's
    # still within the retention window.
    client = RecordingClient(get_res=FakeGetRes([{"event_at": _iso_days_ago(91)}]))
    assert await main._prune_old_sale_events(client) is True
    _, params = client.calls[0]
    _, lt_param = params["event_at"]
    lt_days_ago = (main.datetime.now(main.timezone.utc) - main.datetime.fromisoformat(lt_param[len("lt."):])).total_seconds() / 86400
    assert abs(lt_days_ago - 90) < 0.01


async def test_sale_events_prune_is_a_noop_once_caught_up():
    # The oldest row is well within the retention window - nothing to
    # delete, and no DELETE should even be attempted.
    client = RecordingClient(get_res=FakeGetRes([{"event_at": _iso_days_ago(10)}]))
    assert await main._prune_old_sale_events(client) is True
    assert client.calls == []


async def test_sale_events_prune_is_a_noop_on_an_empty_table():
    client = RecordingClient(get_res=FakeGetRes([]))
    assert await main._prune_old_sale_events(client) is True
    assert client.calls == []


async def test_call_buyers_keep_their_own_longer_window():
    # Must NOT follow the sale log down to 90 - it was only ever sharing that
    # constant by coincidence, and it's the evidence behind wallet track records.
    client = RecordingClient()
    assert await main._prune_old_call_buyers(client) is True
    url, params = client.calls[0]
    assert url.endswith("/nft_scope_call_buyers")
    assert abs(_days_ago(params["called_at"]) - 180) < 0.01


async def test_snapshots_are_still_pruned_at_thirty_days():
    client = RecordingClient()
    assert await main._prune_old_snapshots(client) is True
    url, params = client.calls[0]
    assert url.endswith("/nft_snapshot_history")
    assert abs(_days_ago(params["captured_at"]) - 30) < 0.01


async def test_a_rejected_prune_is_logged_with_the_reason_not_just_returned_as_false(caplog):
    # Previously a rejected delete came back as a bare False that nothing
    # looked at, so a table that stopped being pruned would only ever have
    # been noticed by its size.
    body = '{"code":"57014","message":"canceling statement due to statement timeout"}'
    for fn, table in (
        (main._prune_old_sale_events, "nft_sale_events_log"),
        (main._prune_old_call_buyers, "nft_scope_call_buyers"),
        (main._prune_old_snapshots, "nft_snapshot_history"),
    ):
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            result = await fn(RecordingClient(FakeRes(500, body)))
        assert result is False
        assert table in caplog.text and "HTTP 500" in caplog.text and "57014" in caplog.text


async def test_a_successful_prune_does_not_log_an_error(caplog):
    with caplog.at_level(logging.ERROR):
        assert await main._prune_old_sale_events(RecordingClient(FakeRes(204))) is True
    assert caplog.text == ""


async def test_a_prune_response_without_a_body_still_reports_failure():
    class NoText:
        status_code = 503

    assert await main._prune_old_sale_events(RecordingClient(NoText())) is False
