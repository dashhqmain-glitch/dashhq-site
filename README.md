# Dash HQ — Website

Static, dependency-free frontend for the Dash HQ community site. No build step — open `index.html` in a browser or serve the folder.

## Structure
```
index.html      Landing page (markup only)
styles.css      Landing styles
app.js          Landing behaviour (scroll reveals, cube, cursor ghost,
                testimonial marquee, blog/research modals, FAQ, subscribe,
                legal modals, mobile nav, progress bar)
portal.html     Citizens Portal (Discord verification + membership card)
portal.css      Portal styles
portal.js       Portal behaviour (verification states, particles)
assets/         Logo + team photos (PNG)
```

## Run locally
```
npx serve .        # or: python3 -m http.server
```
Then open http://localhost:3000 (or the printed port).

## Integration notes (for backend work)
- **Newsletter** (`app.js`, subscribe form): posts directly to Substack
  `dashhq1.substack.com`. Swap `SUBSTACK_PUB` if the publication changes.
- **Discord verification** (`portal.js`): currently a front-end demo
  (`verify('member'|'notmember')`). Wire `verify()` to a real Discord
  OAuth + guild-membership check. The membership card data
  (name, handle, ID, tier) is hard-coded placeholder in `portal.html`.
- **Apply / Contact**: buttons point to `#join`. Hook to the real
  application form / Discord invite.
- **SEO/social**: `index.html` `<head>` has meta + Open Graph + JSON-LD.
  Upload a real `og-image.png` to the site root and confirm the domain
  (`https://dashhq.site`).
- Contact email in legal modals: `dashhqmain@gmail.com`.

## Fonts
Sora (display) · DM Sans (body) · JetBrains Mono (mono) — loaded from Google Fonts.

## Discord bot backend (`backend/`)
FastAPI app (`backend/main.py`), deployed as a Vercel serverless function
(`api/index.py` via Mangum) and driving the server's Discord Toolkit bot:
`/nft`, `/watchlist`, `/gas`, `/scan`, `/rug`, `/wallet`, `/xray`, `/pairs`,
application review, and the NFT alert system below. Runs on free-tier APIs
only (OpenSea's keyless "instant" key, CoinGecko, DexScreener, Blockscout,
public RPCs) - no paid data source anywhere in this backend.

### NFT alerts & mint radar (2026-08-07)
Two things were added on top of the existing `/nft` and `/watchlist`
commands:

**Richer `/nft` card** — floor (native + USD), top offer, all-time-high
floor, total volume, owners, contract address, and a chart-history note,
all in one embed. Floor/offer USD conversion and ATH both come straight
from data this backend already had access to (OpenSea's own live pricing
and a new self-collected snapshot history) rather than a new paid API.

**Proactive alerts**, posted automatically to one shared Discord channel
via `/cron/nft-poll` (deliberately one channel, not several, to keep the
server's channel list lean - the two alert types are visually distinct
by emoji/color, so interleaving them reads fine):
- **Watchlist alerts** - every collection on anyone's `/watchlist` is
  checked every ~5 minutes for a **supply cut** (total supply dropped),
  a **volume spike** (24h volume well above its recent average), or a
  **sweep** (several sales in a short window concentrated in very few
  buyer wallets).
- **Mint radar** - newly-created collections on Ethereum, Base,
  Polygon and Robinhood Chain are scanned and scored against an explicit
  8-point on-chain checklist (real social/website link, real description,
  recorded sales, floor set, healthy owner spread, plausible supply size,
  not single-wallet concentrated, category assigned). Verdict is
  🔥 High Potential (6-8 passed) / 👀 Worth Watching (3-5) / 🚮 Likely
  Junk (0-2) - every check is shown, not just the verdict, so it's never
  a black box.

The channel gets a single pinned "how this works" explainer the first
time `/cron/nft-init-channels` is run, so new members don't have to ask.

**Architecture** - polling, not a persistent worker or websocket:
```
GitHub Actions (free, cron: */5 * * * *)
        │  curl + Authorization: Bearer NFT_CRON_SECRET
        ▼
GET /cron/nft-poll  (Vercel, this backend)
        ├─ watchlist alert pass  → nft_snapshot_history + nft_alert_state (Supabase)
        └─ mint radar pass       → nft_mint_radar_seen (Supabase, dedupe)
                                 → posts embeds via bot-token REST call
                                   (same pattern already used for
                                   application notifications - no
                                   Discord gateway/websocket connection
                                   needed anywhere in this backend)
```
Chosen over a paid always-on worker (~$5-7/mo) or Vercel Pro (~$20/mo for
native minute-level cron) specifically to stay at $0. Trade-off: alerts
are only as fresh as the last 5-minute poll, and "sweep" detection reads
OpenSea's sale-event feed rather than a true real-time stream - fine for
this use case, worth revisiting only if sub-minute detection ever matters.

**New Supabase tables** (see `backend/schema.sql`, run once in the
Supabase SQL Editor): `nft_snapshot_history`, `nft_alert_state`,
`nft_mint_radar_seen`. The "tracked collections" list isn't a separate
table - it's just the distinct slugs across everyone's `/watchlist`.

**New env vars** (`backend/config.py`): `discord_nft_channel_id`,
`nft_cron_secret` (deliberately separate from the existing `cron_secret`
- this one is reachable from a public GitHub Actions workflow far more
often than the other cron endpoints, so keeping it distinct limits the
blast radius if it ever leaks).

**Setup steps this required** (all already done for the live deployment):
1. Run the new tables from `backend/schema.sql` in the Supabase SQL Editor.
2. Set `DISCORD_NFT_CHANNEL_ID` and `NFT_CRON_SECRET` in Vercel's
   Production environment.
3. Set the same `NFT_CRON_SECRET` as a GitHub Actions repo secret
   (`gh secret set NFT_CRON_SECRET`) so `.github/workflows/nft-poll.yml`
   can authenticate to the cron endpoint.
4. Give the bot Send Messages + Embed Links in that channel, then hit
   `/cron/nft-init-channels` once (same `NFT_CRON_SECRET`) to post and
   pin the explainer.

### `/monitor` — personal per-collection DM alerts (2026-08-08)
The shared alerts channel above is all-or-nothing (everyone sees every
watchlisted collection's alerts). `/monitor` is the personal, opt-in
layer on top of it: `/monitor set collection:<name>` shows a select menu
of five event types -

- 📈 **Floor Price Change** (±8% move)
- ✂️ **Supply Cut / Burns** (total supply decreased)
- 🌱 **Mint Progress** (total supply increased)
- 🧹 **Sweep Detected**
- 📊 **Volume Spike**

Picking any of them saves the choice (replacing, not appending, so
re-running the menu resets it - selecting nothing clears it entirely);
`/monitor list` shows everything a citizen is subscribed to; `/monitor
clear collection:<name>` wipes one collection's subscriptions. Whenever
`/cron/nft-poll` detects one of these events firing for a collection
(same detection logic as the public channel - this reuses it rather than
duplicating it), it now *also* DMs every citizen subscribed to that
specific (collection, event type) pair, with real message `content` text
(not just an embed) so it actually surfaces as a push notification and
not a silently-delivered message.

**New Supabase table**: `nft_watch_subscriptions` (also added to
`backend/schema.sql` - run it again, every statement is a safe re-run).
The poller's tracked-collections set is now the union of everyone's
`/watchlist` *and* everyone's `/monitor` subscriptions, so a collection
nobody watchlisted but someone specifically monitors still gets polled.

No new env vars, no new infrastructure - this rides the same 5-minute
`/cron/nft-poll` cycle and the same bot token (DMs use
`POST /users/@me/channels` + `POST /channels/{id}/messages`, still no
Discord gateway connection anywhere in this backend).
