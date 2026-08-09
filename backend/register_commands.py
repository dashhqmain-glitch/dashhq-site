"""One-time (and re-run-on-change) script to register the toolkit bot's
slash commands with Discord. Guild-scoped (not global) so changes show up
instantly instead of waiting up to an hour for Discord's global cache.

Usage:  python register_commands.py
"""
import asyncio

import httpx

from config import settings

DISCORD_API = "https://discord.com/api/v10"

GAS_CHAIN_CHOICES = [
    {"name": "Ethereum", "value": "ethereum"},
    {"name": "BNB Chain", "value": "bsc"},
    {"name": "Polygon", "value": "polygon"},
    {"name": "Arbitrum", "value": "arbitrum"},
    {"name": "Optimism", "value": "optimism"},
    {"name": "Base", "value": "base"},
    {"name": "Avalanche", "value": "avalanche"},
    {"name": "Robinhood Chain", "value": "robinhood"},
    {"name": "Solana", "value": "solana"},
]
RUG_CHAIN_CHOICES = [
    {"name": "Ethereum", "value": "1"},
    {"name": "BNB Chain", "value": "56"},
    {"name": "Polygon", "value": "137"},
    {"name": "Arbitrum", "value": "42161"},
    {"name": "Optimism", "value": "10"},
    {"name": "Base", "value": "8453"},
    {"name": "Avalanche", "value": "43114"},
    {"name": "Solana", "value": "solana"},
]
PAIRS_CHAIN_CHOICES = [
    {"name": "Ethereum", "value": "eth"},
    {"name": "BNB Chain", "value": "bsc"},
    {"name": "Polygon", "value": "polygon_pos"},
    {"name": "Arbitrum", "value": "arbitrum"},
    {"name": "Optimism", "value": "optimism"},
    {"name": "Base", "value": "base"},
    {"name": "Avalanche", "value": "avax"},
    {"name": "Robinhood Chain", "value": "robinhood"},
    {"name": "Solana", "value": "solana"},
]

COMMANDS = [
    {
        "name": "dashboard",
        "description": "Browse every toolkit command and how to use it",
    },
    {
        "name": "xray",
        "description": "Heuristic on-chain score for any wallet address",
        "options": [{"name": "address", "description": "Wallet address or ENS name", "type": 3, "required": True}],
    },
    {
        "name": "gas",
        "description": "Current gas price on a chain",
        "options": [{"name": "chain", "description": "Which chain (default: Ethereum)", "type": 3, "required": False, "choices": GAS_CHAIN_CHOICES}],
    },
    {
        "name": "scan",
        "description": "Look up a token contract address: price, liquidity, volume",
        "options": [{"name": "address", "description": "Token contract address", "type": 3, "required": True}],
    },
    {
        "name": "rug",
        "description": "Quick red-flag check on a token contract",
        "options": [
            {"name": "address", "description": "Token contract address", "type": 3, "required": True},
            {"name": "chain", "description": "Which chain (default: Ethereum)", "type": 3, "required": False, "choices": RUG_CHAIN_CHOICES},
        ],
    },
    {
        "name": "nft",
        "description": "Look up an NFT collection: floor, top offer, ATH, volume, owners, contract",
        "options": [{"name": "collection", "description": "Collection name or contract address", "type": 3, "required": True}],
    },
    {
        "name": "wallet",
        "description": "Get a shareable wallet card with QR code",
        "options": [{"name": "address", "description": "Wallet address or ENS name", "type": 3, "required": True}],
    },
    {
        "name": "pairs",
        "description": "Freshly created trading pairs on a chain",
        "options": [{"name": "chain", "description": "Which chain (default: Ethereum)", "type": 3, "required": False, "choices": PAIRS_CHAIN_CHOICES}],
    },
    {
        "name": "watchlist",
        "description": "Manage your personal NFT watchlist",
        "options": [
            {
                "name": "add", "description": "Add a collection to your watchlist", "type": 1,
                "options": [{"name": "collection", "description": "Collection name or contract address", "type": 3, "required": True}],
            },
            {
                "name": "remove", "description": "Remove a collection from your watchlist", "type": 1,
                "options": [{"name": "collection", "description": "Collection name or contract address", "type": 3, "required": True}],
            },
            {"name": "list", "description": "Show everything on your watchlist", "type": 1},
        ],
    },
    {
        "name": "pnl",
        "description": "Generate a shareable PnL card for one of your mints",
        "options": [
            {"name": "collection", "description": "Collection name or contract address", "type": 3, "required": True},
            {"name": "mint_price", "description": "What you paid per NFT, in ETH", "type": 10, "required": True},
            {"name": "amount_minted", "description": "How many you minted", "type": 4, "required": True},
            {"name": "x_username", "description": "Your X handle to show on the card (default: your Discord name)", "type": 3, "required": False},
        ],
    },
    {
        "name": "monitor",
        "description": "Get personal DM pings for specific NFT collection events",
        "options": [
            {
                "name": "set", "description": "Choose what to be DM'd about for a collection", "type": 1,
                "options": [{"name": "collection", "description": "Collection name or contract address", "type": 3, "required": True}],
            },
            {
                "name": "clear", "description": "Stop all /monitor alerts for a collection", "type": 1,
                "options": [{"name": "collection", "description": "Collection name or contract address", "type": 3, "required": True}],
            },
            {
                "name": "price", "description": "Get DM'd when a collection's floor crosses a specific ETH price", "type": 1,
                "options": [
                    {"name": "collection", "description": "Collection name or contract address", "type": 3, "required": True},
                    {"name": "target", "description": "Exact target floor price in ETH, e.g. 0.03 (use this OR percent)", "type": 10, "required": False},
                    {"name": "percent", "description": "Target as % off current floor, e.g. -50 or 100 (use this OR target)", "type": 10, "required": False},
                    {"name": "loop", "description": "Keep re-alerting hourly while true, instead of firing once (default: off)", "type": 5, "required": False},
                ],
            },
            {"name": "list", "description": "Show everything you're monitoring", "type": 1},
        ],
    },
    {
        "name": "pidgin-exempt",
        "description": "Team only: exempt (or un-exempt) a member from the English-only #general timeout",
        # Same hidden-unless-Manage-Server pattern as /history - regular
        # members never see this command exists.
        "default_member_permissions": "32",
        "options": [
            {
                "name": "add", "description": "Exempt a member from the pidgin timeout", "type": 1,
                "options": [{"name": "user", "description": "Member to exempt", "type": 6, "required": True}],
            },
            {
                "name": "remove", "description": "Remove a member's exemption", "type": 1,
                "options": [{"name": "user", "description": "Member to un-exempt", "type": 6, "required": True}],
            },
        ],
    },
    {
        "name": "history",
        "description": "Team only: browse every citizenship application and its verdict",
        # Hidden from regular members entirely - Discord only shows/allows a
        # command to members holding this permission bit (32 = Manage
        # Server), unless a server admin overrides it in Integrations
        # settings. No new role or config needed to turn this on.
        "default_member_permissions": "32",
        "options": [
            {
                "name": "status", "description": "Filter by verdict (default: all)", "type": 3, "required": False,
                "choices": [
                    {"name": "All", "value": "all"},
                    {"name": "Pending", "value": "pending"},
                    {"name": "Accepted", "value": "accepted"},
                    {"name": "Declined", "value": "declined"},
                ],
            },
        ],
    },
]


async def main():
    if not settings.discord_bot_token:
        raise SystemExit("DISCORD_BOT_TOKEN not set")
    if not settings.discord_client_id:
        raise SystemExit("DISCORD_CLIENT_ID not set")
    if not settings.discord_guild_id:
        raise SystemExit("DISCORD_GUILD_ID not set")

    url = f"{DISCORD_API}/applications/{settings.discord_client_id}/guilds/{settings.discord_guild_id}/commands"
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.put(
            url,
            headers={"Authorization": f"Bot {settings.discord_bot_token}"},
            json=COMMANDS,
        )
        print("Status:", res.status_code)
        print(res.text)
        res.raise_for_status()
        print(f"\nRegistered {len(res.json())} commands to guild {settings.discord_guild_id}.")


if __name__ == "__main__":
    asyncio.run(main())
