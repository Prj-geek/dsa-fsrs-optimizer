"""
Pulls every row from the "Review Log" Notion database, fits personalized
FSRS-5 weights (w0-w18) from that review history, and writes the fitted
weights back into the single row of the "FSRS Config" database.

Requires:
    pip install -r requirements.txt

Environment variables (set these as GitHub Actions repo secrets, or put them
in a local .env file next to this script, or export them in your shell):
    NOTION_TOKEN                    Internal integration token (starts with "ntn_" or "secret_")
    REVIEW_LOG_DATA_SOURCE_ID       Data source id of the "Review Log" database
    FSRS_CONFIG_PAGE_ID             Page id of the single row in "FSRS Config"

Optional:
    MIN_REVIEWS                     Minimum review events before fitting (default 50)
    DRY_RUN                         Set to 1 to fit but NOT write weights to Notion

Run locally:
    python optimize_fsrs.py          (reads .env automatically if present)
"""

import os
import sys
from collections import Counter
from datetime import datetime, timezone

import requests
from fsrs import Optimizer, Rating, ReviewLog

NOTION_VERSION = "2025-09-03"
NOTION_API = "https://api.notion.com/v1"

RATING_MAP = {
    "again": Rating.Again,
    "hard": Rating.Hard,
    "good": Rating.Good,
    "easy": Rating.Easy,
}

# Why rows got skipped -- printed at the end of parsing.
drop_reasons: Counter = Counter()


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader (no extra dependency). Does not override real env vars."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def env(name: str, required: bool = True, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return value


def notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def fetch_all_review_log_rows(data_source_id: str, headers: dict) -> list[dict]:
    """Paginate through every row in the Review Log data source."""
    rows = []
    payload = {"page_size": 100}
    url = f"{NOTION_API}/data_sources/{data_source_id}/query"
    while True:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        rows.extend(data["results"])
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return rows


def find_prop(props: dict, *names: str) -> dict:
    """Case/whitespace-insensitive property lookup. Returns {} if not found."""
    lookup = {k.strip().lower(): v for k, v in props.items()}
    for name in names:
        hit = lookup.get(name.strip().lower())
        if hit is not None:
            return hit
    return {}


def prop_text(prop: dict) -> str | None:
    """Read a text-ish value from select / status / rich_text / title / formula."""
    t = prop.get("type")
    if t in ("select", "status"):
        v = prop.get(t)
        return v["name"] if v else None
    if t in ("rich_text", "title"):
        return "".join(x.get("plain_text", "") for x in prop.get(t, [])) or None
    if t == "formula":
        return prop.get("formula", {}).get("string")
    return None


def parse_rating(label: str | None) -> Rating | None:
    if not label:
        return None
    clean = label.strip().lower()
    if clean in RATING_MAP:
        return RATING_MAP[clean]
    # tolerate things like "🟢 Good" or "Good (3)"
    for key, rating in RATING_MAP.items():
        if key in clean:
            return rating
    return None


def row_to_review_log(row: dict, card_id_map: dict) -> ReviewLog | None:
    """Convert one Notion 'Review Log' page into an fsrs.ReviewLog."""
    props = row["properties"]

    problem_relation = find_prop(props, "Problem").get("relation", [])
    if not problem_relation:
        drop_reasons["no Problem relation"] += 1
        return None
    problem_page_id = problem_relation[0]["id"]

    rating_prop = find_prop(props, "Rating")
    rating_label = prop_text(rating_prop)
    if not rating_label:
        drop_reasons[f"Rating empty or unsupported type ({rating_prop.get('type')})"] += 1
        return None
    rating = parse_rating(rating_label)
    if rating is None:
        drop_reasons[f"unmapped rating: {rating_label!r}"] += 1
        return None

    date_prop = find_prop(props, "Date of Review", "Date")
    if date_prop.get("type") == "created_time":
        start = date_prop.get("created_time")
    else:
        start = (date_prop.get("date") or {}).get("start")
    if not start:
        drop_reasons["no Date of Review"] += 1
        return None
    review_dt = datetime.fromisoformat(start)
    if review_dt.tzinfo is None:
        review_dt = review_dt.replace(tzinfo=timezone.utc)

    # fsrs.ReviewLog.card_id must be an int; Notion page ids are UUID
    # strings, so map each problem page id to a stable incrementing int.
    if problem_page_id not in card_id_map:
        card_id_map[problem_page_id] = len(card_id_map) + 1
    card_id = card_id_map[problem_page_id]

    return ReviewLog(
        card_id=card_id,
        rating=rating,
        review_datetime=review_dt,
        review_duration=None,
    )


def write_weights_to_config(
    config_page_id: str, headers: dict, weights: tuple, review_count: int
) -> None:
    properties = {f"w{i}": {"number": float(w)} for i, w in enumerate(weights)}
    properties["Last Optimizer Run"] = {
        "date": {"start": datetime.now(timezone.utc).date().isoformat()}
    }
    properties["Review Count At Last Fit"] = {"number": review_count}

    url = f"{NOTION_API}/pages/{config_page_id}"
    resp = requests.patch(
        url, headers=headers, json={"properties": properties}, timeout=30
    )
    resp.raise_for_status()


def main() -> None:
    load_dotenv()

    token = env("NOTION_TOKEN")
    review_log_ds_id = env("REVIEW_LOG_DATA_SOURCE_ID")
    config_page_id = env("FSRS_CONFIG_PAGE_ID")
    min_reviews = int(env("MIN_REVIEWS", required=False, default="50"))
    dry_run = env("DRY_RUN", required=False, default="0") == "1"

    headers = notion_headers(token)

    print("Fetching Review Log rows from Notion...")
    rows = fetch_all_review_log_rows(review_log_ds_id, headers)
    print(f"Found {len(rows)} review log rows.")

    if rows:
        print("Property names/types on first row:")
        for name, prop in rows[0]["properties"].items():
            print(f"  {name!r}: {prop.get('type')}")

    card_id_map: dict = {}
    review_logs = []
    for row in rows:
        rl = row_to_review_log(row, card_id_map)
        if rl is not None:
            review_logs.append(rl)

    review_logs.sort(key=lambda r: r.review_datetime)

    print(f"Parsed {len(review_logs)} usable review events across {len(card_id_map)} problems.")
    if drop_reasons:
        print("Dropped rows:", dict(drop_reasons))

    if len(review_logs) < min_reviews:
        print(
            f"Only {len(review_logs)} review events logged (minimum {min_reviews}). "
            "Skipping fit for now -- log more reviews and re-run later. "
            "This is not an error; early runs are expected to skip.",
        )
        return

    print("Fitting FSRS-5 weights (this can take a minute)...")
    optimizer = Optimizer(review_logs)
    optimal_parameters = optimizer.compute_optimal_parameters()

    if len(optimal_parameters) != 19:
        print(
            f"Warning: optimizer returned {len(optimal_parameters)} parameters, "
            "expected 19 (FSRS-5). Check your installed 'fsrs' package version -- "
            "this project pins fsrs==5.1.3 on purpose. Aborting write to avoid "
            "corrupting the Notion Config row.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("New weights:", optimal_parameters)

    if dry_run:
        print("DRY_RUN=1 -- not writing to Notion.")
        return

    write_weights_to_config(config_page_id, headers, optimal_parameters, len(review_logs))
    print("Wrote new weights to FSRS Config in Notion.")


if __name__ == "__main__":
    main()
