from datetime import datetime, timezone

import pandas as pd

from backend.premarket import radar


def _bars():
    idx = pd.to_datetime([
        "2026-06-01 19:55:00+00:00",
        "2026-06-01 20:00:00+00:00",
        "2026-06-02 08:00:00+00:00",
        "2026-06-02 08:01:00+00:00",
        "2026-06-02 12:01:00+00:00",
        "2026-06-02 12:02:00+00:00",
    ])
    return pd.DataFrame(
        {
            "Open": [99, 100, 101, 102, 104, 105],
            "High": [100, 100, 102, 103, 105, 106],
            "Low": [99, 100, 100, 101, 103, 104],
            "Close": [100, 100, 101, 102, 104, 105],
            "Volume": [1000, 1000, 10000, 10000, 80000, 90000],
        },
        index=idx,
    )


def test_session_window_uses_new_york_time():
    assert radar.session_window(datetime(2026, 6, 2, 12, 30, tzinfo=timezone.utc)) == "primary_premarket"
    assert radar.session_window(datetime(2026, 6, 2, 13, 31, tzinfo=timezone.utc)) == "opening_confirmation"
    assert radar.session_window(datetime(2026, 6, 7, 22, 30, tzinfo=timezone.utc)) == "sunday_futures_watch"


def test_build_gap_features_from_extended_bars(monkeypatch):
    monkeypatch.setattr(radar, "_latest_quote_spread_pct", lambda ticker: 0.2)
    import backend.signals.engine as engine

    monkeypatch.setattr(
        engine,
        "news_sentiment_score",
        lambda *args, **kwargs: (0.5, {"latest_headline": "NVDA raises guidance"}),
    )

    features = radar.build_gap_features(
        "NVDA",
        bars=_bars(),
        now=datetime(2026, 6, 2, 12, 5, tzinfo=timezone.utc),
        earnings_context={"blocked": False},
    )

    assert features is not None
    assert features.gap_pct == 5.0
    assert features.premarket_high == 106
    assert features.premarket_low == 100
    assert features.premarket_volume == 190000
    assert features.catalyst_label == "company_news"


def test_classify_gap_continuation_watch(monkeypatch):
    monkeypatch.setenv("PREMARKET_MIN_VOLUME", "50000")
    features = radar.GapFeatures(
        ticker="NVDA",
        last_price=105,
        prior_close=100,
        gap_pct=5.0,
        premarket_high=106,
        premarket_low=100,
        premarket_vwap=104,
        premarket_volume=180000,
        premarket_rvol=2.0,
        spread_pct=0.2,
        news_score=0.5,
        catalyst_label="company_news",
        latest_headline="NVDA raises guidance",
        earnings_context={},
        data_quality={},
    )

    result = radar.classify_gap(features)

    assert result["classification"] == "gap_continuation_watch"
    assert "ORB confirmation" in result["opening_plan"]


def test_classify_gap_blocks_wide_spread(monkeypatch):
    monkeypatch.setenv("PREMARKET_MAX_SPREAD_PCT", "0.75")
    features = radar.GapFeatures(
        ticker="AMD",
        last_price=103,
        prior_close=100,
        gap_pct=3.0,
        premarket_high=104,
        premarket_low=102,
        premarket_vwap=103,
        premarket_volume=200000,
        premarket_rvol=3.0,
        spread_pct=1.2,
        news_score=0.0,
        catalyst_label="unknown",
        latest_headline="",
        earnings_context={},
        data_quality={},
    )

    result = radar.classify_gap(features)

    assert result["classification"] == "ignore_wide_spread"
