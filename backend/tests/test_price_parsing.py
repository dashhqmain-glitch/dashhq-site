"""_parse_eth_amount backs every price input across /pnl and /monitor
price - ETH or $USD, converted at a live rate."""
from unittest.mock import patch

import pytest

import main


async def test_parses_plain_eth_amount():
    async with main.httpx.AsyncClient() as client:
        assert await main._parse_eth_amount(client, "0.05", "mint price") == 0.05


async def test_parses_usd_with_dollar_prefix():
    async def fake_prices(client, ids):
        return {"ethereum": {"usd": 2000.0}}

    with patch.object(main, "_get_coingecko_prices", new=fake_prices):
        async with main.httpx.AsyncClient() as client:
            value = await main._parse_eth_amount(client, "$100", "mint price")
    assert abs(value - 0.05) < 1e-9


async def test_parses_usd_suffix_variants():
    async def fake_prices(client, ids):
        return {"ethereum": {"usd": 2000.0}}

    with patch.object(main, "_get_coingecko_prices", new=fake_prices):
        async with main.httpx.AsyncClient() as client:
            assert abs(await main._parse_eth_amount(client, "100 usd", "x") - 0.05) < 1e-9


async def test_parses_explicit_eth_suffix():
    async with main.httpx.AsyncClient() as client:
        assert await main._parse_eth_amount(client, "0.08 eth", "x") == 0.08


async def test_rejects_garbage_input():
    async with main.httpx.AsyncClient() as client:
        with pytest.raises(main.HTTPException) as exc_info:
            await main._parse_eth_amount(client, "abc", "mint price")
    assert exc_info.value.status_code == 400


async def test_rejects_negative_amount():
    async with main.httpx.AsyncClient() as client:
        with pytest.raises(main.HTTPException) as exc_info:
            await main._parse_eth_amount(client, "-5", "mint price")
    assert exc_info.value.status_code == 400
