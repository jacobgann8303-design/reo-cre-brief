# CLAUDE.md

Project instructions for Claude Code working in this repo.

## What this is

An automated daily commercial real estate brief for a Texas Tech REO student. It runs in GitHub
Actions every weekday morning, pulls live data from ~17 sources, has Claude write a teaching brief,
publishes it to GitHub Pages, and pushes a notification to the student's phone.

The point is teaching, not summarizing. Every brief ends with a concept explained against a real
number from that morning. A brief that reads like a newsletter has failed.

## Pipeline

```
collect/gather.py  →  state/raw.json  →  brief/generate.py  →  state/brief-DATE.md
                                                            →  docs/index.html
                                                            →  state/log.json
                                                            →  deliver/push.py
```

Each stage runs independently. Debug them one at a time rather than running the whole chain.

```bash
python collect/gather.py --verbose      # see which feeds succeeded
python brief/generate.py --dry-run      # print the prompt, no API call, no cost
python deliver/push.py --test           # send a test notification
./run.sh                                # all three
```

## Files that matter

| Path | What it does |
|---|---|
| `collect/sources.yaml` | The source registry. Add or remove feeds here, nowhere else. |
| `collect/gather.py` | Parallel fetch. Never let one dead feed kill the run. |
| `brief/generate.py` | Builds the prompt and calls Claude. The prompt is in `build_prompt`. |
| `state/curriculum.md` | 46 concepts in teaching order. The rotation index lives in `log.json`. |
| `state/log.json` | Running memory: yesterday's 10-year, concept index, streak, open thread. |
| `deliver/push.py` | ntfy push and optional email. |
| `.github/workflows/daily-brief.yml` | The 6:30 AM CT weekday schedule. |

## Rules for working here

**Never let the brief invent a number.** The prompt says so and the code enforces it by passing
only verified market data. If you change `build_prompt`, keep that constraint intact. The student
may repeat these numbers to a professional.

**Verify a feed before adding it.** Many CRE publishers return 403 to scripts. Test first:

```bash
curl -sL -H "User-Agent: Mozilla/5.0" "FEED_URL" | grep -c "<item"
```

If it 403s, route it through Google News instead and mark `via: google_news` so the parser knows
to split the publisher name back out of the title:

```
https://news.google.com/rss/search?q=site:example.com+when:2d&hl=en-US&gl=US&ceid=US:en
```

Currently proxied this way because they block scripts: GlobeSt, Trepp, ULI, CoStar, CBRE.

**Market data sources are keyless on purpose.** FRED's `fredgraph.csv` endpoint for the 10-year
(DGS10) and S&P (SP500), Yahoo's `v8/finance/chart` for the REIT. Don't swap in anything requiring
an API key without a strong reason — every added key is another thing that can expire silently.

**The log is the product.** Continuity is what makes this a course rather than a feed. Don't break
`state/log.json` handling. If you add fields, keep them backward compatible — a missing key should
degrade, not crash.

**Failures should be loud but non-fatal.** A brief built from 12 of 17 feeds is fine. A workflow
that crashed at 6:30 AM is not. Keep the try/except boundaries around every network call.

## Common tasks

**Change the send time** — edit the cron in the workflow. GitHub cron is UTC. `30 11 * * 1-5` is
6:30 AM CDT. In winter (CST) the same line fires at 5:30 AM, so shift to `30 12` around November if
that matters.

**Change the student's REIT** — set `"reit": "TICKER"` in `state/log.json`. The collector reads it
from there.

**Add a concept** — append to `state/curriculum.md` in the numbered `**Bold**` format. The parser
picks up anything matching `N. **Name**`.

**Cost control** — `limits.max_chars_to_model` in `sources.yaml` and `per_feed_items` bound how much
goes to the model. A run is a few cents at current Sonnet pricing.
