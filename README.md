# Recruiter Outreach OS

A local-first app for personalised internship outreach to recruiters: import contacts → review → generate individual emails → approve → schedule → send through the **official Gmail API** → track bounces/replies → manage follow-ups.

Runs entirely on your Mac (Python, Streamlit, SQLite). No paid services. No AI API needed.

> **Honest scope.** Nothing here can guarantee inbox or Primary-tab placement; that is decided by each recipient's provider. "Submitted to Gmail" means only that Gmail's API accepted the message. The app deliberately has no tracking pixels, sender rotation, randomised "human" delays, or filter evasion. It improves your odds only through legitimate means: relevant recipients, individual personalisation, accurate subjects, small pilots, conservative caps, and removing bounces/opt-outs.

---------------------------------------------------------------------

## 1. Install (macOS)

```bash
cd "Recruiter Outreach OS"
/opt/homebrew/bin/python3.11 -m venv .venv      # any Python 3.10–3.12 works
source .venv/bin/activate
pip install -r requirements.txt                  # exact tested versions: requirements.lock.txt
cp .env.example .env                             # optional; defaults are fine
```

## 2. Run

```bash
./scripts/start.sh
```

That starts the **worker** (scheduler/sender) and the **dashboard** (http://localhost:8501). Or in two terminals:

```bash
source .venv/bin/activate && python -m recruiter_os.worker     # terminal 1
source .venv/bin/activate && streamlit run app.py              # terminal 2
```

The database (`data/recruiter_os.db`) is created and migrated automatically (Alembic).
The app starts in **TEST MODE**: nothing is sent to recruiters until you switch it off.

The UI is a dark, violet-accented theme (`recruiter_os/ui/assets/theme.css`) with a live 3D hero (three.js, loaded from cdnjs, so it needs internet; offline it falls back to a CSS cube) and 3D-tilt cards. Respects *reduced motion*.

## 3. Google Cloud + Gmail OAuth setup (one time, ~5 min, free)

1. Open <https://console.cloud.google.com/> → create a project (e.g. `recruiter-outreach-os`).
2. **APIs & Services → Library** → enable **Gmail API**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform): User type **External**; app name, your email as support/developer contact. Add yourself under **Test users**.
4. **Scopes**: add `gmail.send`; add `gmail.readonly` only if you want reply/bounce detection (it is requested only when you enable monitoring).
5. **Credentials → Create credentials → OAuth client ID → Application type: Desktop app**. Download the JSON.
6. In the app: **Gmail Settings → upload `client_secret.json`** (or place it at `data/secrets/client_secret.json`) → **Connect Gmail** → approve in the browser. The redirect goes to `http://127.0.0.1:8765` only.
7. Tokens are stored **encrypted** (Fernet; key in the macOS Keychain) under `data/secrets/`, never in the database.

Things to know (verify against Google's current docs, they change):
* While the consent screen is in **Testing**, refresh tokens for external apps **expire after about 7 days**. The app detects this (status "Needs re-authorization", campaigns auto-pause); reconnect. Moving the app to *In production* (personal use, unverified warning screen) avoids the 7-day expiry.
* The Gmail API quota is **not** your sending limit. Personal Gmail accounts have their own daily sending limits set by Google. Stay far below them.
* Disconnecting in the app revokes the token at Google and deletes the local copy.

## 4. First-time workflow

1. **Profile Settings**: pre-filled from `seed/profile.json`; edit anything. Only what is in the profile is ever used in emails.
2. **Resume Manager**: upload a PDF (≤ 5 MB). Nothing is attached unless a campaign selects it.
3. **Contacts → Import**: upload CSV/XLSX or load `data/imports/hr_contacts.csv`. Check the column mapping, import, download rejected rows if any.
   Your file: 1,842 rows → **1,841 imported**, 1 rejected (row 1371, email is the text `undefined`).
4. **Contacts → Browse**: filter, optionally *Check domains* (DNS only; it cannot prove a mailbox exists), set relevance, then **Mark Ready for Outreach**. Only reviewed contacts can enter a campaign.
5. **Campaigns → New campaign**: choose role, mode (A standard / B technical / C role-specific), template, resume.
6. **Recruiters tab**: add a **small pilot** (5–10). Contacts already contacted in any campaign are excluded (global history + a database constraint).
7. **Emails & launch → Generate drafts**, then **Email Generator**: read each email, edit, **Send test copy to myself**, then **Approve**.
8. **Gmail Settings**: connect; confirm daily cap; (when ready) turn **Test mode OFF**.
9. **Campaigns → Start campaign**: type `START CAMPAIGN`. Approved emails get send slots. The worker sends them.
10. Check **Sending Queue**, **Sent Emails**, **Replies**, **Deliverability**. Evaluate the pilot before enlarging anything.

## 5. Scheduling

Settings (Application Settings → Scheduler): time zone (default Asia/Kolkata), sending days, business-hour window, daily cap, batch size, batch interval, pause between emails, retries/backoff, follow-up wait, start/end dates.

* Defaults are conservative: 20/day, batches of 5 every 60 min, Mon–Fri 10:00–17:00.
* A schedule like **batch 50 every 2 h** can be configured, but the daily cap still applies (hard ceiling 150/day in the code) and the app warns about it. It is not a recommendation.
* Slots are deterministic and the cap is re-checked at send time. The cap is global across campaigns.
* The queue is the database. A restart loses and duplicates nothing: sending claims are atomic, each email has a stored unique Message-ID, and an email stuck in *Sending* after a crash is reconciled against Gmail (found → *Submitted*; not found → *Delivery Unknown*, never auto-resent).
* Slots missed while the Mac slept are **re-planned** with normal spacing, not drained as a burst.
* Transient errors retry with exponential backoff up to the max; permanent errors are never auto-retried; quota/auth errors pause the campaign; N consecutive failures auto-pause it.

### Local-execution limitation
The scheduler exists only while `python -m recruiter_os.worker` runs on your Mac. If the Mac sleeps, is shut down, or the worker is stopped, **nothing is sent**. Queued emails wait in the database and resume afterwards. (Tip: `caffeinate -i ./scripts/start.sh` keeps the Mac awake while it runs.)

## 6. Monitoring (needs the read-only scope)

* **Bounces**: Gmail's delivery-failure notices are parsed. Hard bounces (e.g. 5.1.1) mark the email *Bounced* and suppress the address; soft bounces are recorded only; 5.7.x policy rejections are flagged for review.
* **Replies**: inbound messages in your sent threads are matched to the contact and keyword-classified (Positive, Request for Resume, …, Out of Office, Unclassified). Heuristic: correct it on the Replies page. Auto-replies are not counted as replies. Explicit opt-out phrases ("remove me", "stop emailing") auto-suppress.
* Nothing is auto-replied. Draft replies manually.
* **Follow-ups**: reminders after N days if no reply/bounce/opt-out, limited (default 1). Each is drafted, reviewed and approved by you, and sent in the original thread.
* Unavailable from Gmail (shown as such, never estimated): opens, tab placement, spam-folder placement, complaints, reputation.

## 7. Tests

```bash
source .venv/bin/activate
python -m pytest -q          # 142 tests: unit, integration (mocked Gmail), end-to-end, headless UI smoke
```

No test sends real email: Gmail is replaced by an in-memory fake (`tests/fakes.py`). The suite covers import/validation/duplicates, personalisation, state transitions, slot planning, mocked OAuth, MIME, dispatcher (atomic claim, restart, crash recovery, retry/backoff, quota/auth handling), bounce/reply parsing, follow-up rules, suppression, audit immutability, backup/restore, and a full end-to-end workflow. The test-send step is also available for real from the UI (*Send test copy to myself*).

## 8. Security

* OAuth tokens encrypted at rest (Fernet), key in macOS Keychain (or `RECRUITER_OS_SECRET_KEY`, or a 0600 key file). Minimum scopes: `gmail.send`, plus `gmail.readonly` only for monitoring.
* No passwords/SMTP/browser automation; no hard-coded secrets; `.env`, `data/`, `logs/`, databases, tokens, client secrets are git-ignored.
* Templates run in a Jinja2 **sandbox**; HTML email is built from escaped text; header-injection blocked; uploaded resumes validated (extension, `%PDF` header, size, EOF marker), stored under random names with 0600 permissions and checksum-verified before attaching.
* SQL via SQLAlchemy ORM only. The audit log is append-only (SQLite triggers block UPDATE/DELETE).
* Logs are JSON, with tokens and email addresses redacted.
* Backups (Application Settings) contain DB + resumes, **not** tokens. They hold recruiter data: keep them private.
* Test mode is on by default; campaigns need typed authorization; edits revoke approval; suppressed/opted-out contacts can never be approved or sent.

## 9. Environment variables (`.env.example`)

| Variable | Meaning |
|---|---|
| `RECRUITER_OS_DATA_DIR` | data folder (DB, resumes, encrypted tokens, backups). Default `./data` |
| `GOOGLE_CLIENT_SECRET_FILE` | OAuth desktop client JSON |
| `OAUTH_LOCAL_PORT` | local OAuth redirect port (default 8765) |
| `RECRUITER_OS_SECRET_KEY` | optional Fernet key override |
| `RECRUITER_OS_TEST_MODE` | initial test-mode value (the UI toggle is authoritative afterwards) |
| `AI_PROVIDER`, `AI_API_KEY` | reserved; AI drafting is not implemented. Core features never need it |
| `LOG_LEVEL` | log verbosity |

## 10. Known limitations

* No Primary-tab/spam-folder/open/complaint data exists in Gmail's API; none is shown.
* Email verification is syntax + DNS (MX) only; mailbox existence is only learned from bounces.
* Reply classification is keyword-based, not ML. Bounce parsing covers Gmail and RFC 3464 formats; unusual formats may be missed (they remain *Submitted*, never guessed as delivered).
* Local scheduling only (see above). Single Gmail account by design: no sender rotation.
* Reply-sync matches by Gmail thread ID; if a recruiter replies from a different thread (new subject), it can be missed.
* Optional AI-assisted drafting is not implemented (the spec made it optional); templates are deterministic.
* The UI uses Streamlit with custom CSS; it follows your system light/dark setting. Layout was verified by headless render tests, not by pixel review.
* The OAuth flow was tested with mocks. The first real connection happens on your machine with your credentials.

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| "OAuth client file not found" | Complete section 3; place the JSON in `data/secrets/` or upload it in Gmail Settings |
| "Access blocked / app not verified" | Add your account under OAuth consent screen → Test users; click *Advanced → continue* |
| Status "Needs re-authorization" | Token revoked/expired (7-day Testing limit): Gmail Settings → Connect |
| Emails stay *Scheduled* | Worker not running (check badge in Sending Queue), Test mode ON, campaign not Active, outside window/day, or daily cap reached |
| "Port 8765 in use" | change `OAUTH_LOCAL_PORT` and add nothing in Google (Desktop clients allow any loopback port) |
| *Delivery Unknown* | A crash interrupted a send and Gmail had no trace. Check Gmail Sent; if absent use *Re-queue* |
| Campaign auto-paused | See the reason on the Campaigns page; fix cause (quota, repeated rejections), then *Resume* |
| Token decrypt error | Keychain key changed; reconnect Gmail |
| Restore a backup | stop worker → Application Settings → Backup & restore → restart the app |

## Project layout

```
app.py                     Streamlit entry (navigation)
recruiter_os/
  models.py db.py config.py constants.py bootstrap.py worker.py
  secure_store.py logging_setup.py
  services/  contacts personalize templates messages campaigns scheduling dispatcher
             gmail monitoring followups resumes suppression analytics backup settings audit
  ui/        common.py + pages/ (15 pages)
migrations/                Alembic (schema + audit-immutability triggers)
tests/                     pytest suite
seed/profile.json          starter profile (git-ignored; editable in the UI)
```
One deliberate simplification: `email_messages` is a single table covering draft → approval → queue → sent (statuses are exactly those in the spec), with `email_events` as the observed-event history, instead of three tables that would duplicate state.
FastAPI was not added: the UI calls the service layer directly, so a separate API server would be unjustified complexity.
