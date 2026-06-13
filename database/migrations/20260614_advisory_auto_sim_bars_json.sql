-- Store the fill->session-close 1m bar path per sim so the replay harness can
-- re-run exit logic with different parameters long after yfinance drops the
-- intraday history (~1-2 trading days). Captured once, after session close, by
-- _capture_bar_paths_eod() in backend/advisory_auto/simulator.py.

alter table advisory_auto_simulations
    add column if not exists bars_json jsonb;

comment on column advisory_auto_simulations.bars_json is
    'Compact fill->session-close 1m bar path {date, bars:[[HH:MM, low, high, close]]} for offline replay calibration.';
