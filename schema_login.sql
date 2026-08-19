-- Run this in the Supabase SQL Editor. Adds a single shared login credential
-- row: one account number, two bcrypt password hashes (admin vs investor).
-- No RLS policies are defined, so anon/authenticated clients can't read this
-- table at all — only the backend (service_role key) can, which is what we
-- want since this holds password hashes.

create table if not exists login_credentials (
  id int primary key default 1,
  account_number text not null,
  admin_password_hash text not null,
  investor_password_hash text not null,
  constraint singleton check (id = 1)
);

alter table login_credentials enable row level security;
-- Intentionally no policies — only service_role bypasses RLS.
