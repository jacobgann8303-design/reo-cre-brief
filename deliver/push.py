#!/usr/bin/env python3
"""
deliver/push.py — get the brief onto the phone.

Two channels, both optional, controlled entirely by environment variables:

  NTFY_TOPIC   push notification (free, no account, iOS + Android app)
  BRIEF_URL    the GitHub Pages link the notification opens

  SMTP_HOST / SMTP_USER / SMTP_PASS / MAIL_TO   email the full brief

If neither is configured this exits cleanly — a missing notification should
never fail the workflow that already produced a good brief.

    python deliver/push.py
    python deliver/push.py --test    # send a test ping and stop
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"


def latest_brief() -> tuple[str, str]:
    files = sorted(STATE.glob("brief-*.md"))
    if not files:
        sys.exit("No brief found. Run brief/generate.py first.")
    return files[-1].stem.replace("brief-", ""), files[-1].read_text()


def make_teaser(text: str, limit: int = 380) -> str:
    """
    Notification body: the numbers line plus the concept name. That's the part
    worth seeing on a lock screen — everything else needs the full page.
    """
    parts = []

    nums = re.search(r"##\s*The numbers\s*\n(.+?)(?=\n##|\Z)", text, re.S | re.I)
    if nums:
        body = re.sub(r"[*_`]", "", nums.group(1)).strip()
        lines = [l.strip("- ").strip() for l in body.split("\n") if l.strip()]
        parts.append(" · ".join(lines[:3]))

    concept = re.search(r"##\s*Today'?s concept:?\s*(.+)", text, re.I)
    if concept:
        parts.append(f"Today: {concept.group(1).strip()}")

    move = re.search(r"##\s*Your move\s*\n(.+?)(?=\n##|\Z)", text, re.S | re.I)
    if move:
        first = re.sub(r"[*_`]", "", move.group(1)).strip().split("\n")[0]
        parts.append(f"Do: {first}")

    out = "\n".join(parts) or text[:limit]
    return out[:limit].rstrip() + ("…" if len(out) > limit else "")


def send_ntfy(topic: str, title: str, body: str, url: str | None) -> bool:
    headers = {
        "Title": title.encode("utf-8"),
        "Tags": "chart_with_upwards_trend",
        "Priority": "default",
        "Markdown": "yes",
    }
    if url:
        # Tapping the notification opens the brief.
        headers["Click"] = url
        headers["Actions"] = f"view, Read the brief, {url}, clear=true"

    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = 200 <= r.status < 300
            print(f"ntfy: {'sent' if ok else 'HTTP ' + str(r.status)} -> topic '{topic}'")
            return ok
    except Exception as e:
        print(f"ntfy failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def send_email(subject: str, markdown_text: str) -> bool:
    import smtplib
    from email.message import EmailMessage

    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to = os.environ.get("MAIL_TO")
    if not all([host, user, password, to]):
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(markdown_text)

    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        print(f"email: sent -> {to}")
        return True
    except Exception as e:
        print(f"email failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    topic = os.environ.get("NTFY_TOPIC")
    url = os.environ.get("BRIEF_URL")

    if args.test:
        if not topic:
            sys.exit("Set NTFY_TOPIC first. See README, section 4.")
        ok = send_ntfy(topic, "The Tape — test",
                       "If this landed on your phone, notifications work.", url)
        return 0 if ok else 1

    date, text = latest_brief()
    pretty = dt.datetime.strptime(date, "%Y-%m-%d").strftime("%A, %B %-d")
    sent_any = False

    if topic:
        sent_any |= send_ntfy(topic, f"The Tape — {pretty}", make_teaser(text), url)
    else:
        print("NTFY_TOPIC not set — skipping push.")

    sent_any |= send_email(f"The Tape — {pretty}", text)

    if not sent_any:
        print("No delivery channel configured. The brief is still in state/ and docs/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
