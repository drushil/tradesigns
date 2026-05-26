"""Read the latest local daily review snapshot without network access."""

from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    path = Path("artifacts/daily_reviews/latest.json")
    if not path.exists():
        print(json.dumps({"ok": False, "error": "latest snapshot not found", "path": str(path)}, indent=2))
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
