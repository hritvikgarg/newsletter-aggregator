"""
Phase 3 — Fill expected_next_check for all subscribed rows.

Formula: date_subscribed + frequency_days + 2 buffer
  daily     → +3
  weekly    → +9
  biweekly  → +16
  3x-weekly → +5  (every ~2.3 days, +2 buffer)
  monthly   → +32

Run: python scripts/phase3_next_check.py
"""

import csv
from datetime import date, timedelta
from pathlib import Path

BASE_DIR    = Path(__file__).parent.parent
SOURCES_CSV = BASE_DIR / "data" / "sources.csv"

SOURCES_FIELDS = [
    "segment", "newsletter_name", "publisher", "signup_url",
    "subscriber_estimate", "send_frequency", "double_optin", "description",
    "pick_type", "kt_email", "kt_feed_url", "status", "status_detail",
    "date_subscribed", "expected_next_check",
]

FREQ_DAYS = {
    "daily":     3,
    "weekly":    9,
    "biweekly":  16,
    "3x-weekly": 5,
    "monthly":   32,
}


def calc_next_check(subscribed_str: str, frequency: str) -> str:
    try:
        subscribed = date.fromisoformat(subscribed_str)
    except Exception:
        return ""
    days = FREQ_DAYS.get(frequency.lower().strip(), 9)  # default weekly
    return (subscribed + timedelta(days=days)).isoformat()


def main():
    with open(SOURCES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    updated = 0
    for row in rows:
        if row.get("date_subscribed") and not row.get("expected_next_check"):
            row["expected_next_check"] = calc_next_check(
                row["date_subscribed"], row.get("send_frequency", "weekly")
            )
            updated += 1

    with open(SOURCES_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SOURCES_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Updated expected_next_check for {updated} rows.")


if __name__ == "__main__":
    main()
