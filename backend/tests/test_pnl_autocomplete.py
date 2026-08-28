"""Discord autocomplete for /pnl's collection field - the actual fix for
repeated live reports of the wrong project being shown: without
autocomplete, users kept typing short/partial names blind, with no
feedback on what would actually be searched. Picking a suggestion sends
its real SLUG as the value, so it round-trips through the exact-match
path in _pnl_render_core as a guaranteed-correct lookup - the same tier
as a raw contract address, without the user needing to know one.

Autocomplete responses (type 8) can never be deferred, same constraint as
MODAL - these tests also pin that a dispatch-level failure still returns
a valid type-8 response, never the generic type-4 error message, which
Discord would reject as invalid for this interaction type."""
from unittest.mock import patch

import main


def _autocomplete_payload(focused_value, focused_name="collection"):
    return {
        "type": 4,
        "data": {
            "name": "pnl",
            "options": [
                {"name": focused_name, "value": focused_value, "focused": True, "type": 3},
                {"name": "mint_price", "value": "0.03", "type": 3},
            ],
        },
    }


async def test_autocomplete_returns_slug_as_value_not_display_name():
    # This is the actual fix: picking a choice must send the SLUG, not the
    # pretty name, so it exact-matches in _pnl_render_core afterward.
    fake_search_response = {
        "results": [
            {"type": "collection", "collection": {"collection": "naives-by-mannay----", "name": "Naives by Mannay"}},
            {"type": "collection", "collection": {"collection": "naive-by-olga-fradina", "name": "Naïve by Olga Fradina"}},
        ]
    }

    async def fake_opensea_get(client, path, params=None):
        assert path == "/search"
        return fake_search_response

    with patch.object(main, "_opensea_get", new=fake_opensea_get):
        choices = await main._nft_autocomplete_choices("naiv")

    assert choices == [
        {"name": "Naives by Mannay", "value": "naives-by-mannay----"},
        {"name": "Naïve by Olga Fradina", "value": "naive-by-olga-fradina"},
    ]


async def test_autocomplete_recognizes_pasted_opensea_url_directly():
    # Real live report: typing/pasting the OpenSea URL showed zero
    # suggestions, since it doesn't match anything in OpenSea's own NAME
    # search. Must be recognized and confirmed directly, not fuzzy-searched.
    async def fake_collection_core(slug):
        assert slug == "naives-by-mannay"
        return {"slug": "naives-by-mannay----", "name": "Naives by Mannay"}

    async def exploding_search(client, path, params=None):
        raise AssertionError("a recognized URL should never fall through to fuzzy text search")

    with patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main, "_opensea_get", new=exploding_search):
        choices = await main._nft_autocomplete_choices("https://opensea.io/collection/naives-by-mannay")

    assert choices == [{"name": "Naives by Mannay", "value": "naives-by-mannay----"}]


async def test_autocomplete_recognizes_raw_contract_address_directly():
    GOOD_ADDR = "0x263f61210bf2a0e0dc56fbd35813ccf04050ebfb"

    async def fake_resolve_by_contract(client, address):
        assert address == GOOD_ADDR
        return {"slug": "naives-by-mannay----", "name": "Naives by Mannay"}

    async def exploding_search(client, path, params=None):
        raise AssertionError("a recognized address should never fall through to fuzzy text search")

    with patch.object(main, "_nft_resolve_by_contract", new=fake_resolve_by_contract), \
         patch.object(main, "_opensea_get", new=exploding_search):
        choices = await main._nft_autocomplete_choices(GOOD_ADDR)

    assert choices == [{"name": "Naives by Mannay", "value": "naives-by-mannay----"}]


async def test_autocomplete_returns_empty_for_a_url_that_does_not_resolve():
    async def fake_collection_core(slug):
        raise main.HTTPException(status_code=404, detail="Collection not found")

    with patch.object(main, "_nft_collection_core", new=fake_collection_core):
        choices = await main._nft_autocomplete_choices("https://opensea.io/collection/does-not-exist")

    assert choices == []


async def test_autocomplete_skips_search_for_very_short_input():
    async def exploding_opensea_get(client, path, params=None):
        raise AssertionError("should not search OpenSea for input under 2 chars")

    with patch.object(main, "_opensea_get", new=exploding_opensea_get):
        assert await main._nft_autocomplete_choices("n") == []
        assert await main._nft_autocomplete_choices("") == []


async def test_autocomplete_caps_at_discord_25_choice_limit():
    fake_search_response = {
        "results": [
            {"type": "collection", "collection": {"collection": f"slug-{i}", "name": f"Name {i}"}}
            for i in range(40)
        ]
    }

    async def fake_opensea_get(client, path, params=None):
        return fake_search_response

    with patch.object(main, "_opensea_get", new=fake_opensea_get):
        choices = await main._nft_autocomplete_choices("test")

    assert len(choices) == 25


async def test_autocomplete_handles_opensea_outage_gracefully():
    async def fake_opensea_get(client, path, params=None):
        return None

    with patch.object(main, "_opensea_get", new=fake_opensea_get):
        assert await main._nft_autocomplete_choices("something") == []


async def test_handle_pnl_autocomplete_finds_the_focused_option():
    captured_query = {}

    async def fake_choices(q):
        captured_query["q"] = q
        return [{"name": "Naives by Mannay", "value": "naives-by-mannay----"}]

    with patch.object(main, "_nft_autocomplete_choices", new=fake_choices):
        result = await main._handle_pnl_autocomplete(_autocomplete_payload("naiv"))

    assert result == {"type": 8, "data": {"choices": [{"name": "Naives by Mannay", "value": "naives-by-mannay----"}]}}
    assert captured_query["q"] == "naiv"


async def test_handle_pnl_autocomplete_ignores_a_different_focused_field():
    # mint_price also has a value, but it's not the field being typed into
    # right now - only the genuinely focused option should trigger search.
    async def exploding_choices(q):
        raise AssertionError("should not search when collection isn't the focused field")

    with patch.object(main, "_nft_autocomplete_choices", new=exploding_choices):
        result = await main._handle_pnl_autocomplete(_autocomplete_payload("0.03", focused_name="mint_price"))

    assert result == {"type": 8, "data": {"choices": []}}


async def test_handle_pnl_autocomplete_never_raises_on_search_failure():
    async def failing_choices(q):
        raise RuntimeError("OpenSea exploded")

    with patch.object(main, "_nft_autocomplete_choices", new=failing_choices):
        result = await main._handle_pnl_autocomplete(_autocomplete_payload("naiv"))

    assert result == {"type": 8, "data": {"choices": []}}


async def test_dispatch_routes_pnl_autocomplete_correctly():
    payload = _autocomplete_payload("naiv")

    async def fake_handler(p):
        return {"type": 8, "data": {"choices": [{"name": "X", "value": "x"}]}}

    with patch.object(main, "_handle_pnl_autocomplete", new=fake_handler):
        result = await main._dispatch_interaction(payload, 4)

    assert result == {"type": 8, "data": {"choices": [{"name": "X", "value": "x"}]}}


async def test_dispatch_returns_empty_choices_for_autocomplete_on_other_commands():
    payload = {"type": 4, "data": {"name": "nft", "options": [{"name": "collection", "value": "x", "focused": True}]}}
    result = await main._dispatch_interaction(payload, 4)
    assert result == {"type": 8, "data": {"choices": []}}


async def test_outer_safety_net_returns_valid_autocomplete_type_on_dispatch_crash():
    # The generic type-4 error message fallback is invalid for interaction
    # type 4 (autocomplete) - Discord requires type 8 regardless of
    # outcome. A crash anywhere in dispatch must not violate that.
    from fastapi import Request

    class FakeRequest:
        async def body(self):
            return b'{"type":4,"data":{"name":"pnl","options":[{"name":"collection","value":"x","focused":true}]}}'

        headers = {"x-signature-ed25519": "sig", "x-signature-timestamp": "ts"}

        async def json(self):
            return {"type": 4, "data": {"name": "pnl", "options": [{"name": "collection", "value": "x", "focused": True}]}}

    async def crashing_dispatch(payload, itype):
        raise RuntimeError("boom")

    with patch.object(main, "_verify_discord_signature", return_value=True), \
         patch.object(main, "_dispatch_interaction", new=crashing_dispatch):
        result = await main.discord_interactions(FakeRequest())

    assert result == {"type": 8, "data": {"choices": []}}
