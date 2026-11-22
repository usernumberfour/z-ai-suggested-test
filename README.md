# Sam's daily wrap up — Path C V1

Nightly digest of developing stories across 10 regions, produced by **GitHub Actions**
with **no AI session dependency**: RSS feeds + market endpoints → markdown edition →
committed to `editions/` → (optional) pushed to Discord / Telegram / email.

Every edition is a git commit — **the repo history is the audit ledger**.
Failed runs show a red X in the Actions tab and GitHub emails you automatically.

---

## Setup (5 minutes, no coding)

### 1. Upload the files

Unzip `path-c-v1.zip`. In your GitHub repo:

1. Click **Add file → Upload files**.
2. Drag in the **four items**: the `.github` folder, `scripts` folder, `config` folder, and `README.md`.
3. Commit message anything (e.g. "add daily wrap up pipeline") → **Commit changes**.

> **Hidden folder note:** `.github` starts with a dot.
> - **Windows:** File Explorer shows it normally.
> - **macOS Finder:** press `Cmd + Shift + .` to reveal hidden folders before dragging.

The files must end up like this (relative to repo root):

```
.github/workflows/daily-wrapup.yml
scripts/fetch_digest.py
config/feeds.yaml
README.md
editions/          (created automatically by the first run)
```

### 2. Confirm Actions is enabled

Repo → **Settings → Actions → General** → "Allow all actions and reusable workflows"
(default on most repos). You will also see a **Actions** tab with a
"daily-wrapup" workflow.

### 3. Test it now (30 seconds)

Repo → **Actions** tab → **daily-wrapup** → **Run workflow** → Run.
Green tick = an edition for today is committed in `editions/`.
Open the file and check it — that is exactly what every night will look like.

### 4. Zero-config delivery (recommended first step)

Click **Watch → All Activity** at the top of the repo.
GitHub will email/notify you on every edition commit. Combined with automatic
failure emails, you get: **silence + daily commit = working; email = problem.**

### 5. (Optional) Push delivery to Discord / Telegram / email

Each channel activates only if its repo secret exists — no code changes:

| Channel | Setup | Secrets to add |
|---|---|---|
| **Discord** (easiest) | Channel → Edit channel → Integrations → Webhooks → New Webhook → Copy URL | `DISCORD_WEBHOOK` |
| **Telegram** | Message @BotFather → /newbot → get token; message the bot once; get chat id | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| **Email (Gmail)** | Google account → Security → 2-Step Verification on → App passwords → create | `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `EMAIL_TO` |

Add via **Settings → Secrets and variables → Actions → New repository secret**.

---

## Schedule & reliability

- Runs at **21:00 Melbourne every night**. GitHub cron only understands UTC, so the
  workflow fires at 10:00 **and** 11:00 UTC; the script's **DST guard** produces the
  edition only in the correct 21:00 hour (AEDT vs AEST handled automatically).
  The twin cron self-skips — you'll see "SKIP DST guard" in its log.
- **Duplicate guard:** if today's edition already exists complete, a second run
  cleanly does nothing (no double editions, ever).
- GitHub occasionally delays scheduled runs by 10–30 min under load. The 24h
  coverage window is anchored to actual run time, so the edition stays correct.
- GitHub auto-disables scheduled workflows after 60 days of repo inactivity —
  irrelevant here, because every nightly edition is itself a commit.
- Private repos include 2,000 free Actions-minutes/month; this job uses ~2.

## What V1 output looks like

Per region: up to 5 freshest clippings (title — source — Melbourne timestamp — link),
`None.` when a region has nothing fresh in 24h. A **Markets at wrap time** section
with level + % and honest state labels (`closed` vs `still trading`, per-exchange
local hours, including Sunday–Thursday markets like Tadawul).

**Honest limitations of V1 (by design):**
- It is a **clippings digest** — freshest items per source, not an editorially
  selected/synthesized analysis. Selection depth = V2 (AI step inside the pipeline).
- No verifiable timestamp → item excluded (timestamp discipline is enforced, not hoped for).
- EGX 30 has no free reliable endpoint → reported as `unavailable`, not faked.
- JSE Top 40 uses the Satrix 40 ETF as a proxy (labeled as such in the edition).

## Tuning without code

Edit **`config/feeds.yaml`** only:
- Add/remove/reorder feeds (order = priority within a region).
- `require_any:` keeps only items mentioning those keywords (used for Al Jazeera's
  global feed → Middle East items only; SCMP Asia → SEA items only). Copy that
  pattern to any feed you add.

Dead feeds never break the edition: they are skipped and listed in the footer
(`feeds failed: ...`) — the honesty discipline carries over from the Z.ai ledger.

## V2 upgrade path

V1 is intentionally shaped so V2 is one added step: after the clippings are
fetched, the workflow calls an LLM API (your key, stored as a repo secret) that
applies the full editorial layer — fresh-angle selection, threading, market
context — and writes the edition in the exact format you have now. Same
intelligence, hosted where it always fires. Ask when you want it wired.
