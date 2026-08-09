"""Infrastructure-level safety nets: the global Discord interaction crash
handler, and purge-channel's rate-limit/permission handling."""
from unittest.mock import patch

import main
from config import settings


async def test_dispatch_crash_anywhere_returns_clean_ephemeral_error():
    async def broken_toolkit_command(payload):
        raise RuntimeError("simulated unexpected bug deep in some command")

    with patch.object(main, "_verify_discord_signature", return_value=True), \
         patch.object(main, "_handle_toolkit_command", new=broken_toolkit_command):

        class FakeRequest:
            headers = {"x-signature-ed25519": "sig", "x-signature-timestamp": "ts"}

            async def body(self):
                return b'{"type": 2, "data": {"name": "gas"}}'

            async def json(self):
                return {"type": 2, "data": {"name": "gas"}, "member": {"user": {"id": "1"}}}

        result = await main.discord_interactions(FakeRequest())

    assert result["type"] == 4
    assert "Something went wrong" in result["data"]["content"]
    assert result["data"]["flags"] == 64


async def test_dispatch_working_command_passes_through_unaffected():
    async def working_toolkit_command(payload):
        return {"type": 4, "data": {"content": "all good", "flags": 64}}

    with patch.object(main, "_verify_discord_signature", return_value=True), \
         patch.object(main, "_handle_toolkit_command", new=working_toolkit_command):

        class FakeRequest:
            headers = {"x-signature-ed25519": "sig", "x-signature-timestamp": "ts"}

            async def body(self):
                return b'{"type": 2, "data": {"name": "gas"}}'

            async def json(self):
                return {"type": 2, "data": {"name": "gas"}, "member": {"user": {"id": "1"}}}

        result = await main.discord_interactions(FakeRequest())

    assert result["data"]["content"] == "all good"


async def test_purge_channel_falls_back_to_single_delete_on_403_and_retries_429():
    settings.cron_secret = "testsecret"
    settings.discord_bot_token = "x"

    class FakeRes:
        def __init__(self, status_code, json_data=None, text=""):
            self.status_code = status_code
            self._json = json_data or {}
            self.text = text

        def json(self):
            return self._json

    delete_attempts = {"msg2": 0}

    class FakeClient:
        call_count = 0

        async def get(self, url, headers=None, params=None):
            FakeClient.call_count += 1
            if FakeClient.call_count == 1:
                return FakeRes(200, json_data=[{"id": "msg1"}, {"id": "msg2"}, {"id": "msg3"}])
            return FakeRes(200, json_data=[])

        async def post(self, url, headers=None, json=None):
            return FakeRes(403, text="Missing Permissions")

        async def delete(self, url, headers=None):
            mid = url.rsplit("/", 1)[-1]
            if mid == "msg1":
                return FakeRes(204)
            if mid == "msg2":
                delete_attempts["msg2"] += 1
                if delete_attempts["msg2"] == 1:
                    return FakeRes(429, json_data={"retry_after": 0.01})
                return FakeRes(204)
            return FakeRes(500, text="Internal error")

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()

        class FakeReq:
            headers = {"authorization": "Bearer testsecret"}

        result = await main.purge_channel(FakeReq(), channel_id="123", max_batches=8)

    assert result["deleted_bulk"] == 0
    assert result["deleted_single"] == 2
    assert any("Manage Messages" in e for e in result["errors"])
    assert delete_attempts["msg2"] == 2
    assert result["done"] is True


async def test_purge_channel_uses_bulk_delete_when_permitted():
    settings.cron_secret = "testsecret"
    settings.discord_bot_token = "x"

    class FakeRes:
        def __init__(self, status_code, json_data=None):
            self.status_code = status_code
            self._json = json_data or {}

        def json(self):
            return self._json

    class FakeClient:
        call_count = 0

        async def get(self, url, headers=None, params=None):
            FakeClient.call_count += 1
            if FakeClient.call_count == 1:
                return FakeRes(200, json_data=[{"id": f"m{i}"} for i in range(5)])
            return FakeRes(200, json_data=[])

        async def post(self, url, headers=None, json=None):
            return FakeRes(200)

        async def delete(self, url, headers=None):
            raise AssertionError("should not fall back to single-delete when bulk succeeds")

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()

        class FakeReq:
            headers = {"authorization": "Bearer testsecret"}

        result = await main.purge_channel(FakeReq(), channel_id="456", max_batches=8)

    assert result["deleted_bulk"] == 5
    assert result["deleted_single"] == 0
    assert result["errors"] == []
