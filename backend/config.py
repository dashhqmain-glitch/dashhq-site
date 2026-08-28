from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_redirect_uri: str = ""
    discord_guild_id: str = ""
    jwt_secret: str = "not_set"
    frontend_url: str = "https://www.dashhq.site"

    discord_bot_token: Optional[str] = None
    citizen_role_id: Optional[str] = None

    supabase_url: str = ""
    supabase_service_role_key: str = ""
    cron_secret: str = ""

    discord_public_key: str = ""
    discord_applications_channel_id: str = ""
    discord_invite_channel_id: str = ""  # channel accepted citizens land in
    # Watchlist alerts channel: supply cut / sweep / volume spike / floor
    # move posts for anything on anyone's /watchlist.
    discord_nft_channel_id: str = ""

    # NFT Scope (formerly "mint radar") - a separate, deliberately
    # selective feed: a weighted scoring model (not a pass/fail checklist)
    # that studies genuine trading signals, actively screens out faked
    # top-offer manipulation, and covers both fresh mints AND already-
    # tracked collections showing real breakout momentum. Posts to its
    # own channel/server, fully decoupled from discord_nft_channel_id
    # above - empty means no posts anywhere (safe default) until a
    # channel is actually configured. nft_scope_enabled stays available
    # as an emergency off-switch without a redeploy.
    discord_nft_scope_channel_id: str = ""
    nft_scope_enabled: bool = True

    # /monitor is opt-in and normally DMs subscribers privately. When set,
    # any event a member has /monitor'd also gets posted here publicly
    # with those members @mentioned - visible even if their DMs are closed,
    # and lets others see who's tracking what. Leave blank to keep
    # /monitor fully private (DM-only), matching the original behavior.
    discord_nft_monitor_channel_id: str = ""

    # Separate from cron_secret on purpose: this one only guards the NFT
    # poller, which is reachable from a public GitHub Actions workflow log
    # surface far more often than the other cron endpoints - keeping it
    # distinct limits the blast radius if it ever leaks.
    nft_cron_secret: str = ""

    # Same isolation reasoning as nft_cron_secret, for the same structural
    # reason: the ACO drop-expiry poller also has to run every couple
    # minutes via a self-looping GitHub Actions workflow (Vercel's Hobby
    # cron tier only runs daily, far too coarse for a Status badge staff
    # expects to flip promptly), putting it on that same frequently-
    # exposed log surface. Kept scoped to only this one low-stakes,
    # idempotent endpoint rather than the shared cron_secret that also
    # guards aco-key-cleanup's thread-deletion.
    aco_cron_secret: str = ""

    # Pidgin AutoMod: English-only enforcement in #general (10-min timeout),
    # exempting every other channel (Discord AutoMod has no "only apply in
    # these channels" allowlist - only an exempt-list) so #lifestyle-chat
    # and everywhere else stays unaffected.
    discord_general_channel_id: str = ""
    discord_automod_alert_channel_id: str = ""

    # Where CI posts when a deploy's live health check or test suite
    # fails - proactive alerting instead of relying on GitHub's own
    # email-on-failure. Empty = the /cron/notify endpoint is a safe no-op.
    discord_ops_alert_channel_id: str = ""

    # ACO ticketing system: the announcement channel - drops post here,
    # wallet submissions happen here. Staff-posted, but open for every
    # member to view and interact with (submit wallets, etc.) - only
    # posting itself is meant to be staff-only, enforced via that
    # channel's own Discord permissions, not by this bot. Empty means the
    # whole feature is a safe no-op (matches every other channel setting
    # here).
    discord_aco_channel_id: str = ""
    # A SEPARATE channel from the one above - every support-ticket thread
    # spawns here instead of wherever "Need Help?" was clicked, so tickets
    # never mix into the public announcement channel's history. Meant to
    # be restricted (via Discord's own channel permissions, not this bot)
    # to the ACO staff role only - a customer never sees the channel
    # itself, only the one private thread the bot explicitly adds them to,
    # which Discord grants independently of whether they can see the
    # parent channel. Falls back to discord_aco_channel_id if unset, so
    # the feature degrades instead of breaking if this is never configured.
    discord_aco_support_channel_id: str = ""
    # A dedicated role for ACO staff, deliberately separate from
    # citizen_role_id and from full Manage-Server permissions - lets
    # specific trusted members run drops and handle support tickets
    # without needing full server-admin rights. Manage-Server holders
    # (_is_team_member) can always act too, as a safety net in case this
    # role is never set up.
    discord_aco_staff_role_id: str = ""
    # Admin visibility trail: every ACO staff action (drop created,
    # resolved/cancelled, wallet export pulled, support ticket opened/
    # closed) gets logged here, so the team has a full audit log in one
    # place without needing to watch the public ACO channel itself.
    discord_aco_admin_log_channel_id: str = ""

    # NFT Intel: wallet-following mint alerts, a deliberately different
    # product from NFT Scope. NFT Scope scores/discovers COLLECTIONS from
    # public trading signals with no idea who's buying. NFT Intel tracks a
    # curated list of WALLETS and alerts the instant one of them mints
    # anything, on any chain - a "what is this wallet doing right now"
    # feed, not a "here's a promising project" feed. Separate channel on
    # purpose so the two never compete for the same feed's attention.
    discord_nft_intel_channel_id: str = ""

    # Alchemy powers NFT Intel's mint detection directly from the chain,
    # deliberately NOT through OpenSea's own API - OpenSea only knows what
    # its own indexer has caught up to, which can meaningfully lag a fresh
    # contract's actual on-chain mints. Reading straight from Alchemy means
    # a mint is visible the moment it's confirmed on-chain, independent of
    # any marketplace's indexing speed. Empty means NFT Intel's cron is a
    # safe no-op, same pattern as every other integration in this file.
    alchemy_api_key: str = ""

    x_client_id: str = ""
    x_client_secret: str = ""
    x_redirect_uri: str = "https://www.dashhq.site/auth/x/callback"

    # Optional real OpenSea developer key (docs.opensea.io/reference/api-keys).
    # When set, this is used directly and the app never touches OpenSea's
    # anonymous /v2/auth/keys self-issuance endpoint at all - that endpoint
    # is IP-rate-limited and shared across however many other callers sit
    # behind the same egress IP, which is what took every OpenSea-backed
    # feature down at once. Leave blank to keep using the anonymous flow.
    opensea_api_key: str = ""

    model_config = {"env_file": ".env"}


settings = Settings()
