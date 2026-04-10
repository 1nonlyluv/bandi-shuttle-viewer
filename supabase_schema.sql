create table if not exists schedule_uploads (
  id uuid primary key default gen_random_uuid(),
  file_name text not null,
  storage_path text not null,
  month_key text not null,
  uploaded_by text,
  created_at timestamptz not null default now()
);

create table if not exists schedule_days (
  id uuid primary key default gen_random_uuid(),
  date_key text not null unique,
  month_key text not null,
  sheet_name text,
  source_file_name text,
  source_upload_id uuid references schedule_uploads(id) on delete set null,
  schedule_json jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists schedule_overrides (
  id uuid primary key default gen_random_uuid(),
  date_key text not null,
  override_key text not null,
  payload jsonb not null,
  updated_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (date_key, override_key)
);

create index if not exists schedule_overrides_date_key_idx
  on schedule_overrides(date_key);

create or replace function set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists schedule_days_set_updated_at on schedule_days;
create trigger schedule_days_set_updated_at
before update on schedule_days
for each row execute function set_updated_at();

drop trigger if exists schedule_overrides_set_updated_at on schedule_overrides;
create trigger schedule_overrides_set_updated_at
before update on schedule_overrides
for each row execute function set_updated_at();
