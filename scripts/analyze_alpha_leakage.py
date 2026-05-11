"""
scripts/analyze_alpha_leakage.py
Step 0: Identify which block stages and reasons are silently killing alpha.

Queries blocked_opportunities with replay data, groups by block_stage/block_reason,
and reports which blocks had high max_favorable_pct (would have been profitable).

Usage:
    python scripts/analyze_alpha_leakage.py [--days 30] [--min-favorable 0.5]
"""
import os
import sys
import argparse
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from database.client import get_client


def fetch_replayed_blocks(days: int = 30) -> list[dict]:
    client = get_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = (
        client.table("blocked_opportunities")
        .select(
            "id,ticker,action_hint,composite_score,block_stage,block_reason,"
            "candidate_rank_score,ev_net_pct,max_favorable_pct,max_adverse_pct,"
            "close_after_pct,created_at,setup_grade,a_plus_blocked"
        )
        .gte("created_at", cutoff)
        .not_.is_("max_favorable_pct", "null")
        .execute()
    )
    return result.data or []


def analyze(rows: list[dict], min_favorable: float = 0.5) -> None:
    if not rows:
        print("No replayed blocked opportunities found.")
        return

    # Group by block_stage → block_reason
    by_stage: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        stage = r.get("block_stage") or "unknown"
        reason = r.get("block_reason") or "unknown"
        by_stage[stage][reason].append(r)

    total = len(rows)
    missed = [r for r in rows if float(r.get("max_favorable_pct") or 0) >= min_favorable]

    print(f"\n{'='*70}")
    print(f"Alpha Leakage Analysis — {total} replayed blocks")
    print(f"Profitable if-taken (≥{min_favorable}% favorable): {len(missed)} ({len(missed)/total:.0%})")
    print(f"{'='*70}\n")

    # Summary table per stage
    stage_order = ["gate", "ev", "llm", "conviction", "ranking", "unknown"]
    all_stages = sorted(by_stage.keys(), key=lambda s: stage_order.index(s) if s in stage_order else 99)

    for stage in all_stages:
        reasons = by_stage[stage]
        stage_rows = [r for rlist in reasons.values() for r in rlist]
        stage_missed = [r for r in stage_rows if float(r.get("max_favorable_pct") or 0) >= min_favorable]
        print(f"  [{stage.upper()}]  {len(stage_rows)} blocks  |  {len(stage_missed)} profitable-if-taken ({len(stage_missed)/len(stage_rows):.0%})")

        # Top reasons within stage
        sorted_reasons = sorted(
            reasons.items(),
            key=lambda kv: sum(1 for r in kv[1] if float(r.get("max_favorable_pct") or 0) >= min_favorable),
            reverse=True,
        )
        for reason, reason_rows in sorted_reasons[:8]:
            hit = [r for r in reason_rows if float(r.get("max_favorable_pct") or 0) >= min_favorable]
            if not hit:
                continue
            avg_fav = sum(float(r.get("max_favorable_pct") or 0) for r in hit) / len(hit)
            avg_adv = sum(float(r.get("max_adverse_pct") or 0) for r in reason_rows) / len(reason_rows)
            tickers = sorted({r.get("ticker", "") for r in hit})[:6]
            print(f"    {reason[:55]:<55}  {len(hit):>3} hits  avg_fav={avg_fav:+.2f}%  avg_adv={avg_adv:+.2f}%")
            print(f"      tickers: {', '.join(tickers)}")
        print()

    # A+ specific report
    a_plus = [r for r in rows if r.get("a_plus_blocked") or r.get("setup_grade") == "A+"]
    if a_plus:
        a_plus_missed = [r for r in a_plus if float(r.get("max_favorable_pct") or 0) >= min_favorable]
        print(f"  A+ BLOCKS: {len(a_plus)} total  |  {len(a_plus_missed)} profitable-if-taken")
        for r in sorted(a_plus_missed, key=lambda x: float(x.get("max_favorable_pct") or 0), reverse=True)[:10]:
            print(f"    {r.get('ticker'):>6}  {r.get('block_stage')}/{r.get('block_reason')[:40]}  "
                  f"fav={float(r.get('max_favorable_pct') or 0):+.2f}%  "
                  f"created={str(r.get('created_at'))[:16]}")
        print()

    # Top individual missed opportunities
    print(f"  TOP 15 MISSED (by max_favorable_pct ≥{min_favorable}%):")
    top_missed = sorted(missed, key=lambda r: float(r.get("max_favorable_pct") or 0), reverse=True)[:15]
    for r in top_missed:
        print(
            f"    {r.get('ticker'):>6}  {r.get('block_stage'):>10}/{r.get('block_reason', '')[:35]:<35}  "
            f"fav={float(r.get('max_favorable_pct') or 0):+.2f}%  "
            f"adv={float(r.get('max_adverse_pct') or 0):+.2f}%  "
            f"composite={float(r.get('composite_score') or 0):+.3f}"
        )
    print()


def main():
    parser = argparse.ArgumentParser(description="Analyze alpha leakage from blocked trade opportunities.")
    parser.add_argument("--days", type=int, default=30, help="Look-back window in days (default 30)")
    parser.add_argument("--min-favorable", type=float, default=0.5,
                        help="Min max_favorable_pct to count as 'missed' (default 0.5%%)")
    args = parser.parse_args()

    print(f"Fetching blocked opportunities from last {args.days} days...")
    rows = fetch_replayed_blocks(days=args.days)
    analyze(rows, min_favorable=args.min_favorable)


if __name__ == "__main__":
    main()
