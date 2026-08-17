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

