#!/usr/bin/env python3
"""
brief/generate.py — turn state/raw.json into a teachable daily brief.

Sends the collected data to Claude with a teaching-focused prompt, then writes:
    state/brief-YYYY-MM-DD.md    the brief itself
    state/log.json               running memory (yesterday's numbers, concept index)
    docs/index.html              phone-readable page for GitHub Pages
    docs/archive/YYYY-MM-DD.html

The log is what makes this a course rather than a newsletter. Each run reads
the previous entry, so the brief can say "the 10-year is up 14bps since Monday"
and the curriculum never repeats a concept until the cycle finishes.

    python brief/generate.py                 # normal run
    python brief/generate.py --dry-run       # build the prompt, skip the API call
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
DOCS = ROOT / "docs"
MODEL = os.environ.get("BRIEF_MODEL", "claude-sonnet-4-6")


def load_curriculum() -> list[str]:
    text = (ROOT / "state" / "curriculum.md").read_text(encoding="utf-8")
    return [m.group(1).strip()
            for m in re.finditer(r"^\s*\d+\.\s+\*\*(.+?)\*\*", text, re.M)]


def load_curriculum_notes() -> dict[str, str]:
    """Concept name -> its one-line curriculum blurb, used to frame review questions."""
    text = (ROOT / "state" / "curriculum.md").read_text(encoding="utf-8")
    notes = {}
    for m in re.finditer(r"^\s*\d+\.\s+\*\*(.+?)\*\*[ \t]*(?:[—-]\s*(.*))?$", text, re.M):
        notes[m.group(1).strip()] = (m.group(2) or "").strip()
    return notes


# ----------------------------------------------------------------------------
# Spaced repetition
#
# Scheduling is by brief count, not calendar date. A missed run then can't cause
# a pile-up of overdue reviews — the sequence just resumes where it left off.
# ----------------------------------------------------------------------------

REVIEW_INTERVALS = [1, 4, 14]   # briefs until the next review, indexed by stage
MAX_REVIEWS = 2                 # per brief, so a backlog drains instead of flooding


def due_reviews(schedule: list[dict], brief_no: int,
                limit: int = MAX_REVIEWS) -> list[dict]:
    """Concepts whose next review has come due, longest-overdue first."""
    due = [e for e in schedule
           if isinstance(e, dict) and e.get("concept")
           and e.get("due_at", 0) <= brief_no]
    due.sort(key=lambda e: (e.get("due_at", 0), e.get("taught_at", 0)))
    return due[:limit]


def advance_schedule(schedule: list[dict], reviewed: list[dict],
                     new_concept: str, brief_no: int) -> list[dict]:
    """Push reviewed concepts to their next stage, graduate finished ones, enrol today's."""
    reviewed_names = {e["concept"] for e in reviewed}
    out = []

    for e in schedule:
        if not isinstance(e, dict) or not e.get("concept"):
            continue                                  # drop anything malformed
        if e["concept"] not in reviewed_names:
            out.append(e)
            continue
        stage = e.get("stage", 0) + 1
        if stage >= len(REVIEW_INTERVALS):
            continue                                  # graduated — stop scheduling it
        out.append({**e, "stage": stage,
                    "due_at": brief_no + REVIEW_INTERVALS[stage]})

    if new_concept and not any(e["concept"] == new_concept for e in out):
        out.append({"concept": new_concept, "taught_at": brief_no,
                    "due_at": brief_no + REVIEW_INTERVALS[0], "stage": 0})
    return out


def build_prompt(raw: dict, history: dict, concept: str, streak: int,
                 reviews: list[dict] | None = None,
                 notes: dict[str, str] | None = None) -> str:
    m = raw["markets"]

    def fmt_market() -> str:
        lines = []
        t = m.get("treasury_10y", {})
        if "value" in t:
            chg = t.get("change_bps")
            if chg is not None:
                arrow = "up" if chg > 0 else "down" if chg < 0 else "flat"
                change_clause = (f" ({arrow} {abs(chg)} bps from {t.get('prior_date')} "
                                  f"at {t.get('prior')}%)")
            else:
                change_clause = ""
            lines.append(f"- 10-Year Treasury: {t['value']:.2f}% as of {t['date']}{change_clause}")
        s = m.get("sp500", {})
        if "value" in s:
            lines.append(f"- S&P 500: {s['value']:,.2f} as of {s['date']} "
                         f"({s.get('change_pct', 0):+.2f}% from prior close)")
        r = m.get("reit", {})
        if r.get("price"):
            lines.append(f"- {r['symbol']} (their REIT): ${r['price']:.2f} "
                         f"({r.get('change_pct', 0):+.2f}%)")
        for key, val in m.items():
            if isinstance(val, dict) and val.get("error"):
                lines.append(f"- {key}: UNAVAILABLE ({val['error']})")
        return "\n".join(lines) or "- No market data retrieved."

    def fmt_events() -> str:
        if not raw["reo_events"]:
            return "- Nothing on the REO calendar in the next 30 days."
        out = []
        for e in raw["reo_events"][:10]:
            flags = []
            if e["is_deadline"]:
                flags.append("DEADLINE")
            if e["urgent"]:
                flags.append(f"{e['days_out']}d away")
            tail = f"  [{', '.join(flags)}]" if flags else ""
            loc = f" @ {e['location']}" if e["location"] else ""
            out.append(f"- {e['when']} {e['time']}: {e['summary']}{loc}{tail}")
        return "\n".join(out)

    def fmt_feeds() -> str:
        chunks = []
        for tier in ("national", "cre", "texas", "research", "teaching"):
            group = [f for f in raw["feeds"] if f["tier"] == tier and f["items"]]
            if not group:
                continue
            chunks.append(f"\n### {tier.upper()}")
            for f in group:
                for it in f["items"][:6]:
                    pub = it.get("publisher") or f["name"]
                    line = f"- [{pub}] {it['title']}"
                    if it.get("summary"):
                        line += f"\n  {it['summary'][:220]}"
                    if it.get("url"):
                        line += f"\n  {it['url']}"
                    chunks.append(line)
        return "\n".join(chunks)

    def fmt_reviews() -> str:
        if not reviews:
            return ("Nothing due for review yet — this is early in the course. "
                    "Skip the Review section entirely today.")
        out = []
        for r in reviews:
            blurb = (notes or {}).get(r["concept"], "")
            taught = r.get("taught_at")
            gap = f" (taught in brief #{taught})" if taught else ""
            out.append(f"- {r['concept']}{gap}" + (f" — {blurb}" if blurb else ""))
        return "\n".join(out)

    prev = history.get("last_entry", {})
    prev_block = "No previous brief — this is day one."
    if prev:
        prev_block = (
            f"Date: {prev.get('date')}\n"
            f"10-year then: {prev.get('treasury_10y')}%\n"
            f"Concept taught: {prev.get('concept')}\n"
            f"Open thread: {prev.get('open_thread', 'none')}"
        )

    return f"""You are the daily commercial real estate tutor for a Texas Tech student in the Real Estate Organization (REO). They are learning CRE from scratch, have no finance background, and sit down with this for about ten minutes each morning.

You are a tutor, not a newsletter. A newsletter tells. A tutor explains in plain words, checks whether it landed, and comes back to things you taught last week. Your goal is that they could explain today's idea out loud to a friend at lunch — not that they read it and nodded.

## How to write for this reader

**Explain every piece of jargon the moment you use it.** Not just today's concept — every term of art anywhere in the brief. "Cap rate", "NOI", "basis point", "absorption", "CMBS", "the capital stack". A short plain-English clause right where the word appears. Never leave an acronym unexplained. Never assume they remember a term from a previous brief — a one-clause reminder costs you nothing.

**Lead with an everyday comparison, then the real thing.** Before the CRE definition, give them something from ordinary life that works the same way. A vending machine, a savings account, a used car, a rent split with roommates. Then show the same shape in commercial real estate with a real number from today. The analogy is the handle they grab; the number is what makes it real.

**Write plainly.** Short sentences. Everyday words where they exist. If you catch yourself writing "yield compression," write "buyers accepting a smaller return" and then note that professionals call it yield compression. They need to recognize the term later — they just don't need it doing the explaining.

**Show the arithmetic.** Write the small calculation out where they can see it. $168,000 ÷ $2,400,000 = 7.0%. Numbers worked in front of someone teach the mechanism; numbers asserted teach nothing.

## Today
{raw['date_label']} — brief #{streak}

## Market data (verified, from FRED and Yahoo — use these exact numbers, do not invent or round away the precision)
{fmt_market()}

## Previous brief
{prev_block}

## REO club calendar (live feed, authoritative)
{fmt_events()}

## Headlines collected this morning
{fmt_feeds()}

## Due for review today (concepts taught earlier — they have NOT seen these since)
{fmt_reviews()}

---

# Write the brief

Use this exact structure:

## The numbers
Three or four lines. Lead with the 10-year and its change in basis points, then the S&P, then their REIT. If the previous brief exists, add one line on the move since then — continuity is the point. Never state a number that isn't in the market data above.

## Headlines
Three to five bullets, each one sentence, each attributed to its publisher in brackets. Pick for relevance to a CRE student, not for drama. Prefer anything touching interest rates, lending, construction costs, or Texas. Skip pure residential and pure celebrity-adjacent stories.

## The one read
Pick exactly one article worth fifteen minutes. Name it, link it, and say in two sentences why this one. Prefer research (Trepp, ULI, NAIOP, CBRE) over news when the quality is comparable — arguments teach better than events.

## REO watch
Only what's actionable. Lead with anything flagged DEADLINE inside 7 days and say plainly what happens if they miss it. Then the next one or two events. Skip this section entirely if nothing is close.

## Today's concept: {concept}
About 200 words, in three moves:
1. **The everyday version.** An ordinary-life comparison that works the same way. Two or three sentences, no CRE vocabulary at all.
2. **The real version.** The same shape in commercial real estate, tied to an actual number from today's data — the actual 10-year, an actual headline, an actual price. Write the arithmetic out.
3. **Why a professional cares.** One or two sentences on when this number changes someone's decision.

A definition they could get from a glossary is a failure.

## Check yourself
One question testing whether they actually understood {concept} — something they must reason through, not recall. Prefer a small calculation or a "what would happen if" over "what does X stand for".

Then put the answer on its own line in exactly this format, on ONE line:

> ANSWER: the full answer, including the reasoning, in one or two sentences.

## Review
{"Skip this section entirely today — nothing is due for review yet." if not reviews else "One question for EACH concept listed under 'Due for review today' above. They have not seen these in days, so make the question stand on its own — do not assume they remember the definition. Each question gets its own answer line in the same format:"}

{"" if not reviews else "> ANSWER: the full answer in one or two sentences."}

{"" if not reviews else "Keep each review question short. The point is retrieval — making them pull it from memory — not teaching the concept again from scratch."}

## Your move
One action, five minutes or less, concrete enough to actually do. Tie it to today when you can.

---

Rules that matter:
- Never invent a number, headline, event, or deadline. Everything comes from the data above.
- If market data is marked UNAVAILABLE, say so in one short clause and move on. Do not guess.
- Every answer line must start with exactly "> ANSWER:" and sit on ONE line. This is parsed by code.
- No throat-clearing, no "in today's dynamic market", no motivational filler.
- Plain markdown. No preamble before the first heading, no sign-off after the last section.

After the brief, output exactly this block on its own lines so tomorrow's run can read it:

<!--STATE
concept_taught: {concept}
open_thread: [one short phrase — a question the brief raised that tomorrow could pick up]
-->
"""


def call_claude(prompt: str) -> str:
    try:
        from anthropic import Anthropic
    except ImportError:
        sys.exit("Missing dependency. Run: pip install -r requirements.txt")

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set. See README, section 3.")

    resp = Anthropic(api_key=key).messages.create(
        model=MODEL,
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


# The model is told to emit "> ANSWER: ...". Accept the near-misses too (**ANSWER:**,
# a missing "> ") so a formatting wobble degrades to a visible answer rather than a
# broken one. The model never emits HTML — we build the <details> ourselves from a
# trusted template, so render_html can keep escaping everything it receives.
ANSWER_RE = re.compile(r"^\s*>?\s*\*{0,2}ANSWER\*{0,2}\s*:\s*(.*)$", re.I)


def render_html(markdown_text: str, raw: dict, concept: str) -> str:
    """Minimal dependency-free markdown -> HTML. Handles what the prompt emits."""
    body, lines = [], markdown_text.split("\n")
    in_list = False
    for line in lines:
        line = line.rstrip()
        answer = ANSWER_RE.match(line)
        if line.startswith("### "):
            if in_list:
                body.append("</ul>"); in_list = False
            body.append(f"<h3>{esc(line[4:])}</h3>")
        elif line.startswith("## "):
            if in_list:
                body.append("</ul>"); in_list = False
            body.append(f"<h2>{esc(line[3:])}</h2>")
        elif answer and answer.group(1).strip():
            if in_list:
                body.append("</ul>"); in_list = False
            body.append('<details><summary>Show answer</summary>'
                        f'<p>{inline(answer.group(1).strip())}</p></details>')
        elif line.startswith("- "):
            if not in_list:
                body.append("<ul>"); in_list = True
            body.append(f"<li>{inline(line[2:])}</li>")
        elif not line.strip():
            if in_list:
                body.append("</ul>"); in_list = False
        else:
            if in_list:
                body.append("</ul>"); in_list = False
            body.append(f"<p>{inline(line)}</p>")
    if in_list:
        body.append("</ul>")

    stats = raw["stats"]
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0B0D10">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="The Tape">
<title>The Tape — {esc(raw['date_label'])}</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Newsreader:opsz,wght@6..72,400;6..72,500&display=swap" rel="stylesheet">
<style>
:root{{--night:#0B0D10;--slab:#15181D;--rule:#252B34;--amber:#FFB000;--cyan:#5BC8D6;--bone:#E6E2D8;--dim:#79828F}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--night);color:var(--bone);font-family:'Newsreader',Georgia,serif;
font-size:18px;line-height:1.6;padding:0 1.15rem 4rem;
background-image:radial-gradient(ellipse 120% 40% at 50% -5%,rgba(255,176,0,.09),transparent 70%)}}
.wrap{{max-width:640px;margin:0 auto}}
header{{padding:2.2rem 0 1rem}}
.dateline{{font-family:'JetBrains Mono',monospace;font-size:.62rem;letter-spacing:.16em;
text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--rule);
padding-bottom:.5rem;margin-bottom:.9rem;display:flex;justify-content:space-between}}
h1{{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:2.6rem;letter-spacing:-.045em;
line-height:.92;margin:0}}
h1 span{{color:var(--amber)}}
h2{{font-family:'JetBrains Mono',monospace;font-size:.72rem;letter-spacing:.16em;text-transform:uppercase;
color:var(--cyan);margin:2.2rem 0 .7rem;padding-top:1.1rem;border-top:1px solid var(--rule);font-weight:700}}
h2:first-of-type{{border-top:0}}
h3{{font-family:'JetBrains Mono',monospace;font-size:.68rem;letter-spacing:.11em;
text-transform:uppercase;color:var(--dim);margin:1.5rem 0 .5rem;font-weight:700}}
p{{margin:0 0 .9rem}}
ul{{margin:0 0 .9rem;padding-left:1.1rem}}
li{{margin-bottom:.55rem}}
a{{color:var(--amber);text-decoration:none;border-bottom:1px solid rgba(255,176,0,.35);
overflow-wrap:anywhere}}
strong{{color:#fff;font-weight:500}}
code{{font-family:'JetBrains Mono',monospace;font-size:.85em;background:var(--slab);padding:.1em .35em}}
details{{background:var(--slab);border:1px solid var(--rule);border-left:2px solid var(--cyan);
margin:.2rem 0 1.1rem;padding:.15rem .95rem}}
details summary{{font-family:'JetBrains Mono',monospace;font-size:.66rem;letter-spacing:.13em;
text-transform:uppercase;color:var(--cyan);cursor:pointer;padding:.7rem 0;list-style:none;
user-select:none}}
details summary::-webkit-details-marker{{display:none}}
details summary::after{{content:" ▸";display:inline-block;transition:transform .15s}}
details[open] summary::after{{transform:rotate(90deg)}}
details[open] summary{{border-bottom:1px solid var(--rule);margin-bottom:.6rem}}
details p{{margin:0 0 .8rem}}
footer{{margin-top:3rem;border-top:1px solid var(--rule);padding-top:1rem;
font-family:'JetBrains Mono',monospace;font-size:.62rem;letter-spacing:.1em;
text-transform:uppercase;color:var(--dim);line-height:1.9}}
footer a{{color:var(--dim);border:0}}
</style></head><body><div class="wrap">
<header>
<div class="dateline"><span>{esc(raw['date_label'])}</span><span>Morning Edition</span></div>
<h1>THE<br>TAPE<span>.</span></h1>
</header>
{chr(10).join(body)}
<footer>
{stats['total_items']} stories from {stats['feeds_ok']} sources &middot;
{stats['reo_events']} REO events &middot; concept: {esc(concept)}<br>
Built {esc(raw['generated_at'][:16].replace('T', ' '))} CT &middot;
<a href="archive/">archive</a>
</footer>
</div></body></html>"""


def esc(t: str) -> str:
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def inline(t: str) -> str:
    t = esc(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    t = re.sub(r"\[(.+?)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', t)
    t = re.sub(r"(?<!\")(?<!=)(https?://[^\s<]+)", r'<a href="\1">\1</a>', t)
    return t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="build prompt, skip API call")
    ap.add_argument("--raw", default=str(STATE / "raw.json"))
    args = ap.parse_args()

    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    log_path = STATE / "log.json"
    history = (json.loads(log_path.read_text(encoding="utf-8"))
               if log_path.exists() else {})

    curriculum = load_curriculum()
    idx = history.get("concept_index", 0) % len(curriculum)
    concept = curriculum[idx]
    streak = history.get("streak", 0) + 1

    # Spaced repetition. A missing or malformed schedule degrades to "no reviews"
    # rather than crashing the 6:30 AM run.
    schedule = history.get("schedule") or []
    if not isinstance(schedule, list):
        schedule = []
    reviews = due_reviews(schedule, streak)
    notes = load_curriculum_notes()

    prompt = build_prompt(raw, history, concept, streak, reviews, notes)

    if args.dry_run:
        print(prompt)
        due = ", ".join(r["concept"] for r in reviews) or "none"
        print(f"\n--- prompt is {len(prompt):,} chars, concept #{idx+1}: {concept}"
              f"\n--- brief #{streak}, reviews due: {due}"
              f"\n--- schedule holds {len(schedule)} concept(s)", file=sys.stderr)
        return 0

    text = call_claude(prompt)

    # Pull the state block back out, then strip it from what the user sees.
    open_thread = ""
    sm = re.search(r"<!--STATE(.*?)-->", text, re.S)
    if sm:
        ot = re.search(r"open_thread:\s*(.+)", sm.group(1))
        if ot:
            open_thread = ot.group(1).strip().strip("[]")
        text = text[: sm.start()].rstrip()

    today = dt.datetime.now().strftime("%Y-%m-%d")
    (STATE / f"brief-{today}.md").write_text(text, encoding="utf-8")

    t = raw["markets"].get("treasury_10y", {})
    history.update({
        "concept_index": idx + 1,
        "streak": streak,
        "reit": raw["reit_symbol"],
        "schedule": advance_schedule(schedule, reviews, concept, streak),
        "last_entry": {
            "date": today,
            "treasury_10y": t.get("value"),
            "concept": concept,
            "reviewed": [r["concept"] for r in reviews],
            "open_thread": open_thread,
        },
    })
    history.setdefault("entries", []).append(history["last_entry"])
    history["entries"] = history["entries"][-90:]
    log_path.write_text(json.dumps(history, indent=2), encoding="utf-8")

    html_out = render_html(text, raw, concept)
    DOCS.mkdir(exist_ok=True)
    (DOCS / "archive").mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(html_out, encoding="utf-8")
    (DOCS / "archive" / f"{today}.html").write_text(html_out, encoding="utf-8")

    print(f"Brief #{streak} written. Concept: {concept}")
    print(f"  state/brief-{today}.md")
    print(f"  docs/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
