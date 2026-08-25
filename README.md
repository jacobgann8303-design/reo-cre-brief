# The Tape — automated CRE brief

Runs every weekday at 6:30 AM Central in the cloud, pulls ~17 live sources, has Claude write a
teaching brief, and pushes it to your phone. **Your laptop can be closed.**

```
      GitHub Actions (cloud, on a timer)
                  │
    ┌─────────────┼─────────────┐
    ▼             ▼             ▼
  FRED         17 RSS        REO's live
 10yr, S&P     feeds        calendar feed
    └─────────────┼─────────────┘
                  ▼
          Claude writes the brief
                  ▼
    ┌─────────────┴─────────────┐
    ▼                           ▼
 GitHub Pages              ntfy push
 (phone webpage)         (lock screen)
```

Everything is verified working: all 17 feeds return items, FRED and Yahoo need no API key, and the
push notification was tested end to end.

---

## Why not run it on your laptop

You asked for the pipeline to run from this device. It can — `./run.sh` does exactly that. But a
cron job on a laptop only fires when the laptop is awake, so a closed lid at 6:30 AM means no brief.
GitHub Actions is free for public repos and always on. Same code either way.

---

## Setup — about 20 minutes

### 1. Create the repo

```bash
cd reo-cre-brief
git init
git add -A
git commit -m "Initial commit"
gh repo create reo-cre-brief --public --source=. --push
```

No `gh` CLI? Make an empty repo on github.com, then:

```bash
git remote add origin https://github.com/YOURNAME/reo-cre-brief.git
git branch -M main
git push -u origin main
```

Public matters — Actions minutes are free on public repos. Nothing secret lives in the code; keys
go in Secrets.

### 2. Get an Anthropic API key

console.anthropic.com → API Keys → Create Key. This is separate from your Claude subscription and
billed per use. Expect a few cents per brief.

### 3. Add secrets

Repo → Settings → Secrets and variables → Actions.

**Secrets** (encrypted):

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `NTFY_TOPIC` | a hard-to-guess string, e.g. `reo-brief-a7f3k9x2` |

**Variables** (visible, not secret) — the Variables tab:

| Name | Value |
|---|---|
| `BRIEF_URL` | `https://YOURNAME.github.io/reo-cre-brief/` |

> Your ntfy topic name **is** the password. Anyone who guesses it can read your notifications.
> Make it random. Never use `reo-brief`.

### 4. Set up the phone notification

Install **ntfy** — free, open source, on the App Store and Google Play. Open it, tap **+**, and
subscribe to the exact topic string you used above. Done. No account, no signup.

Test it:

```bash
NTFY_TOPIC=your-topic-here python3 deliver/push.py --test
```

Your phone should buzz within a second or two.

### 5. Turn on GitHub Pages

Repo → Settings → Pages → Source: **Deploy from a branch** → branch `gh-pages`, folder `/`.

The branch appears after the first successful run, so do step 6 first, then come back.

### 6. Run it once by hand

Repo → Actions → **Daily CRE Brief** → **Run workflow**.

Watch the log. When it's green, your brief is at `https://YOURNAME.github.io/reo-cre-brief/` and
your phone has a notification.

### 7. Add it to your home screen

Open the Pages URL in Safari or Chrome on your phone → Share → **Add to Home Screen**. It opens
full screen like an app.

---

## Running locally instead

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in your key and topic
./run.sh
open docs/index.html
```

To schedule it on a Mac that's usually awake:

```bash
crontab -e
# 6:30 AM weekdays
30 6 * * 1-5 cd /full/path/to/reo-cre-brief && ./run.sh >> /tmp/brief.log 2>&1
```

---

## What's in it

**Market data, keyless and authoritative.** The 10-year Treasury and S&P come from FRED — the St.
Louis Fed's own data. Your REIT comes from Yahoo's chart endpoint. Both free forever, no key to
expire.

**17 verified feeds.** Bisnow, Commercial Observer, CommercialCafe, Connect CRE, REBusinessOnline,
NAIOP, Propmodo, CNBC Finance, CNBC Real Estate, plus GlobeSt, Trepp, ULI, CoStar, and CBRE routed
through Google News because they block scripts. Two custom queries pull Texas-specific CRE and
capital-markets stories with cap-rate/CMBS/NOI language for teaching material.

**REO's live calendar**, so club deadlines appear in the brief automatically and get flagged when
they're inside a week.

**A 46-concept curriculum** that rotates without repeating — cap rate, NOI, DSCR, the capital stack,
waterfalls, IRR vs. equity multiple, NNN leases, CMBS, FFO, 1031s, development spread, ARGUS. One a
day, taught against that morning's actual numbers.

**Memory.** `state/log.json` carries yesterday's 10-year forward, so the brief can tell you the move
since Monday and pick up threads it left open.

---

## Adding a source

Test it first — many CRE publishers block scripts:

```bash
curl -sL -H "User-Agent: Mozilla/5.0" "FEED_URL" | grep -c "<item"
```

Returns a number? Add it to `collect/sources.yaml`. Returns 403? Route it through Google News:

```yaml
  - id: newsource
    name: New Source
    url: "https://news.google.com/rss/search?q=site:example.com+when:2d&hl=en-US&gl=US&ceid=US:en"
    tier: cre
    weight: 3
    via: google_news
```

`tier` decides which brief section it feeds: `national`, `cre`, `texas`, `research`, `teaching`.

---

## Troubleshooting

**Nothing arrived.** Actions tab → open the run → find the red step. `Collect sources` failing means
a network issue; `Generate brief` means the API key.

**Feeds failing.** Run `python collect/gather.py --verbose` locally — it prints ok/FAIL per feed.
Publishers change feed URLs occasionally.

**Wrong time in winter.** GitHub cron is UTC and doesn't observe daylight saving. `30 11` is 6:30 AM
CDT but 5:30 AM CST. Change to `30 12` in November.

**Workflow stopped after 60 days.** GitHub disables scheduled workflows on repos with no activity.
Push any commit to re-enable.

**Cost creeping up.** Lower `per_feed_items` in `sources.yaml`, or set the `BRIEF_MODEL` variable to
a smaller model.

---

## Files

```
reo-cre-brief/
├── .github/workflows/daily-brief.yml   the 6:30 AM schedule
├── collect/
│   ├── sources.yaml                    all 17 feeds + market config
│   └── gather.py                       parallel fetch → state/raw.json
├── brief/generate.py                   prompt + Claude call → brief + HTML
├── deliver/push.py                     ntfy push, optional email
├── state/
│   ├── curriculum.md                   46 concepts in teaching order
│   └── log.json                        running memory
├── docs/                               published to GitHub Pages
├── CLAUDE.md                           instructions for Claude Code
├── run.sh                              local run
└── .env.example
```
