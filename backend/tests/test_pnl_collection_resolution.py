"""/pnl used to blindly trust OpenSea search's top result for whatever
free-text the caller typed - confirmed live that a short/ambiguous query
like "naive" top-hits an entirely unrelated collection ("Naive by Olga
Fradina") and never even returns the intended one ("Naives by Mannay") in
its results at all. When the query DOES exactly name one of the returned
collections, that's almost certainly the one meant regardless of where
OpenSea ranked it - these tests pin that behavior down.

_pnl_render_core returns (png_bytes, matched_exactly) - matched_exactly is
False only for the free-text fallback with no exact match in the results,
which is exactly the case a brand-new project (nothing local to check
against) falls into. The Discord handler turns that into a visible warning
caption rather than a silently-maybe-wrong card."""
from unittest.mock import patch

import pytest

import main


def test_normalize_collection_key_makes_slug_and_name_forms_equal():
    # OpenSea pads a slug with trailing dashes to dedupe against an
    # existing one - "naives-by-mannay----" is the real slug seen live.
    assert main._normalize_collection_key("naives-by-mannay----") == "naives-by-mannay"
    assert main._normalize_collection_key("Naives by Mannay") == "naives-by-mannay"
    assert main._normalize_collection_key("  Naives   by   Mannay  ") == "naives-by-mannay"


def test_normalize_collection_key_is_case_and_separator_insensitive():
    assert main._normalize_collection_key("Pudgy_Penguins") == main._normalize_collection_key("pudgy penguins")


def test_extract_opensea_slug_from_real_url_shapes():
    # The exact real-world case reported live: a pasted OpenSea collection
    # URL in the collection field, with or without scheme/query string.
    assert main._extract_opensea_slug("https://opensea.io/collection/naives-by-mannay") == "naives-by-mannay"
    assert main._extract_opensea_slug("opensea.io/collection/naives-by-mannay") == "naives-by-mannay"
    assert main._extract_opensea_slug("https://opensea.io/collection/naives-by-mannay?tab=items") == "naives-by-mannay"


def test_extract_opensea_slug_returns_none_for_non_url_input():
    assert main._extract_opensea_slug("Naives by Mannay") is None
    assert main._extract_opensea_slug("0x263f61210bf2a0e0dc56fbd35813ccf04050ebfb") is None


def _collection(slug, name, floor=0.1):
    return {
        "slug": slug, "name": name, "image": None, "floor": floor,
        "symbol": "ETH", "listingUsdRate": 2000,
    }


def _patched_render(captured):
    def fake_render(data, project_thumb_bytes=None):
        captured.update(data)
        return b"fake-png-bytes"
    return fake_render


async def test_pnl_prefers_exact_name_match_over_opensea_top_result():
    # Real bug reproduction: OpenSea's own ranking puts the unrelated
    # collection first, but the caller's query exactly names the second
    # one - that exact match must win, not position 0.
    wrong_top_hit = _collection("naive-by-olga-fradina", "Naïve by Olga Fradina")
    intended = _collection("naives-by-mannay----", "Naives by Mannay")
    captured = {}

    async def fake_search(q):
        return [wrong_top_hit, intended]

    async def fake_collection_core(slug):
        return intended if slug == intended["slug"] else wrong_top_hit

    with patch.object(main, "_nft_search_core", new=fake_search), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main.pnl_card, "render_pnl_card", new=_patched_render(captured)):
        png, matched_exactly = await main._pnl_render_core("Naives by Mannay", "0.03", 1, "citizen")

    assert captured["project"] == "Naives by Mannay"
    assert matched_exactly is True


async def test_pnl_exact_slug_match_also_wins_over_top_result():
    wrong_top_hit = _collection("naive-by-olga-fradina", "Naïve by Olga Fradina")
    intended = _collection("naives-by-mannay----", "Naives by Mannay")
    captured = {}

    async def fake_search(q):
        return [wrong_top_hit, intended]

    async def fake_collection_core(slug):
        return intended if slug == intended["slug"] else wrong_top_hit

    with patch.object(main, "_nft_search_core", new=fake_search), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main.pnl_card, "render_pnl_card", new=_patched_render(captured)):
        png, matched_exactly = await main._pnl_render_core("naives-by-mannay", "0.03", 1, "citizen")

    assert captured["project"] == "Naives by Mannay"
    assert matched_exactly is True


async def test_pnl_falls_back_to_top_result_when_nothing_matches_exactly():
    # Genuinely ambiguous free text (no exact match in the result set at
    # all) - this is the one case that can't be fixed by matching alone
    # (e.g. a brand-new project nobody's looked up before), so it must
    # keep the old behavior (OpenSea's own top hit) rather than erroring
    # out or picking arbitrarily - but must flag matched_exactly=False so
    # the caller can warn instead of presenting it as certain.
    only_hit = _collection("naive-by-olga-fradina", "Naïve by Olga Fradina")
    captured = {}

    async def fake_search(q):
        return [only_hit]

    async def fake_collection_core(slug):
        return only_hit

    with patch.object(main, "_nft_search_core", new=fake_search), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main.pnl_card, "render_pnl_card", new=_patched_render(captured)):
        png, matched_exactly = await main._pnl_render_core("naive", "0.03", 1, "citizen")

    assert captured["project"] == "Naïve by Olga Fradina"
    assert matched_exactly is False


async def test_pnl_recognizes_opensea_url_pasted_into_collection_field():
    # Real bug reproduction: a pasted OpenSea URL in the free-text
    # `collection` field (not `contract_address`) used to fall through to
    # fuzzy search on the whole URL string, which never exact-matches
    # anything - triggering the "no exact match" warning even though the
    # URL unambiguously names one real collection. Must resolve directly,
    # with matched_exactly=True, no warning.
    intended = _collection("naives-by-mannay----", "Naives by Mannay")
    captured = {}

    async def exploding_search(q):
        raise AssertionError("a recognized OpenSea URL should never fall through to text search")

    async def fake_collection_core(slug):
        assert slug == "naives-by-mannay"
        return intended

    with patch.object(main, "_nft_search_core", new=exploding_search), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main.pnl_card, "render_pnl_card", new=_patched_render(captured)):
        png, matched_exactly = await main._pnl_render_core(
            "https://opensea.io/collection/naives-by-mannay", "0.03", 1, "citizen",
        )

    assert captured["project"] == "Naives by Mannay"
    assert matched_exactly is True


async def test_pnl_recognizes_opensea_url_pasted_into_contract_address_field():
    # Same recognition, but for someone who (reasonably) pastes the URL
    # into contract_address instead, since "contract address" reads to a
    # real user as "the thing that identifies it precisely".
    intended = _collection("naives-by-mannay----", "Naives by Mannay")
    captured = {}

    async def fake_collection_core(slug):
        assert slug == "naives-by-mannay"
        return intended

    with patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main.pnl_card, "render_pnl_card", new=_patched_render(captured)):
        png, matched_exactly = await main._pnl_render_core(
            "Naives by Mannay", "0.03", 1, "citizen",
            contract_address="https://opensea.io/collection/naives-by-mannay",
        )

    assert captured["project"] == "Naives by Mannay"
    assert matched_exactly is True


GOOD_ADDR = "0x263f61210bf2a0e0dc56fbd35813ccf04050ebfb"


async def test_pnl_contract_address_bypasses_search_entirely():
    # The authoritative path: a contract address names exactly one
    # collection, so this must never even call the ambiguous text search,
    # and must always report matched_exactly=True.
    intended = _collection("naives-by-mannay----", "Naives by Mannay")
    captured = {}

    async def exploding_search(q):
        raise AssertionError("contract_address given - should never fall through to text search")

    async def fake_resolve_by_contract(client, address):
        assert address == GOOD_ADDR
        return intended

    with patch.object(main, "_nft_search_core", new=exploding_search), \
         patch.object(main, "_nft_resolve_by_contract", new=fake_resolve_by_contract), \
         patch.object(main.pnl_card, "render_pnl_card", new=_patched_render(captured)):
        png, matched_exactly = await main._pnl_render_core(
            "literally anything, even a wrong name", "0.03", 1, "citizen",
            contract_address=GOOD_ADDR,
        )

    assert captured["project"] == "Naives by Mannay"
    assert matched_exactly is True


async def test_pnl_rejects_malformed_contract_address():
    with pytest.raises(main.HTTPException) as exc_info:
        await main._pnl_render_core("some name", "0.03", 1, "citizen", contract_address="not-an-address")
    assert exc_info.value.status_code == 400


async def test_pnl_reports_not_found_for_valid_but_unknown_contract_address():
    async def fake_resolve_by_contract(client, address):
        return None

    with patch.object(main, "_nft_resolve_by_contract", new=fake_resolve_by_contract):
        with pytest.raises(main.HTTPException) as exc_info:
            await main._pnl_render_core("some name", "0.03", 1, "citizen", contract_address=GOOD_ADDR)
    assert exc_info.value.status_code == 404


async def test_pnl_exact_match_search_among_multiple_wins_even_when_last():
    # Exact match must win regardless of its position in the results list,
    # not just when it happens to be second.
    a = _collection("collection-a", "Collection A")
    b = _collection("collection-b", "Collection B")
    intended = _collection("target-collection", "Target Collection")
    captured = {}

    async def fake_search(q):
        return [a, b, intended]

    async def fake_collection_core(slug):
        return {c["slug"]: c for c in (a, b, intended)}[slug]

    with patch.object(main, "_nft_search_core", new=fake_search), \
         patch.object(main, "_nft_collection_core", new=fake_collection_core), \
         patch.object(main.pnl_card, "render_pnl_card", new=_patched_render(captured)):
        png, matched_exactly = await main._pnl_render_core("Target Collection", "0.03", 1, "citizen")

    assert captured["project"] == "Target Collection"
    assert matched_exactly is True
