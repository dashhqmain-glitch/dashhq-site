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
  x_profile           text not null,
  intro               text not null,
  communities         text not null,
  value               text not null,
  followed_team       boolean not null default false,
  status              text not null default 'pending', -- pending | accepted | declined
  reviewed_by         text,
  reviewed_at         timestamptz,
  discord_channel_id  text,
  discord_message_id  text,
  submitted_at        timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create index if not exists applications_status_idx on applications (status);

drop trigger if exists applications_set_updated_at on applications;
create trigger applications_set_updated_at
  before update on applications
  for each row execute function set_updated_at();

alter table applications enable row level security;

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
