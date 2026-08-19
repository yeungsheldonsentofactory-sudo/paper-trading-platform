-- Run this once in the Supabase SQL Editor (Project -> SQL Editor -> New query).
-- Single shared fund: one admin trades it, everyone else (role='investor') can
-- only read. All writes go through the FastAPI backend using the service_role
-- key, so RLS below only needs to allow SELECT for logged-in users — there are
-- no INSERT/UPDATE/DELETE policies, which means the anon/authenticated roles
-- can never write directly from the browser even if someone tries.

-- 1. profiles: one row per Supabase Auth user, holds the admin/investor role.
create table if not exists profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null,
  role text not null default 'investor' check (role in ('admin', 'investor')),
  created_at timestamptz not null default now()
);

alter table profiles enable row level security;

create policy "profiles: users can read their own row"
  on profiles for select
  using (auth.uid() = id);

-- Auto-create a profile (default role investor) whenever someone signs up.
create or replace function handle_new_user()
returns trigger as $$
begin
  insert into public.profiles (id, email) values (new.id, new.email);
  return new;
end;
$$ language plpgsql security definer;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();

-- 2. fund_account: singleton row holding the shared portfolio's cash + leverage.
create table if not exists fund_account (
  id int primary key default 1,
  balance numeric not null default 100000,
  leverage numeric not null default 100,
  created_at timestamptz not null default now(),
  constraint singleton check (id = 1)
);
insert into fund_account (id) values (1) on conflict (id) do nothing;

alter table fund_account enable row level security;
create policy "fund_account: readable by any authenticated user"
  on fund_account for select
  using (auth.role() = 'authenticated');

-- Shared ticket sequence: positions, pending orders and history all draw
-- from the same counter so ticket numbers stay globally unique, matching
-- how a pending order becomes a position under a new ticket when triggered.
create sequence if not exists ticket_seq;

-- 3. positions: currently open trades.
create table if not exists positions (
  ticket bigint primary key default nextval('ticket_seq'),
  symbol text not null,
  side text not null check (side in ('buy', 'sell')),
  qty numeric not null,
  entry_price numeric not null,
  sl numeric,
  tp numeric,
  open_time timestamptz not null default now()
);
alter table positions enable row level security;
create policy "positions: readable by any authenticated user"
  on positions for select using (auth.role() = 'authenticated');

-- 4. pending_orders: limit/stop orders waiting to trigger.
create table if not exists pending_orders (
  ticket bigint primary key default nextval('ticket_seq'),
  symbol text not null,
  order_type text not null check (order_type in ('buy_limit', 'sell_limit', 'buy_stop', 'sell_stop')),
  qty numeric not null,
  trigger_price numeric not null,
  sl numeric,
  tp numeric,
  placed_time timestamptz not null default now()
);
alter table pending_orders enable row level security;
create policy "pending_orders: readable by any authenticated user"
  on pending_orders for select using (auth.role() = 'authenticated');

-- 5. trade_history: closed (or partially closed) trades.
create table if not exists trade_history (
  id bigint generated always as identity primary key,
  ticket bigint not null,
  symbol text not null,
  side text not null,
  qty numeric not null,
  entry_price numeric not null,
  close_price numeric not null,
  open_time timestamptz not null,
  close_time timestamptz not null default now(),
  pnl numeric not null,
  reason text not null check (reason in ('manual', 'sl', 'tp', 'stop_out'))
);
alter table trade_history enable row level security;
create policy "trade_history: readable by any authenticated user"
  on trade_history for select using (auth.role() = 'authenticated');

-- 6. journal: human-readable activity log.
create table if not exists journal (
  id bigint generated always as identity primary key,
  time timestamptz not null default now(),
  message text not null
);
alter table journal enable row level security;
create policy "journal: readable by any authenticated user"
  on journal for select using (auth.role() = 'authenticated');

-- After running this, sign up through the app once with the account that
-- should be admin, then in Table Editor -> profiles, change that row's
-- role from 'investor' to 'admin'. Everyone else stays read-only.
