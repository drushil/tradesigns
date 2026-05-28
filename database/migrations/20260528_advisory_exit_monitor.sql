-- Lightweight manual advisory exit monitoring.
-- Keeps advisory_signals as the alert log while allowing entered rows to emit
-- one-shot T1/T2/stop/time-window recommendation alerts.

alter table if exists advisory_signals
    add column if not exists t1_alerted        boolean     default false,
    add column if not exists exit_alert_type   text,
    add column if not exists exit_alerted_at   timestamptz,
    add column if not exists exit_monitor_json jsonb       default '{}'::jsonb;

create index if not exists idx_advisory_signals_entered_open
    on advisory_signals (created_at desc)
    where entry_triggered = true and status = 'entered';
