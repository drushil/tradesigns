-- Persist intraday hold-deadline extension counters for scheduled runs.

alter table if exists trades
    add column if not exists hold_extension_count smallint default 0;

alter table if exists open_trades
    add column if not exists hold_extension_count smallint default 0;
