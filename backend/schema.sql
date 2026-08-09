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

create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

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

