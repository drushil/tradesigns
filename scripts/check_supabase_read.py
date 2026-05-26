"""Read-only Supabase connectivity check for Codex sandbox escalation."""

from __future__ import annotations

import json

from dotenv import load_dotenv

load_dotenv()

from database.client import get_daily_reviews


def main() -> None:
    reviews = get_daily_reviews(limit=3)
    payload = {
        "ok": True,
        "count": len(reviews),
        "reviews": [
            {
                "review_date": row.get("review_date"),
                "status": row.get("status"),
                "summary": (row.get("summary") or "")[:160],
            }
            for row in reviews
        ],
    }
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
