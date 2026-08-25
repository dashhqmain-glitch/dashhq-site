"""Tests for NFT Intel - wallet-following mint alerts. Deliberately a
different product from NFT Scope: this tracks specific WALLETS and alerts
when they mint, not collections worth discovering."""
from unittest.mock import patch

import main
from config import settings

SEED_ADDR = "0x8ceec06ebd2910879a0266ca9643431cd9b5baef"
CO_MINTER_ADDR = "0x9abd78db91e716280f42d83541a7b39268a34278"


def _mint_event(to=SEED_ADDR, from_addr=main._NFT_INTEL_NULL_ADDRESS, event_type="transfer",
                 chain="ethereum", contract="0xcontract", token_id=42, collection="cool-collection", name=None, image=None):
    return {
        "event_type": event_type,
        "chain": chain,
        "from_address": from_addr,
        "to_address": to,
        "nft": {
            "chain": chain, "contract": contract, "identifier": token_id,
            "collection": collection, "name": name, "image_url": image,
        },
    }


# ── _alchemy_transfer_to_event ──────────────────────────────────────────────

def test_alchemy_transfer_converts_hex_token_id_to_decimal_string():
    transfer = {
        "from": main._NFT_INTEL_NULL_ADDRESS, "to": SEED_ADDR,
        "tokenId": "0x00000000000000000000000000000000000000000000000000000000000015b1",
        "rawContract": {"address": "0xabc"},
    }
    event = main._alchemy_transfer_to_event(transfer, "ethereum")
    assert event["nft"]["identifier"] == str(int("15b1", 16))  # 5553
    assert event["nft"]["contract"] == "0xabc"
    assert event["chain"] == "ethereum"
    assert event["from_address"] == main._NFT_INTEL_NULL_ADDRESS
    assert event["to_address"] == SEED_ADDR


def test_alchemy_transfer_handles_plain_integer_token_id():
    # Not every response necessarily hex-encodes tokenId - handle a plain
    # int/string the same as a "0x..." one rather than assuming one shape.
    transfer = {"from": main._NFT_INTEL_NULL_ADDRESS, "to": SEED_ADDR, "tokenId": "42", "rawContract": {"address": "0xabc"}}
    event = main._alchemy_transfer_to_event(transfer, "ethereum")
    assert event["nft"]["identifier"] == "42"


def test_alchemy_transfer_returns_none_when_contract_or_token_id_missing():
    assert main._alchemy_transfer_to_event({"from": "0x0", "to": SEED_ADDR, "tokenId": "0x1", "rawContract": {}}, "ethereum") is None
    assert main._alchemy_transfer_to_event({"from": "0x0", "to": SEED_ADDR, "rawContract": {"address": "0xabc"}}, "ethereum") is None


# ── _alchemy_rpc / _alchemy_get_nft_metadata: safe no-op without a key ──────

async def test_alchemy_rpc_returns_none_without_a_configured_key_and_makes_no_network_call():
    settings.alchemy_api_key = ""

    class ExplodingClient:
        async def post(self, *a, **kw):
            raise AssertionError("must not make a network call with no API key configured")

    result = await main._alchemy_rpc(ExplodingClient(), "ethereum", "alchemy_getAssetTransfers", {})
    assert result is None


async def test_alchemy_rpc_returns_none_for_an_unknown_chain():
    settings.alchemy_api_key = "testkey"

    class ExplodingClient:
        async def post(self, *a, **kw):
            raise AssertionError("must not make a network call for a chain with no subdomain mapping")

    result = await main._alchemy_rpc(ExplodingClient(), "some-unmapped-chain", "alchemy_getAssetTransfers", {})
    assert result is None


async def test_alchemy_get_nft_metadata_returns_none_without_a_configured_key():
    settings.alchemy_api_key = ""

    class ExplodingClient:
        async def get(self, *a, **kw):
            raise AssertionError("must not make a network call with no API key configured")

    result = await main._alchemy_get_nft_metadata(ExplodingClient(), "ethereum", "0xabc", "1")
    assert result is None


# ── _nft_intel_explorer_url ────────────────────────────────────────────────

def test_explorer_url_known_chain():
    assert main._nft_intel_explorer_url("ethereum", "0xabc") == "https://etherscan.io/token/0xabc"
    assert main._nft_intel_explorer_url("base", "0xabc") == "https://basescan.org/token/0xabc"


def test_explorer_url_unknown_chain_returns_none():
    assert main._nft_intel_explorer_url("some-unknown-chain", "0xabc") is None
    assert main._nft_intel_explorer_url(None, "0xabc") is None


# ── _nft_intel_is_mint ──────────────────────────────────────────────────────

def test_is_mint_true_for_null_address_transfer_to_the_wallet():
    event = _mint_event(to=SEED_ADDR)
    assert main._nft_intel_is_mint(event, SEED_ADDR) is True


def test_is_mint_false_for_a_real_transfer_between_wallets():
    # Not a mint - from_address is a real prior owner, not the null address.
    event = _mint_event(to=SEED_ADDR, from_addr="0x1111111111111111111111111111111111111111")
    assert main._nft_intel_is_mint(event, SEED_ADDR) is False


def test_is_mint_false_when_it_mints_to_a_different_wallet():
    # A null-address transfer, but not TO the wallet we're checking - this
    # is someone else's mint event showing up in a shared events response,
    # must not be misattributed.
    event = _mint_event(to="0x2222222222222222222222222222222222222222")
    assert main._nft_intel_is_mint(event, SEED_ADDR) is False


def test_is_mint_false_for_non_transfer_event_types():
    event = _mint_event(to=SEED_ADDR, event_type="sale")
    assert main._nft_intel_is_mint(event, SEED_ADDR) is False


def test_is_mint_case_insensitive_address_matching():
    event = _mint_event(to=SEED_ADDR.upper())
    assert main._nft_intel_is_mint(event, SEED_ADDR) is True


# ── _nft_intel_embed ──────────────────────────────────────────────────────

def test_embed_labels_seed_wallets_correctly():
    event = _mint_event(name="Cool #42")
    embed = main._nft_intel_embed(event, {"address": SEED_ADDR, "source": "seed"})
    reason_field = next(f for f in embed["fields"] if f["name"] == "Why tracked")
    assert reason_field["value"] == "Seed tracked wallet"
    assert "Cool #42" in embed["title"]


def test_embed_labels_co_minter_wallets_with_provenance():
    event = _mint_event(to=CO_MINTER_ADDR)
    embed = main._nft_intel_embed(event, {"address": CO_MINTER_ADDR, "source": "co_minter", "discovered_via": "azuki"})
    reason_field = next(f for f in embed["fields"] if f["name"] == "Why tracked")
    assert "co-minted" in reason_field["value"]
    assert "azuki" in reason_field["value"]


def test_embed_includes_opensea_and_explorer_links_when_available():
    event = _mint_event(chain="ethereum", contract="0xabc", token_id=7)
    embed = main._nft_intel_embed(event, {"address": SEED_ADDR, "source": "seed"})
    links_field = next(f for f in embed["fields"] if f["name"] == "Links")
    assert "opensea.io/assets/ethereum/0xabc/7" in links_field["value"]
    assert "etherscan.io" in links_field["value"]


def test_embed_omits_links_field_on_unknown_chain_with_no_explorer():
    event = _mint_event(chain="some-obscure-chain")
    embed = main._nft_intel_embed(event, {"address": SEED_ADDR, "source": "seed"})
    links_field = next((f for f in embed["fields"] if f["name"] == "Links"), None)
    # OpenSea link still renders (chain-agnostic URL format) even with no explorer mapping.
    assert links_field is not None
    assert "opensea.io" in links_field["value"]
    assert "Explorer" not in links_field["value"]


def test_embed_carries_nft_intel_branding_distinct_from_nft_scope():
    event = _mint_event()
    embed = main._nft_intel_embed(event, {"address": SEED_ADDR, "source": "seed"})
    assert embed["color"] == main._NFT_INTEL_COLOR
    assert embed["color"] != main._ACO_BLUE
    assert "NFT Intel" in embed["footer"]["text"]
    assert "NFT Scope" not in embed["footer"]["text"]


# ── cron_nft_intel: auth / config guards ────────────────────────────────────

class FakeRequest:
    def __init__(self, auth_header=None):
        self.headers = {"authorization": auth_header} if auth_header else {}


async def test_cron_nft_intel_rejects_missing_secret():
    settings.nft_cron_secret = "realsecret"
    try:
        await main.cron_nft_intel(FakeRequest())
        assert False, "should have raised"
    except main.HTTPException as exc:
        assert exc.status_code == 401


async def test_cron_nft_intel_rejects_wrong_secret():
    settings.nft_cron_secret = "realsecret"
    try:
        await main.cron_nft_intel(FakeRequest("Bearer wrongsecret"))
        assert False, "should have raised"
    except main.HTTPException as exc:
        assert exc.status_code == 401


async def test_cron_nft_intel_noop_when_channel_not_configured():
    settings.nft_cron_secret = "realsecret"
    settings.discord_nft_intel_channel_id = ""
    result = await main.cron_nft_intel(FakeRequest("Bearer realsecret"))
    assert result["polled"] == 0
    assert "not configured" in result["reason"]


async def test_cron_nft_intel_noop_when_alchemy_key_not_configured():
    settings.nft_cron_secret = "realsecret"
    settings.discord_nft_intel_channel_id = "intel-chan"
    settings.alchemy_api_key = ""
    result = await main.cron_nft_intel(FakeRequest("Bearer realsecret"))
    assert result["polled"] == 0
    assert "Alchemy" in result["reason"]


async def test_cron_nft_intel_skips_tick_when_alchemy_unhealthy():
    settings.nft_cron_secret = "realsecret"
    settings.discord_nft_intel_channel_id = "intel-chan"
    settings.alchemy_api_key = "testkey"

    async def fake_unhealthy(client):
        return False

    async def fake_batch(client):
        raise AssertionError("should never fetch a batch when Alchemy is unhealthy")

    with patch.object(main, "_alchemy_healthy", new=fake_unhealthy), \
         patch.object(main, "_nft_intel_poll_batch", new=fake_batch):
        async with main.httpx.AsyncClient() as client:
            result = await main.cron_nft_intel(FakeRequest("Bearer realsecret"))

    assert result["polled"] == 0
    assert "rate-limited" in result["reason"]


# ── cron_nft_intel: end-to-end mint detection ───────────────────────────────

async def test_cron_nft_intel_detects_a_new_mint_and_alerts_once():
    settings.nft_cron_secret = "realsecret"
    settings.discord_nft_intel_channel_id = "intel-chan"
    settings.alchemy_api_key = "testkey"
    posted = []
    seen_store = set()
    polled_marks = []

    async def fake_healthy(client):
        return True

    async def fake_batch(client):
        return [{"address": SEED_ADDR, "source": "seed", "discovered_via": None}]

    async def fake_events(client, address):
        return [_mint_event(to=address, chain="ethereum", contract="0xabc", token_id=1, collection="cool-collection")]

    async def fake_seen(client, chain, contract, token_id):
        return (chain, contract, token_id) in seen_store

    async def fake_mark_seen(client, chain, contract, token_id, wallet, slug):
        seen_store.add((chain, contract, token_id))

    async def fake_metadata(client, chain, contract, token_id):
        return {"name": "Cool #1", "image": {"cachedUrl": "https://img"}, "contract": {"openSeaMetadata": {"collectionName": "cool-collection"}}}

    async def fake_post(client, channel_id, embed, content=None):
        posted.append((channel_id, embed))
        return True

    async def fake_mark_polled(client, address):
        polled_marks.append(address)

    async def fake_tracked_count(client):
        return 1

    async def fake_discover(client, chain, contract, exclude):
        return []

    async def fake_add_co_minters(client, addresses, label):
        return 0

    with patch.object(main, "_alchemy_healthy", new=fake_healthy), \
         patch.object(main, "_nft_intel_poll_batch", new=fake_batch), \
         patch.object(main, "_nft_intel_wallet_transfer_events", new=fake_events), \
         patch.object(main, "_nft_intel_seen", new=fake_seen), \
         patch.object(main, "_nft_intel_mark_seen", new=fake_mark_seen), \
         patch.object(main, "_alchemy_get_nft_metadata", new=fake_metadata), \
         patch.object(main, "_post_channel_message", new=fake_post), \
         patch.object(main, "_nft_intel_mark_polled", new=fake_mark_polled), \
         patch.object(main, "_nft_intel_tracked_count", new=fake_tracked_count), \
         patch.object(main, "_nft_intel_discover_co_minters", new=fake_discover), \
         patch.object(main, "_nft_intel_add_co_minters", new=fake_add_co_minters):
        async with main.httpx.AsyncClient() as client:
            result = await main.cron_nft_intel(FakeRequest("Bearer realsecret"))

    assert result["polled"] == 1
    assert result["alerted"] == 1
    assert len(posted) == 1
    assert posted[0][0] == "intel-chan"
    # Enrichment landed in the posted embed - proves the metadata lookup
    # actually feeds the alert, not just gets called and discarded.
    assert "Cool #1" in posted[0][1]["title"]
    assert polled_marks == [SEED_ADDR]


async def test_cron_nft_intel_never_reposts_an_already_seen_mint():
    settings.nft_cron_secret = "realsecret"
    settings.discord_nft_intel_channel_id = "intel-chan"
    settings.alchemy_api_key = "testkey"
    posted = []

    async def fake_healthy(client):
        return True

    async def fake_batch(client):
        return [{"address": SEED_ADDR, "source": "seed", "discovered_via": None}]

    async def fake_events(client, address):
        return [_mint_event(to=address, chain="ethereum", contract="0xabc", token_id=1)]

    async def fake_seen(client, chain, contract, token_id):
        return True  # already recorded from a prior tick

    async def fake_post(client, channel_id, embed, content=None):
        posted.append(embed)
        return True

    async def fake_mark_polled(client, address):
        return None

    with patch.object(main, "_alchemy_healthy", new=fake_healthy), \
         patch.object(main, "_nft_intel_poll_batch", new=fake_batch), \
         patch.object(main, "_nft_intel_wallet_transfer_events", new=fake_events), \
         patch.object(main, "_nft_intel_seen", new=fake_seen), \
         patch.object(main, "_post_channel_message", new=fake_post), \
         patch.object(main, "_nft_intel_mark_polled", new=fake_mark_polled):
        async with main.httpx.AsyncClient() as client:
            result = await main.cron_nft_intel(FakeRequest("Bearer realsecret"))

    assert result["alerted"] == 0
    assert posted == []


async def test_cron_nft_intel_ignores_non_mint_transfer_events():
    # A regular wallet-to-wallet transfer (not from the null address) must
    # never be mistaken for a mint and alerted on.
    settings.nft_cron_secret = "realsecret"
    settings.discord_nft_intel_channel_id = "intel-chan"
    settings.alchemy_api_key = "testkey"
    posted = []

    async def fake_healthy(client):
        return True

    async def fake_batch(client):
        return [{"address": SEED_ADDR, "source": "seed", "discovered_via": None}]

    async def fake_events(client, address):
        return [_mint_event(to=address, from_addr="0x1111111111111111111111111111111111111111")]

    async def fake_post(client, channel_id, embed, content=None):
        posted.append(embed)
        return True

    async def fake_mark_polled(client, address):
        return None

    with patch.object(main, "_alchemy_healthy", new=fake_healthy), \
         patch.object(main, "_nft_intel_poll_batch", new=fake_batch), \
         patch.object(main, "_nft_intel_wallet_transfer_events", new=fake_events), \
         patch.object(main, "_post_channel_message", new=fake_post), \
         patch.object(main, "_nft_intel_mark_polled", new=fake_mark_polled):
        async with main.httpx.AsyncClient() as client:
            result = await main.cron_nft_intel(FakeRequest("Bearer realsecret"))

    assert result["alerted"] == 0
    assert posted == []


async def test_cron_nft_intel_triggers_co_minter_discovery_only_for_seed_wallets():
    # A co-minter's OWN mint must never itself fan out into more discovery -
    # only a SEED wallet's mint should, or the tracked list could grow
    # unboundedly off nothing but the graph's own momentum.
    settings.nft_cron_secret = "realsecret"
    settings.discord_nft_intel_channel_id = "intel-chan"
    settings.alchemy_api_key = "testkey"
    discover_calls = []

    async def fake_healthy(client):
        return True

    async def fake_batch(client):
        return [{"address": CO_MINTER_ADDR, "source": "co_minter", "discovered_via": "azuki"}]

    async def fake_events(client, address):
        return [_mint_event(to=address, chain="ethereum", contract="0xabc", token_id=1, collection="cool-collection")]

    async def fake_seen(client, chain, contract, token_id):
        return False

    async def fake_mark_seen(client, chain, contract, token_id, wallet, slug):
        return None

    async def fake_metadata(client, chain, contract, token_id):
        return None

    async def fake_post(client, channel_id, embed, content=None):
        return True

    async def fake_mark_polled(client, address):
        return None

    async def fake_discover(client, chain, contract, exclude):
        discover_calls.append(contract)
        return []

    with patch.object(main, "_alchemy_healthy", new=fake_healthy), \
         patch.object(main, "_nft_intel_poll_batch", new=fake_batch), \
         patch.object(main, "_nft_intel_wallet_transfer_events", new=fake_events), \
         patch.object(main, "_nft_intel_seen", new=fake_seen), \
         patch.object(main, "_nft_intel_mark_seen", new=fake_mark_seen), \
         patch.object(main, "_alchemy_get_nft_metadata", new=fake_metadata), \
         patch.object(main, "_post_channel_message", new=fake_post), \
         patch.object(main, "_nft_intel_mark_polled", new=fake_mark_polled), \
         patch.object(main, "_nft_intel_discover_co_minters", new=fake_discover):
        async with main.httpx.AsyncClient() as client:
            await main.cron_nft_intel(FakeRequest("Bearer realsecret"))

    assert discover_calls == []


async def test_cron_nft_intel_stops_discovering_co_minters_once_cap_reached():
    settings.nft_cron_secret = "realsecret"
    settings.discord_nft_intel_channel_id = "intel-chan"
    settings.alchemy_api_key = "testkey"
    discover_calls = []

    async def fake_healthy(client):
        return True

    async def fake_batch(client):
        return [{"address": SEED_ADDR, "source": "seed", "discovered_via": None}]

    async def fake_events(client, address):
        return [_mint_event(to=address, chain="ethereum", contract="0xabc", token_id=1, collection="cool-collection")]

    async def fake_seen(client, chain, contract, token_id):
        return False

    async def fake_mark_seen(client, chain, contract, token_id, wallet, slug):
        return None

    async def fake_metadata(client, chain, contract, token_id):
        return None

    async def fake_post(client, channel_id, embed, content=None):
        return True

    async def fake_mark_polled(client, address):
        return None

    async def fake_tracked_count(client):
        return main._NFT_INTEL_MAX_TRACKED_WALLETS  # already at the ceiling

    async def fake_discover(client, chain, contract, exclude):
        discover_calls.append(contract)
        return []

    with patch.object(main, "_alchemy_healthy", new=fake_healthy), \
         patch.object(main, "_nft_intel_poll_batch", new=fake_batch), \
         patch.object(main, "_nft_intel_wallet_transfer_events", new=fake_events), \
         patch.object(main, "_nft_intel_seen", new=fake_seen), \
         patch.object(main, "_nft_intel_mark_seen", new=fake_mark_seen), \
         patch.object(main, "_alchemy_get_nft_metadata", new=fake_metadata), \
         patch.object(main, "_post_channel_message", new=fake_post), \
         patch.object(main, "_nft_intel_mark_polled", new=fake_mark_polled), \
         patch.object(main, "_nft_intel_tracked_count", new=fake_tracked_count), \
         patch.object(main, "_nft_intel_discover_co_minters", new=fake_discover):
        async with main.httpx.AsyncClient() as client:
            await main.cron_nft_intel(FakeRequest("Bearer realsecret"))

    assert discover_calls == []


# ── /nft-intel-wallets command ──────────────────────────────────────────────

def _payload(permissions="0"):
    return {
        "member": {"user": {"id": "user1", "username": "someone", "global_name": "Someone"}, "roles": [], "permissions": permissions},
    }


async def test_nft_intel_wallets_command_rejects_non_team_members():
    result = await main._handle_nft_intel_wallets_command(_payload(permissions="0"))
    assert result["type"] == 4
    assert "Team members only" in result["data"]["content"]


async def test_nft_intel_wallets_command_shows_seed_and_co_minter_breakdown():
    class FakeRes:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

        def raise_for_status(self):
            pass

    class FakeClient:
        async def get(self, url, headers=None, params=None):
            return FakeRes([
                {"address": SEED_ADDR, "source": "seed", "discovered_via": None, "added_at": "x"},
                {"address": CO_MINTER_ADDR, "source": "co_minter", "discovered_via": "azuki", "added_at": "y"},
            ])

    with patch("main.httpx.AsyncClient") as MockClient:
        MockClient.return_value.__aenter__.return_value = FakeClient()
        result = await main._handle_nft_intel_wallets_command(_payload(permissions="32"))

    embed = result["data"]["embeds"][0]
    assert "2** total tracked (1 seed, 1 auto-discovered" in embed["description"]
    assert CO_MINTER_ADDR in embed["description"]
    assert "azuki" in embed["description"]
