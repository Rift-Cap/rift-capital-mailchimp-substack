#!/usr/bin/env python3
"""
Step 2 of the Mailchimp -> Substack mirror.

Reads the raw campaign HTML written by fetch_campaigns.py, strips
Mailchimp-specific cruft (merge tags, the address/unsubscribe footer,
tracking pixels, "view in browser" links), converts the remaining content to
clean Markdown, and writes one drafts/{campaign_id}.md file per campaign.

On success for a campaign, appends a record to state/processed_campaigns.json
and to a temporary "prepared this run" list that notify.py reads next.

A campaign whose HTML can't be confidently parsed is skipped (logged, not
recorded as processed) so it is retried on the next run rather than being
committed in a broken, half-finished state.
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_markdown

STATE_FILE = os.path.join("state", "processed_campaigns.json")
DRAFTS_DIR = "drafts"

NEW_CAMPAIGNS_PATH = os.environ.get(
    "MIRROR_NEW_CAMPAIGNS_PATH",
    os.path.join(tempfile.gettempdir(), "mirror_new_campaigns.json"),
)
PREPARED_PATH = os.path.join(tempfile.gettempdir(), "mirror_prepared_campaigns.json")

MERGE_TAG_RE = re.compile(r"\*\|[^|*]+\|\*")
FOOTER_TEXT_MARKERS = (
    "unsubscribe from this list",
    "update subscription preferences",
    "our mailing address is",
    "why did i get this",
)
VIEW_IN_BROWSER_MARKERS = (
    "view this email in your browser",
    "view in browser",
    "view as a web page",
)
TRACKING_HOST_MARKERS = ("list-manage.com/track", "mailchimp.com/track", "/track/click", "/track/open")


def log(message: str) -> None:
    print(f"[transform_content] {message}", flush=True)


def load_json_array(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else []


def is_tracking_pixel(img_tag) -> bool:
    width = str(img_tag.get("width", "")).strip()
    height = str(img_tag.get("height", "")).strip()
    src = (img_tag.get("src") or "").lower()
    if width in ("1", "0") and height in ("1", "0"):
        return True
    if "open.php" in src or "/track/open" in src or src.endswith(".gif") and "mailchimp" in src:
        return True
    return False


def strip_tracking_pixels(soup: BeautifulSoup) -> None:
    for img in soup.find_all("img"):
        if is_tracking_pixel(img):
            img.decompose()


def strip_view_in_browser(soup: BeautifulSoup) -> None:
    for a in soup.find_all("a"):
        if a.attrs is None:
            continue  # already removed as part of an earlier match's container
        text = a.get_text(strip=True).lower()
        if any(marker in text for marker in VIEW_IN_BROWSER_MARKERS):
            # Remove the smallest sensible container (its paragraph/cell), not just the <a>.
            container = a.find_parent(["p", "td", "div"]) or a
            container.decompose()


def strip_footer_block(soup: BeautifulSoup) -> None:
    # Mailchimp's default footer block is a table/div carrying an id or class
    # like templateFooter / mcnFooterBlock; fall back to a text-marker sweep
    # for templates that don't use the stock class names.
    for tag in soup.find_all(["table", "div", "td"]):
        if tag.attrs is None:
            continue  # already removed earlier in this same pass
        identifiers = " ".join([tag.get("id", ""), " ".join(tag.get("class", []))]).lower()
        if "footer" in identifiers and ("mcn" in identifiers or "template" in identifiers or "footer" == identifiers.strip()):
            tag.decompose()

    remaining_text = soup.get_text(" ", strip=True).lower()
    if any(marker in remaining_text for marker in FOOTER_TEXT_MARKERS):
        for tag in soup.find_all(["table", "div"]):
            if tag.attrs is None:
                continue  # already removed earlier in this same pass
            tag_text = tag.get_text(" ", strip=True).lower()
            if any(marker in tag_text for marker in FOOTER_TEXT_MARKERS) and len(tag_text) < 2000:
                tag.decompose()
                remaining_text = soup.get_text(" ", strip=True).lower()
                if not any(marker in remaining_text for marker in FOOTER_TEXT_MARKERS):
                    break


def strip_merge_tags(soup: BeautifulSoup) -> None:
    for text_node in soup.find_all(string=MERGE_TAG_RE):
        text_node.replace_with(MERGE_TAG_RE.sub("", str(text_node)))


def unwrap_button_ctas(soup: BeautifulSoup) -> None:
    """Collapse Mailchimp's table-based CTA buttons into a plain <a> tag."""
    for a in soup.find_all("a"):
        if a.attrs is None:
            continue  # already replaced/removed earlier in this same pass
        classes = " ".join(a.get("class", [])).lower()
        role = (a.get("role") or "").lower()
        if "button" in classes or role == "button":
            plain = soup.new_tag("a", href=a.get("href", "#"))
            plain.string = a.get_text(strip=True) or a.get("href", "")
            # Wrap in <p> so it stays a block-level element and doesn't run
            # into whatever inline content sits next to the original table.
            paragraph = soup.new_tag("p")
            paragraph.append(plain)
            wrapper = a.find_parent("table") or a
            wrapper.replace_with(paragraph)


def unwrap_tracking_links(soup: BeautifulSoup) -> None:
    """Best-effort unwrap of Mailchimp click-tracking redirect URLs.

    Mailchimp resolves *|these|* server-side per send, so the final
    destination isn't always recoverable from the URL alone. We recover it
    when it's present as a literal query parameter, and otherwise leave the
    link as-is rather than guess.
    """
    for a in soup.find_all("a", href=True):
        if a.attrs is None:
            continue  # already replaced/removed earlier in this same pass
        href = a["href"]
        lowered = href.lower()
        if not any(marker in lowered for marker in TRACKING_HOST_MARKERS):
            continue
        # Some legacy tracking links carry the real destination as a query param.
        match = re.search(r"[?&](?:url|u|dest|destination)=([^&]+)", href, re.IGNORECASE)
        if match:
            from urllib.parse import unquote

            candidate = unquote(match.group(1))
            if candidate.startswith("http"):
                a["href"] = candidate


def strip_merge_tags_from_text(markdown: str) -> str:
    return MERGE_TAG_RE.sub("", markdown)


def normalize_dividers(markdown: str) -> str:
    # markdownify renders <hr> as "* * *"; Substack/standard Markdown wants "---".
    return re.sub(r"^\s*\*\s*\*\s*\*\s*$", "---", markdown, flags=re.MULTILINE)


def separate_adjacent_links_and_images(markdown: str) -> str:
    # Mailchimp templates sometimes lay block elements (a CTA table, an
    # image) directly against each other with no whitespace; once collapsed
    # to inline elements they can otherwise run together on one line.
    return re.sub(r"(\]\([^)\n]*\))(!?\[)", r"\1\n\n\2", markdown)


def collapse_blank_lines(markdown: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", markdown).strip() + "\n"


def clean_and_convert(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    strip_tracking_pixels(soup)
    strip_view_in_browser(soup)
    strip_footer_block(soup)
    strip_merge_tags(soup)
    unwrap_button_ctas(soup)
    unwrap_tracking_links(soup)

    body = soup.body or soup
    markdown = html_to_markdown(
        str(body),
        heading_style="ATX",
        bullets="-",
        strip=["script", "style"],
    )

    markdown = strip_merge_tags_from_text(markdown)
    markdown = normalize_dividers(markdown)
    markdown = separate_adjacent_links_and_images(markdown)
    markdown = collapse_blank_lines(markdown)
    return markdown


def yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def write_draft(campaign_id: str, subject: str, body_markdown: str) -> str:
    os.makedirs(DRAFTS_DIR, exist_ok=True)
    draft_path = os.path.join(DRAFTS_DIR, f"{campaign_id}.md")
    front_matter = (
        "---\n"
        f'title: "{yaml_escape(subject)}"\n'
        f'subtitle: ""\n'
        f'campaign_id: "{campaign_id}"\n'
        "---\n\n"
    )
    with open(draft_path, "w", encoding="utf-8") as fh:
        fh.write(front_matter + body_markdown)
    return draft_path


def main() -> None:
    if not os.path.exists(NEW_CAMPAIGNS_PATH):
        log("No new-campaigns file found -- nothing to transform.")
        return

    with open(NEW_CAMPAIGNS_PATH, "r", encoding="utf-8") as fh:
        new_campaigns = json.load(fh)

    if not isinstance(new_campaigns, list) or not new_campaigns:
        log("New-campaigns file was empty -- nothing to transform.")
        return

    processed = load_json_array(STATE_FILE)
    processed_ids = {rec.get("campaign_id") for rec in processed if isinstance(rec, dict)}
    prepared_this_run = []

    for campaign in new_campaigns:
        campaign_id = campaign.get("id")
        subject = campaign.get("subject", "")
        send_time = campaign.get("send_time", "")
        html = campaign.get("html", "")

        try:
            body_markdown = clean_and_convert(html)
            if not body_markdown.strip():
                raise ValueError("conversion produced empty content")
            draft_path = write_draft(campaign_id, subject, body_markdown)
        except Exception as exc:  # noqa: BLE001 - a bad campaign shouldn't kill the run
            log(f"SKIPPING campaign {campaign_id} ({subject!r}) -- could not parse HTML: {exc}")
            continue

        log(f"Transformed campaign {campaign_id} ({subject!r}) -> {draft_path}")

        prepared_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {
            "campaign_id": campaign_id,
            "subject": subject,
            "send_time": send_time,
            "prepared_at": prepared_at,
            "draft_file": draft_path,
            "posted_to_substack": False,
        }

        if campaign_id not in processed_ids:
            processed.append(record)
            processed_ids.add(campaign_id)

        prepared_this_run.append(record)

    if not prepared_this_run:
        log("No campaigns were successfully transformed this run.")
        return

    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(processed, fh, indent=2)
        fh.write("\n")

    with open(PREPARED_PATH, "w", encoding="utf-8") as fh:
        json.dump(prepared_this_run, fh)

    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as fh:
            fh.write(f"MIRROR_PREPARED_PATH={PREPARED_PATH}\n")

    log(f"Wrote {len(prepared_this_run)} draft(s) and updated {STATE_FILE}.")


if __name__ == "__main__":
    main()
