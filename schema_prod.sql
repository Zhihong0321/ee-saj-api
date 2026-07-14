-- SAJ inverter fleet -> prod_main. ALL tables prefixed `saj_`. No FKs into customer tables.

create table if not exists saj_plant (
  plant_uid      text primary key,
  plant_name     text,
  owner_name     text,
  installer_name text,
  pv_power_wp    numeric,
  running_state  text,
  type_name      text,
  updated_at     timestamptz default now()
);

create table if not exists saj_device (
  device_sn   text primary key,
  plant_uid   text,
  device_type text,
  alias       text,
  last_seen   timestamptz,
  updated_at  timestamptz default now()
);

create table if not exists saj_customer_device_map (
  id           bigserial primary key,
  customer_id  text not null,
  device_sn    text not null,
  plant_uid    text,
  match_method text,           -- 'name_exact' | 'manual' | 'serial' ...
  confidence   numeric,        -- 0..1
  verified     boolean default false,
  created_at   timestamptz default now(),
  unique (customer_id, device_sn)
);

create table if not exists saj_reading (
  device_sn   text        not null,
  ts          timestamptz not null,
  ac_power_w  numeric,
  pv_power_w  numeric,
  today_kwh   numeric,
  month_kwh   numeric,
  year_kwh    numeric,
  total_kwh   numeric,
  device_temp numeric,
  raw         jsonb,
  primary key (device_sn, ts)
);

create index if not exists saj_reading_ts_idx on saj_reading (ts);
create index if not exists saj_map_customer_idx on saj_customer_device_map (customer_id);
