-- Run once in the Supabase SQL Editor for the Dash HQ project.

create table if not exists members (
  discord_id        text primary key,
  username          text not null,
  global_name       text,
  nickname          text,
  display_name      text not null,
  avatar_url        text,
  roles             text[] not null default '{}',
  tier              text not null default 'CITIZEN',
  first_seen_at     timestamptz not null default now(),
  joined_at         timestamptz,
  left_at           timestamptz,
  is_active         boolean not null default true,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index if not exists members_is_active_idx on members (is_active);

-- search_path pinned to empty: without this, the function resolves
-- unqualified names against whatever search_path is active at CALL time,
-- not definition time - a classic search-path-hijack vector (a malicious
-- role could create an object earlier in their own search_path to shadow
-- what this function resolves to). now() is a pg_catalog builtin, always
-- resolvable regardless, so this is a pure hardening with no behavior
-- change. Flagged by Supabase's linter as "Function Search Path Mutable."
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql
set search_path = '';

drop trigger if exists members_set_updated_at on members;
create trigger members_set_updated_at
  before update on members
  for each row execute function set_updated_at();

-- The backend talks to Supabase using the service_role key, which bypasses
-- RLS automatically, so this table doesn't need any policies to still work.
alter table members enable row level security;

create table if not exists applications (
  id                  uuid primary key default gen_random_uuid(),
  name                text not null,
  x_user_id           text,          -- X (Twitter) numeric user id, from OAuth - the real dedup key
  x_username          text,          -- X handle at time of connecting, for display only
  intro               text not null,
  communities         text not null,
  value               text not null,
  followed_team       boolean not null default false,
  status              text not null default 'pending', -- pending | accepted | declined
  decline_reason      text,          -- shown to the applicant on their status page
  invite_url          text,          -- one-time Discord invite, saved on accept
  reviewed_by         text,
  reviewed_at         timestamptz,
  discord_channel_id  text,
  discord_message_id  text,
  submitted_at        timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create index if not exists applications_status_idx on applications (status);
create index if not exists applications_x_user_id_idx on applications (x_user_id);

drop trigger if exists applications_set_updated_at on applications;
create trigger applications_set_updated_at
  before update on applications
  for each row execute function set_updated_at();

alter table applications enable row level security;

-- Migration for an applications table created before X OAuth replaced the
-- typed-in X profile link: adds the new identity/decline/invite columns and
-- frees x_profile from its old not-null constraint (existing rows keep
-- their data, new rows just stop writing to it). Safe to re-run.
alter table applications add column if not exists x_user_id text;
alter table applications add column if not exists x_username text;
alter table applications add column if not exists decline_reason text;
alter table applications add column if not exists invite_url text;
alter table applications alter column x_profile drop not null;
create index if not exists applications_x_user_id_idx on applications (x_user_id);

-- Shared cache for OpenSea's free "instant" API key (main.py's
-- _get_opensea_key). OpenSea allows minting only one such key per hour,
-- total, from this site's traffic - an in-memory-only cache works for a
-- single warm serverless instance, but a real traffic spike spins up
-- several instances in parallel, each starting with an empty cache. This
-- single-row table lets every instance check for (and share) a key
-- another instance already minted, instead of each independently racing
-- to mint their own and getting locked out after the first one succeeds.
create table if not exists opensea_key_cache (
  id          int primary key default 1,
  api_key     text not null,
  expires_at  timestamptz not null,
  updated_at  timestamptz not null default now()
);

drop trigger if exists opensea_key_cache_set_updated_at on opensea_key_cache;
create trigger opensea_key_cache_set_updated_at
  before update on opensea_key_cache
  for each row execute function set_updated_at();

alter table opensea_key_cache enable row level security;

-- Discord bot's /watchlist command: each citizen's watched NFT collections,
-- keyed by their Discord user id (the web portal's watchlist stays purely
-- client-side in localStorage - this is a separate, Discord-only list).
create table if not exists discord_nft_watchlist (
  discord_user_id  text not null,
  slug             text not null,
  added_at         timestamptz not null default now(),
  primary key (discord_user_id, slug)
);

create index if not exists discord_nft_watchlist_user_idx on discord_nft_watchlist (discord_user_id);

alter table discord_nft_watchlist enable row level security;

-- Periodic snapshots of every watchlisted collection's stats, written by the
-- /cron/nft-poll worker. This is the only source of "history" this app has
-- (OpenSea's free tier gives none) - it's what powers the /nft chart, the
-- all-time-high floor, and the supply-cut/volume-spike alert comparisons.
create table if not exists nft_snapshot_history (
  id            bigint generated always as identity primary key,
  slug          text not null,
  chain         text,
  floor         numeric,
  symbol        text,
  volume_1d     numeric,
  sales_1d      int,
  volume_total  numeric,
  owners        int,
  total_supply  int,
  captured_at   timestamptz not null default now()
);

create index if not exists nft_snapshot_history_slug_captured_idx on nft_snapshot_history (slug, captured_at desc);

alter table nft_snapshot_history enable row level security;

-- One row per (collection, alert type) so the poller can compare against the
-- last alerted value and cooldown instead of re-posting every 5-minute cycle
-- for the same ongoing spike.
create table if not exists nft_alert_state (
  slug             text not null,
  alert_type       text not null,  -- 'supply_cut' | 'volume_spike' | 'sweep'
  last_alerted_at  timestamptz,
  last_value       numeric,
  primary key (slug, alert_type)
);

alter table nft_alert_state enable row level security;

-- Newly-discovered collections the mint-radar has already posted about, so
-- the same mint doesn't get re-posted on every poll cycle.
create table if not exists nft_mint_radar_seen (
  slug       text primary key,
  posted_at  timestamptz not null default now()
);

alter table nft_mint_radar_seen enable row level security;

-- /monitor: per-citizen, per-collection, per-event-type opt-in alerts
-- (personal DM pings), separate from the blanket /watchlist alerts
-- channel. event_type shares the same vocabulary as nft_alert_state's
-- alert_type, with 'floor_change' split into directional
-- 'floor_up'/'floor_down', plus 'mint_progress'.
create table if not exists nft_watch_subscriptions (
  discord_user_id  text not null,
  slug             text not null,
  event_type       text not null,  -- 'floor_up' | 'floor_down' | 'supply_cut' | 'mint_progress' | 'sweep' | 'volume_spike'
  created_at       timestamptz not null default now(),
  primary key (discord_user_id, slug, event_type)
);

create index if not exists nft_watch_subscriptions_slug_idx on nft_watch_subscriptions (slug, event_type);

alter table nft_watch_subscriptions enable row level security;

-- /monitor price: personal alert firing when a collection's floor crosses
-- a specific ETH price the citizen picked. direction is computed once at
-- creation time from where the floor sat relative to the target - below
-- means "alert on the way down to/through this price", above means
-- "alert on the way up to/through this price". One-shot by default
-- (deletes itself after firing); loop_alert keeps it alive and re-fires
-- on an hour cooldown for as long as the condition keeps being true,
-- matching the reference tool's "Loop alerts" toggle.
create table if not exists nft_price_alerts (
  discord_user_id  text not null,
  slug             text not null,
  target_price     double precision not null,
  direction        text not null check (direction in ('above', 'below')),
  loop_alert       boolean not null default false,
  last_alerted_at  timestamptz,
  created_at       timestamptz not null default now(),
  primary key (discord_user_id, slug, target_price)
);

create index if not exists nft_price_alerts_slug_idx on nft_price_alerts (slug);

alter table nft_price_alerts enable row level security;

-- Wallets seen buying during a verified, wash-clean burst at the moment
-- NFT Scope called out a collection (any of the fresh/trending/momentum
-- passes) - a permanent record of "who was in early," kept regardless of
-- whether that specific call proved out. This is the raw evidence trail;
-- the nft_smart_wallets view below is what actually gets checked against
-- future candidates.
create table if not exists nft_scope_call_buyers (
  slug           text not null,
  buyer          text not null,
  floor_at_call  numeric,
  called_at      timestamptz not null default now(),
  primary key (slug, buyer)
);

create index if not exists nft_scope_call_buyers_slug_idx on nft_scope_call_buyers (slug);
create index if not exists nft_scope_call_buyers_buyer_idx on nft_scope_call_buyers (buyer);

alter table nft_scope_call_buyers enable row level security;

-- One row per slug once its floor is confirmed to have multiplied
-- meaningfully since NFT Scope first tracked it (see the surge scoring's
-- floor-multiple-since-first-seen). This is what "proves" a call right,
-- kept separate from who bought in so a wallet's track record can be
-- computed by joining the two, not by overwriting a single "best proof"
-- per wallet.
create table if not exists nft_scope_proved_slugs (
  slug        text primary key,
  multiple    numeric not null,
  proved_at   timestamptz not null default now()
);

alter table nft_scope_proved_slugs enable row level security;

-- Powers the embed's "Estimated Target" field: the only honest basis for
-- a forward-looking price estimate is this bot's own resolved track
-- record, not a guess - the spread (p25/median/p75) is shown as a RANGE,
-- deliberately not a single number, so it reads as "here's the historical
-- spread of outcomes," not a promise. _nft_scope_estimated_target in
-- main.py additionally requires a minimum sample size before showing
-- anything at all - a handful of proved slugs isn't a distribution.
create or replace view nft_scope_proved_multiple_stats
  with (security_invoker = true) as
select
  count(*) as sample_size,
  percentile_cont(0.25) within group (order by multiple) as p25_multiple,
  percentile_cont(0.5) within group (order by multiple) as median_multiple,
  percentile_cont(0.75) within group (order by multiple) as p75_multiple
from nft_scope_proved_slugs;

-- Wallet win-rate view, not a table anyone writes to directly - a
-- wallet's smart-money credibility has to be its resolved TRACK RECORD
-- across every call it's been part of, not a single lucky hit and
-- explicitly not wallet balance: a big wallet buying into everything
-- indiscriminately must not outscore a smaller wallet that's actually
-- right more often. Only counts calls at least 14 days old in the
-- denominator - a call NFT Scope made yesterday hasn't had a fair chance
-- to prove out yet, and counting it as a "loss" this early would punish
-- a wallet's most recent (and most relevant) activity the hardest.
--
-- security_invoker = true: without this, a Postgres view defaults to
-- running with its OWNER's privileges rather than the querying user's,
-- which silently bypasses the RLS enabled on the underlying tables -
-- flagged CRITICAL by Supabase's own linter. The backend's service-role
-- key already bypasses RLS regardless (same as every other table here),
-- so this changes nothing for it; what it fixes is that without this,
-- anyone hitting PostgREST with just the public anon key could read
-- every wallet address and win rate straight through the view, RLS or
-- not. Same reasoning applies to every view below.
create or replace view nft_smart_wallets
  with (security_invoker = true) as
select
  b.buyer as address,
  count(distinct b.slug) as total_calls,
  count(distinct p.slug) as proved_calls,
  case when count(distinct b.slug) > 0
    then round(count(distinct p.slug)::numeric / count(distinct b.slug), 3)
    else 0 end as win_rate,
  max(p.multiple) as best_multiple,
  max(p.proved_at) as last_proved_at
from nft_scope_call_buyers b
left join nft_scope_proved_slugs p on p.slug = b.slug
where b.called_at < now() - interval '14 days'
group by b.buyer;

-- The broader, always-on half of wallet tracking: every verified sale
-- event NFT Scope observes while doing its normal work (rapid-activity
-- checks, wash-trade checks - it fetches raw sale events dozens of times
-- per poll cycle regardless) gets logged here too, for free - zero extra
-- OpenSea calls, this only persists data already being fetched. This is
-- what lets a wallet's real, ground-truth trading history get
-- reconstructed across EVERYTHING NFT Scope has ever seen, not just
-- collections it personally called out (that's what nft_smart_wallets
-- above covers - the two are complementary). Primary key is the natural
-- identity of a sale, so re-observing the same event across overlapping
-- fetch windows or later poll cycles just harmlessly overwrites - no
-- unbounded growth from repeat sightings of the same trade.
create table if not exists nft_sale_events_log (
  slug       text not null,
  token_id   text not null,
  buyer      text not null,
  seller     text not null,
  price      numeric,
  symbol     text,
  event_at   timestamptz not null,
  logged_at  timestamptz not null default now(),
  primary key (slug, token_id, buyer, seller, event_at)
);

create index if not exists nft_sale_events_log_buyer_idx on nft_sale_events_log (buyer);
create index if not exists nft_sale_events_log_token_idx on nft_sale_events_log (slug, token_id, event_at);

alter table nft_sale_events_log enable row level security;

-- Ground-truth realized trades: a wallet that bought a specific token and
-- was LATER seen selling that exact same token, matched from the log
-- above - actual profit/loss, not an estimate. A wallet can appear more
-- than once per token if it round-tripped it multiple times; that's
-- intentional, each is a separate realized trade.
--
-- Two precision guards, both aimed at keeping a wallet's win rate
-- honest rather than easy to inflate:
--   - sell.buyer != buy.seller excludes a reciprocal round-trip back to
--     the exact wallet it was bought from (buy from B, sell back to B) -
--     the same back-and-forth wash pattern _analyze_wash_trading already
--     screens for elsewhere, applied here to the realized-PnL data too.
--     Self-trades (same wallet as buyer and seller on one leg) are
--     excluded even earlier, at the logging step itself.
--   - sell.event_at > buy.event_at + a minimum hold guards against
--     counting a same-block/near-instant MEV or bot flip as "conviction" -
--     the user's own framing was buying early and HOLDING for a real
--     move, not microsecond arbitrage. 15 minutes is long enough to
--     exclude that noise, short enough not to exclude a genuine trader
--     spotting and acting on a real mispricing.
create or replace view nft_wallet_realized_trades
  with (security_invoker = true) as
select
  buy.buyer as wallet,
  buy.slug,
  buy.token_id,
  buy.price as buy_price,
  sell.price as sell_price,
  buy.event_at as bought_at,
  sell.event_at as sold_at,
  case when buy.price is not null and buy.price > 0 and sell.price is not null
    then round(((sell.price - buy.price) / buy.price)::numeric, 4)
    else null end as pnl_pct
from nft_sale_events_log buy
join nft_sale_events_log sell
  on sell.slug = buy.slug
  and sell.token_id = buy.token_id
  and sell.seller = buy.buyer
  and sell.buyer != buy.seller
  and sell.event_at > buy.event_at + interval '15 minutes';

-- Per-wallet realized win rate, across every trade this bot has directly
-- observed resolve - buys early, sells for profit, this is the metric
-- that actually captures that, not balance and not a single hit.
create or replace view nft_wallet_pnl_stats
  with (security_invoker = true) as
select
  wallet as address,
  count(*) as total_trades,
  count(*) filter (where pnl_pct > 0) as winning_trades,
  round((count(*) filter (where pnl_pct > 0))::numeric / count(*), 3) as win_rate,
  max(pnl_pct) as best_trade_pct,
  round(avg(pnl_pct)::numeric, 4) as avg_trade_pct
from nft_wallet_realized_trades
where pnl_pct is not null
group by wallet;

-- A wallet's own recent buying pace against its own baseline - a
-- distinct signal from win rate, which needs trades to RESOLVE (sell)
-- before it means anything. This catches a wallet suddenly buying much
-- more, much faster, across ANY collection, RIGHT NOW - including a
-- wallet with no resolved track record yet. Raw counts/volume for both
-- windows, deliberately not a ratio - dividing by a near-zero baseline
-- in SQL gets ugly fast (nulls, div-by-zero), so the spike ratio and its
-- minimum-sample guard are computed application-side instead.
--
-- recent_unique_sellers exists specifically so a spike can't be confused
-- with a wash-trading ring: raw buy COUNT alone can't tell a wallet
-- genuinely buying broadly across the market apart from one cycling
-- trades with a handful of colluding counterparties - a real spike buys
-- from many different sellers, a wash ring buys from the same few over
-- and over. _nft_scope_filter_activity_spikes enforces a minimum
-- distinct-seller ratio against this before ever calling something a
-- spike.
create or replace view nft_wallet_recent_activity
  with (security_invoker = true) as
select
  buyer as address,
  count(*) filter (where event_at > now() - interval '3 days') as recent_buys,
  count(distinct seller) filter (where event_at > now() - interval '3 days') as recent_unique_sellers,
  count(*) filter (where event_at > now() - interval '30 days' and event_at <= now() - interval '3 days') as baseline_buys,
  coalesce(sum(price) filter (where event_at > now() - interval '3 days'), 0) as recent_volume,
  coalesce(sum(price) filter (where event_at > now() - interval '30 days' and event_at <= now() - interval '3 days'), 0) as baseline_volume
from nft_sale_events_log
group by buyer;

-- ── ACO ticketing system ────────────────────────────────────────────────
-- A "drop" is one FCFS/allowlist mint opportunity the team is coordinating
-- wallets for. A "ticket" is one member's wallet submitted toward it - a
-- member can submit as many distinct wallets as they like per drop (an
-- explicit product requirement, not an oversight), so this is NOT one row
-- per member, it's one row per (drop, member, wallet).
create table if not exists aco_drops (
  id                  uuid primary key default gen_random_uuid(),
  title               text not null,
  chain               text not null,
  contract_address    text,
  checker_url         text,
  profit_note         text,
  deadline            timestamptz not null,
  status              text not null default 'open' check (status in ('open', 'resolved', 'cancelled')),
  created_by          text not null,  -- discord id of the admin who created it
  discord_channel_id  text,
  discord_message_id  text,
  created_at          timestamptz not null default now(),
  resolved_at         timestamptz
);

-- The public announcement message only ever carries "Submit Wallet(s)" -
-- See Wallets / Mark Resolved / Cancel Drop live on a second, mirrored
-- message posted to the staff-only ACO support channel instead, so those
-- controls are never even rendered to non-staff members (Discord has no
-- per-viewer component visibility within a single message - channel
-- permissions are the only real enforcement, hence the second message).
alter table aco_drops add column if not exists discord_staff_channel_id text;
alter table aco_drops add column if not exists discord_staff_message_id text;

create index if not exists aco_drops_status_idx on aco_drops (status);

alter table aco_drops enable row level security;

-- Uniqueness is on (drop, member, wallet), not (drop, member) - submitting
-- the exact same wallet twice is a harmless no-op (upserted, not doubled),
-- but two DIFFERENT wallets from the same member for the same drop are
-- both real, intentional tickets.
create table if not exists aco_tickets (
  id              bigint generated always as identity primary key,
  drop_id         uuid not null references aco_drops(id) on delete cascade,
  discord_user_id text not null,
  wallet_address  text not null,
  submitted_at    timestamptz not null default now()
);

create unique index if not exists aco_tickets_unique_wallet_per_drop
  on aco_tickets (drop_id, discord_user_id, wallet_address);
create index if not exists aco_tickets_drop_idx on aco_tickets (drop_id);
create index if not exists aco_tickets_user_idx on aco_tickets (discord_user_id);

alter table aco_tickets enable row level security;

-- One thread per support ticket. thread_id is the Discord private thread
-- created off the ACO channel - closing archives the thread AND flips
-- this row, so /history-style admin review has a real record even after
-- the thread itself is archived and out of sight.
create table if not exists aco_support_tickets (
  id              bigint generated always as identity primary key,
  discord_user_id text not null,
  thread_id       text not null unique,
  status          text not null default 'open' check (status in ('open', 'closed')),
  opened_at       timestamptz not null default now(),
  closed_at       timestamptz,
  closed_by       text
);

create index if not exists aco_support_tickets_status_idx on aco_support_tickets (status);

alter table aco_support_tickets enable row level security;

-- Private-key handoff tracking - deliberately METADATA ONLY. This table
-- (and every line of Python that touches it) never stores, reads, or
-- forwards the key itself - only that a handoff thread exists, who
-- opened it, and its status. The member types the actual key as a plain
-- message inside a private Discord thread; the bot never processes
-- message content there at all, it only creates/deletes the thread and
-- tracks this bookkeeping row. status='expired' is set by the cleanup
-- cron (/cron/aco-key-cleanup) for anything a staff member forgot to
-- close - the thread gets force-deleted either way, so a forgotten
-- handoff can't sit around holding a live key indefinitely.
create table if not exists aco_key_handoffs (
  id              bigint generated always as identity primary key,
  discord_user_id text not null,
  thread_id       text not null unique,
  status          text not null default 'open' check (status in ('open', 'completed', 'expired')),
  opened_at       timestamptz not null default now(),
  completed_at    timestamptz,
  completed_by    text
);

create index if not exists aco_key_handoffs_status_idx on aco_key_handoffs (status);

alter table aco_key_handoffs enable row level security;

-- Rotating educational/rules content, posted automatically on a schedule
-- (see /cron/aco-education) - adding a new guide later is just an insert
-- here, no redeploy needed. `sections` is an array of {heading, body}
-- objects, rendered as one Discord embed per section (a message can
-- carry up to 10). last_posted_at drives the rotation: the cron always
-- picks the least-recently-posted active row, so a brand new post (null
-- last_posted_at) goes out first, then everything cycles evenly.
create table if not exists aco_education_posts (
  id              bigint generated always as identity primary key,
  title           text not null unique,
  emoji           text,
  sections        jsonb not null,
  active          boolean not null default true,
  last_posted_at  timestamptz,
  created_at      timestamptz not null default now()
);

alter table aco_education_posts enable row level security;

insert into aco_education_posts (title, emoji, sections) values
('How To Win Gas Wars', '⛽', $json$[
  {"heading": "What You Need", "body": "A basic understanding of how to read Etherscan. Basic math, or just a calculator. And nerve, because this gets expensive fast."},
  {"heading": "What Is A Gas War", "body": "When a mint is obviously profitable (say a free mint with a 0.2 ETH floor), everyone wants in at the same time. The higher your gas, the sooner your transaction lands in a block, so people keep pushing their gas up to beat each other. That competition is the gas war."},
  {"heading": "Checking The Gas Limit", "body": "Every contract uses a different amount of gas, and it can change by mint type (FCFS vs allowlist vs team mint), so check a past transaction from the SAME contract and SAME mint type before committing to a gas setting. On Etherscan, open a similar transaction, click \"Click to show more,\" and you'll see both the Gas Limit and the actual Gas Usage.\n\nGas Limit is the most you could possibly spend. Gas Usage is what actually gets spent once the transaction goes through, and it's almost always lower than the limit. Always fund your wallet based on the Limit, not the Usage, so you're never caught short.\n\nThe formula: gwei * gas usage / 10^9 = cost in ETH.\n150 gwei at 100K usage = 0.015 ETH\n100 gwei at 120K usage = 0.012 ETH\n200 gwei at 250K usage = 0.05 ETH\n400 gwei at 300K usage = 0.12 ETH"},
  {"heading": "Setting Your Gwei", "body": "Gas has two parts: Max Fee and Priority Fee, usually written as Max/Priority. The Max Fee is always the bigger number. Think of Priority Fee as a tip to the bouncer, the more you tip, the faster you get let in. Max Fee is the total cash you're willing to bring for the night, you won't always spend all of it. If you set 500/100, you'll actually pay somewhere between 100 and 500 gwei depending on network conditions."},
  {"heading": "Building Your Strategy", "body": "Start by budgeting at least 50% of your expected gross profit toward gas. If mint price is 0.03 and floor is 0.1, your gross profit is 0.07, so plan to spend up to half of that.\n\nWatch the remaining supply. A tight supply of 100 to 500 usually means higher gas, since fewer people are willing to compete out loud, but a bigger pool of 1000 to 2000 draws a bigger crowd even at lower gas.\n\nWatch the price. Really high profit mints (0.5 ETH or more) tend to push gas into the thousands of gwei, which scares off casual participants, so setting high can be less contested than you'd think. Cheaper mints (under 0.05 to 0.1 ETH) attract way more competition and it's easy to overpay relative to the actual profit.\n\nConsider the dump risk. If a lot of new supply is about to hit the market from an FCFS round, expect people to sell fast for quick profit, which can push the floor down right after mint.\n\nConsider the fail risk too. A failed transaction still burns gas, usually 20K to 70K in usage, so at 1000 gwei a failed attempt alone can cost 0.02 to 0.07 ETH.\n\nAnd remember bots exist. If you're not using one, you're already behind whoever is, so budget higher gas to make up the difference in speed."},
  {"heading": "Real Examples", "body": "PVP: Floor was around 0.28 ETH before FCFS, free mint price, slow volume (1 to 3 sales every 10 minutes). Gas limit/usage were 260K/175K. Supply left was 85 to 90. I expected the floor might dip to 0.25, worst case 0.2, given the light volume, but I had multiple allowlist spots so even 0.05 profit per mint worked for me. I was ready to spend 0.15 to 0.18 ETH per mint. At 1000 gwei and 175K usage that's 0.175 ETH, so I set Max Fee to 1000 and Priority to 750, and got it.\n\nAOFVerse: Floor was 0.6 ETH before FCFS, free mint price, high demand, decent volume. Gas limit/usage were 170K/110K. Supply left was 100 to 110. People were already panicking about gas hitting 3000 to 5000 gwei. I was ready to spend 0.3 to 0.5 ETH because the demand and volume made me confident it could push higher after mint. At 5000 gwei and 110K usage that's 0.55 ETH, so I set Max Fee to 5000 and Priority to 3500. I got it, and even though some people hit it at 2800 to 3000 gwei and I overpaid slightly, the gap barely mattered against that much profit."},
  {"heading": "One Rule Everyone Follows", "body": "Never ask people what gwei they used, and never share what you used either. It's a mind game. If someone tells you a number and you miss, you'll blame them. If they tell you a number and you overpay, you'll blame them. And if you share your number, the next person just outbids you by a hair. Everyone plays their own hand. Good luck out there."}
]$json$::jsonb),
('DASH ACO: Rules & How It Works', '🎟️', $json$[
  {"heading": "What Is DASH ACO", "body": "DASH ACO is our minting service for high-demand or difficult drops. Instead of fighting the gas war and bot competition alone, our team runs advanced mint bots on your behalf to maximize your odds of landing an allocation."},
  {"heading": "Supported Chains", "body": "We cover most EVM chains, Solana, and Bitcoin-based drops."},
  {"heading": "How Botting Works", "body": "You provide a burner wallet's private key through #aco-help so our bots can mint for you. Always use a dedicated burner wallet, never your main wallet, this is non-negotiable for your own security.\n\nIf a gas war is likely, we'll walk you through recommended gas settings before the mint."},
  {"heading": "Fees", "body": "DASH ACO charges a service fee of 20% to 25% of your net profit, whether you hold or sell. The exact rate depends on the drop and gets confirmed with you upfront before we bot it, so there's never a surprise number after the fact.\n\nIf you sell within 1 to 2 hours of minting, the fee is calculated on your actual realized sale profit.\n\nIf you choose to hold, the fee is calculated against a floor price agreed on together at the time of the mint, giving both sides a fair, transparent baseline."},
  {"heading": "No Guarantees", "body": "We do everything we can to land a successful mint, but nothing is guaranteed. Bots can fail, projects can behave unpredictably, and gas can be spent on an attempt that doesn't land. We are not responsible for gas lost on a failed attempt. Using a bot significantly improves your odds, it does not guarantee them."},
  {"heading": "Requesting A Bot", "body": "Drop everything we need in #aco-request at least 6 hours before the mint: project socials, mint date and time, price, supply, and anything else relevant. The earlier we know, the better we can prepare."}
]$json$::jsonb)
-- do UPDATE, not do nothing: this file is the source of truth for guide
-- content, so re-running it after an edit (like the fee change above)
-- actually syncs the live row instead of silently no-op'ing forever.
-- Only sections/emoji are overwritten - active and last_posted_at are
-- runtime state, not seed content, and must survive a re-run untouched.
on conflict (title) do update set sections = excluded.sections, emoji = excluded.emoji;

