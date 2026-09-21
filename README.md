# mailchimp-substack-mirror

Watches Rift Capital's Mailchimp account for newly **sent** newsletter
campaigns and cleans each one into a Substack-ready Markdown draft. It's a
one-way transfer: Mailchimp -> Markdown draft. No emails, no notifications --
the repo's own state file is the thing to check.

Everything in this repo runs on GitHub Actions. **This repo does not, and
will never, publish to Substack.** Substack has no public API for creating a
post, and this repo intentionally does not drive a headless browser against
Substack with a stored session cookie -- that would violate Substack's
Terms of Service. The last mile (turning a Markdown draft into an actual
Substack draft) is handled outside this repo by a Claude scheduled task
using Claude for Chrome.

## How it works

```
mailchimp-substack-mirror/
├── .github/workflows/mirror.yml   # runs weekly (Fri 17:00 Paris) + on demand
├── scripts/
│   ├── fetch_campaigns.py         # Step 1: find newly-sent campaigns
│   └── transform_content.py       # Step 2: HTML -> clean Markdown
├── state/processed_campaigns.json # single source of truth: what's done
├── drafts/                        # one .md file per pending campaign
│   └── posted/                    # moved here once Claude for Chrome
│                                   # has created the matching Substack draft
└── requirements.txt
```

1. **Fetch** (`scripts/fetch_campaigns.py`) calls the Mailchimp API for
   campaigns with `status=sent` in the last 10 days (a wider window than the
   weekly cadence itself, so a delayed or missed run still catches
   everything), skips anything already recorded in
   `state/processed_campaigns.json`, and pulls each new campaign's full
   HTML content.
2. **Transform** (`scripts/transform_content.py`) strips merge tags
   (`*|FNAME|*`), the Mailchimp address/unsubscribe footer, tracking
   pixels, and "view this email in your browser" links; converts headings,
   bold/italic, lists, links, images, CTA buttons, and dividers into clean
   Markdown; and writes `drafts/{campaign_id}.md` with a small YAML front
   matter block (`title`, `subtitle`, `campaign_id`). On success it appends
   a record to `state/processed_campaigns.json` with `posted_to_substack:
   false`.
3. The workflow commits the updated `state/processed_campaigns.json` and any
   new `drafts/*.md` files in the same job run.
4. Separately (not in this repo), a Claude scheduled task periodically
   checks `drafts/*.md` against entries in `state/processed_campaigns.json`
   where `posted_to_substack` is still `false`, and uses Claude for Chrome to
   recreate that Markdown as an actual Substack draft. Once it succeeds, it
   flips that entry's `posted_to_substack` to `true` and (by convention)
   moves the file to `drafts/posted/`, committing the change back. That flag
   is the single source of truth for what's still pending -- this repo does
   not track state anywhere else, and there's no notification step: check
   `drafts/` or the state file directly to see what's waiting.

## Setup

### 1. Make sure the repo is private

`state/processed_campaigns.json` and everything under `drafts/` contains
real newsletter content, so this repo must stay **private** regardless of
the credentials below.

### 2. Repository secrets

Settings → Secrets and variables → Actions → **Secrets**. Never write any of
these into a script, config file, test fixture, or commit.

| Secret | Used by | Notes |
| --- | --- | --- |
| `MAILCHIMP_API_KEY` | `fetch_campaigns.py` | Mailchimp keys are suffixed with a datacenter, e.g. `...-us21`; the script derives the API base URL from that suffix automatically. |
| `MAILCHIMP_AUDIENCE_ID` | `fetch_campaigns.py` | Optional. Only needed to filter campaigns to a specific audience/list. |

No other secrets or variables are needed -- there's no notification step.

### 3. First run

Use the **Run workflow** button (`workflow_dispatch`) on the
`Mirror Mailchimp to Substack drafts` workflow to trigger a run on demand
instead of waiting for the next scheduled Friday 17:00 (Europe/Paris) run --
this is also how the acceptance testing below should be done.

## Testing checklist

Run via `workflow_dispatch` against at least 2 real past Mailchimp
campaigns:

- [ ] A known sent campaign produces a new `drafts/{id}.md` file and a new
      entry in `state/processed_campaigns.json`.
- [ ] Running the workflow again immediately after produces **no**
      duplicate file and **no** duplicate state entry.
- [ ] The generated Markdown contains no merge tags, no unsubscribe footer
      text, no tracking pixel reference, and no "view in browser" link.
- [ ] Headings, bold/italic, lists, links, images, and dividers from the
      original campaign are all present and correctly formatted.
- [ ] No secret value appears anywhere in the committed repo or in logs
      printed by the workflow run.

## Non-goals (intentionally out of scope)

- No code in this repo logs into Substack, stores a Substack session
  cookie, or drives a headless browser against Substack.
- This repo is never made public.
- No secret value is ever written into a committed file, including test
  fixtures, `.env.example` files, or debug logs.
- No custom database, queue, or external service for state --
  `state/processed_campaigns.json` is the entire state layer, by design.
- No email/notification step of any kind -- check `drafts/` or the state
  file directly.
