#!/usr/bin/env python3
"""
Step 3 of the Mailchimp -> Substack mirror.

Sends a short email for each campaign transform_content.py prepared in this
run, naming the campaign subject and the drafts/{id}.md path so the
Claude-for-Chrome handoff (and Paul) know a new draft is waiting.

By design this script must NEVER fail the job: a missed notification is
recoverable by checking the repo directly, a lost/half-committed draft is
not. Every failure path is caught, logged, and this always exits 0.
"""

import json
import os
import smtplib
import sys
import tempfile
from email.mime.text import MIMEText

PREPARED_PATH = os.environ.get(
    "MIRROR_PREPARED_PATH",
    os.path.join(tempfile.gettempdir(), "mirror_prepared_campaigns.json"),
)


def log(message: str) -> None:
    print(f"[notify] {message}", flush=True)


def repo_file_url(draft_file: str) -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    if not repo:
        return draft_file
    return f"{server}/{repo}/blob/{ref}/{draft_file}"


def send_email(to_addr: str, subject: str, body: str) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USERNAME", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    from_addr = os.environ.get("NOTIFY_EMAIL_FROM", smtp_user or to_addr)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
        server.ehlo()
        if server.has_extn("STARTTLS"):
            server.starttls()
            server.ehlo()
        if smtp_user:
            server.login(smtp_user, smtp_password)
        server.sendmail(from_addr, [to_addr], msg.as_string())


def main() -> None:
    try:
        to_addr = os.environ.get("NOTIFY_EMAIL_TO", "").strip()
        if not to_addr:
            log("NOTIFY_EMAIL_TO is not set -- skipping notification.")
            return

        if not os.path.exists(PREPARED_PATH):
            log("No prepared-campaigns file found -- nothing to notify about.")
            return

        with open(PREPARED_PATH, "r", encoding="utf-8") as fh:
            prepared = json.load(fh)

        if not isinstance(prepared, list) or not prepared:
            log("Prepared-campaigns file was empty -- nothing to notify about.")
            return

        if not os.environ.get("SMTP_HOST"):
            log("SMTP_HOST is not configured -- cannot send email, but this is not fatal.")
            return

        for record in prepared:
            subject_line = record.get("subject") or "(no subject)"
            draft_file = record.get("draft_file", "")
            campaign_id = record.get("campaign_id", "")

            email_subject = f"New Substack draft ready: {subject_line}"
            email_body = (
                f"A new Mailchimp campaign has been mirrored and is ready for the "
                f"Substack draft step.\n\n"
                f"Campaign: {subject_line}\n"
                f"Campaign ID: {campaign_id}\n"
                f"Draft file: {draft_file}\n"
                f"View in repo: {repo_file_url(draft_file)}\n"
            )

            try:
                send_email(to_addr, email_subject, email_body)
                log(f"Notified {to_addr} about campaign {campaign_id}.")
            except Exception as exc:  # noqa: BLE001 - never let a single failure stop the loop
                log(f"Could not send notification for campaign {campaign_id}: {exc}")

    except Exception as exc:  # noqa: BLE001 - notify.py must never fail the job
        log(f"Notification step hit an unexpected error, continuing anyway: {exc}")


if __name__ == "__main__":
    main()
    sys.exit(0)
