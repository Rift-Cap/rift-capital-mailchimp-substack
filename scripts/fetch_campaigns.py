#!/usr/bin/env python3
"""
Step 1 of the Mailchimp -> Substack mirror.

Looks up recently *sent* Mailchimp campaigns, skips anything already recorded
in state/processed_campaigns.json, and writes the raw campaign data (id,
subject, send_time, html) for anything new to a temporary JSON file that
Step 2 (transform_content.py) consumes in the same job run.

Never prints the API key, and exits non-zero on any Mailchimp API failure or
unexpected response shape rather than writing a partial/malformed result.
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import requests

STATE_FILE = os.path.join("state", "processed_campaigns.json")
# The workflow runs weekly; look back further than one week so a delayed or
# missed run (a skipped Action, a Mailchimp outage, etc.) still catches
# every campaign sent since the last successful run. Already-processed
# campaigns are filtered out via state/processed_campaigns.json regardless,
# so a wider window costs nothing but a slightly larger API response.
LOOKBACK_DAYS = 10
REQUEST_TIMEOUT = 30

# Where Step 2 will look for this script's output. Kept out of the repo
# (never committed) and shared with the next step via GITHUB_ENV so the two
# processes agree on the exact path within the same job.
NEW_CAMPAIGNS_PATH = os.path.join(tempfile.gettempdir(), "mirror_new_campaigns.json")


def log(message: str) -> None:
    print(f"[fetch_campaigns] {message}", flush=True)


def fail(message: str) -> None:
    print(f"[fetch_campaigns] ERROR: {message}", file=sys.stderr, flush=True)
    sys.exit(1)


def api_base_url(api_key: str) -> str:
    """Mailchimp API keys are suffixed with their datacenter, e.g. abc123-us21."""
    if "-" not in api_key:
        fail("MAILCHIMP_API_KEY does not contain a datacenter suffix (expected format ...-usNN)")
    datacenter = api_key.rsplit("-", 1)[-1]
    if not re.fullmatch(r"[a-z]{2,4}\d{1,3}", datacenter):
        fail(f"Could not parse a valid datacenter suffix from MAILCHIMP_API_KEY (got '{datacenter}')")
    return f"https://{datacenter}.api.mailchimp.com/3.0"


def load_processed_ids(path: str) -> set:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            records = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        fail(f"Could not read/parse {path}: {exc}")
        return set()  # unreachable, keeps type checkers happy

    if not isinstance(records, list):
        fail(f"{path} did not contain a JSON array as expected")

    return {rec.get("campaign_id") for rec in records if isinstance(rec, dict)}


def mailchimp_get(session: requests.Session, base_url: str, path: str, params: dict | None = None) -> dict:
    url = f"{base_url}{path}"
    try:
        resp = session.get(url, params=params or {}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        fail(f"Request to Mailchimp failed ({path}): {exc}")
        return {}  # unreachable

    if resp.status_code != 200:
        # Never echo response bodies verbatim -- they can contain account details.
        fail(f"Mailchimp API returned HTTP {resp.status_code} for {path}")

    try:
        data = resp.json()
    except ValueError as exc:
        fail(f"Mailchimp API returned non-JSON for {path}: {exc}")
        return {}  # unreachable

    if not isinstance(data, dict):
        fail(f"Unexpected response shape (not a JSON object) for {path}")

    return data


def main() -> None:
    api_key = os.environ.get("MAILCHIMP_API_KEY", "").strip()
    if not api_key:
        fail("MAILCHIMP_API_KEY is not set")

    audience_id = os.environ.get("MAILCHIMP_AUDIENCE_ID", "").strip()
    base_url = api_base_url(api_key)

    session = requests.Session()
    session.auth = ("anystring", api_key)

    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    list_params = {
        "status": "sent",
        "sort_field": "send_time",
        "sort_dir": "DESC",
        "since_send_time": since,
        "count": 50,
        "fields": "campaigns.id,campaigns.settings,campaigns.send_time,total_items",
    }
    if audience_id:
        list_params["list_id"] = audience_id

    log(f"Looking up campaigns sent in the last {LOOKBACK_DAYS} day(s)...")
    listing = mailchimp_get(session, base_url, "/campaigns", list_params)

    campaigns = listing.get("campaigns")
    if campaigns is None:
        fail("Mailchimp /campaigns response did not include a 'campaigns' field")
    if not isinstance(campaigns, list):
        fail("Mailchimp /campaigns 'campaigns' field was not a list")

    already_processed = load_processed_ids(STATE_FILE)
    log(f"{len(campaigns)} sent campaign(s) in window; {len(already_processed)} already processed.")

    new_campaigns = []
    for campaign in campaigns:
        if not isinstance(campaign, dict):
            fail("Encountered a malformed campaign entry in the Mailchimp response")

        campaign_id = campaign.get("id")
        if not campaign_id:
            fail("A campaign entry was missing its 'id' field")

        if campaign_id in already_processed:
            continue

        settings = campaign.get("settings") or {}
        subject = settings.get("subject_line", "").strip()
        send_time = campaign.get("send_time", "")

        log(f"Fetching content for new campaign {campaign_id} ({subject!r})...")
        content = mailchimp_get(session, base_url, f"/campaigns/{campaign_id}/content")
        html = content.get("archive_html") or content.get("html")
        if not html:
            fail(f"Campaign {campaign_id} content response had no archive_html/html field")

        new_campaigns.append(
            {
                "id": campaign_id,
                "subject": subject,
                "send_time": send_time,
                "html": html,
            }
        )

    if not new_campaigns:
        log("No new campaigns found. Nothing to do.")
        # Make sure a stale file from a previous local run doesn't leak in.
        if os.path.exists(NEW_CAMPAIGNS_PATH):
            os.remove(NEW_CAMPAIGNS_PATH)
        return

    with open(NEW_CAMPAIGNS_PATH, "w", encoding="utf-8") as fh:
        json.dump(new_campaigns, fh)

    log(f"Found {len(new_campaigns)} new campaign(s); wrote raw content to {NEW_CAMPAIGNS_PATH}")

    # Let subsequent steps in the same job know there is work to do, without
    # ever writing the API key or campaign content into a log line.
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as fh:
            fh.write(f"MIRROR_NEW_CAMPAIGNS_PATH={NEW_CAMPAIGNS_PATH}\n")


if __name__ == "__main__":
    main()
