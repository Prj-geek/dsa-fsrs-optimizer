"""
Pulls every row from the "Review Log" Notion database, fits personalized
FSRS-5 weights (w0-w18) from that review history, and writes the fitted
weights back into the single row of the "FSRS Config" database.

Requires:
    pip install -r requirements.txt

Environment variables (set these as GitHub Actions repo secrets, or export
them locally when running manually):
    NOTION_TOKEN                    Internal integration token (starts with "ntn_" or "secret_")
    REVIEW_LOG_DATA_SOURCE_ID       Data source id of the "Review Log" database
    FSRS_CONFIG_PAGE_ID             Page id of the single row in "FSRS Config"

Optional:
    MIN_REVIEWS                     Minimum review events before fitting (default 50)

Run locally:
    NOTION_TOKEN=... REVIEW_LOG_DATA_SOURCE_ID=... FSRS_CONFIG_PAGE_ID=... \
        python optimize_fsrs.py
"""

import os
import sys
from datetime import datetime, timezone

import requests
from fsrs import Optimizer, Rating, ReviewLog

NOTION_VERSION = "2025-09-03"
NOTION_API = "https://api.notion.com/v1"

RATING_MAP = {
    "Again": Rating.Again,
    "Hard": Rating.Hard,
    "Good": Rating.Good,
    "Easy": Rating.Easy,
}


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


def row_to_review_log(row: dict, card_id_map: dict) -> ReviewLog | None:
    """Convert one Notion 'Review Log' page into an fsrs.ReviewLog."""
    props = row["properties"]

    problem_relation = props.get("Problem", {}).get("relation", [])
    if not problem_relation:
        return None  # unlinked log row, skip
    problem_page_id = problem_relation[0]["id"]

    rating_select = props.get("Rating", {}).get("select")
    if not rating_select:
        return None
    rating = RATING_MAP.get(rating_select["name"])
    if rating is None:
        return None

    date_prop = props.get("Date of Review", {}).get("date")
    if not date_prop or not date_prop.get("start"):
        return None
    review_dt = datetime.fromisoformat(date_prop["start"])
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
    token = env("NOTION_TOKEN")
    review_log_ds_id = env("REVIEW_LOG_DATA_SOURCE_ID")
    config_page_id = env("FSRS_CONFIG_PAGE_ID")
    min_reviews = int(env("MIN_REVIEWS", required=False, default="50"))

    headers = notion_headers(token)

    print("Fetching Review Log rows from Notion...")
    rows = fetch_all_review_log_rows(review_log_ds_id, headers)
    print(f"Found {len(rows)} review log rows.")

    card_id_map: dict = {}
    review_logs = []
    for row in rows:
        rl = row_to_review_log(row, card_id_map)
        if rl is not None:
            review_logs.append(rl)

    print(f"Parsed {len(review_logs)} usable review events across {len(card_id_map)} problems.")

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

    write_weights_to_config(config_page_id, headers, optimal_parameters, len(review_logs))
    print("Wrote new weights to FSRS Config in Notion.")


if __name__ == "__main__":
    main()
