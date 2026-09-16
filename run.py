#!/usr/bin/env python3
import os
import sys
import time
import json
import base64
import signal
import sqlite3
import logging
import requests
import settlements
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timedelta

REPO = Path(__file__).parent
DB_FILE = Path(os.environ.get("DB_FILE") or REPO / "settlements.db")
AVATAR_FILE = REPO / "gavel.png"
WEBHOOK_NAME = "Class Action Alert"
PROJECT_URL = "https://github.com/zeusec/rss-class-actions"
PROJECT_LABEL = "GitHub"
ATTRIBUTION = (f"[ClassAction]({settlements.CAORG_URL})"
               f" • [Sparrow]({settlements.SPARROW_URL})"
               f" • [{PROJECT_LABEL}]({PROJECT_URL})")
DEFAULT_SCAN_AT = "12:00"
EMBED_COLOR = 0x0F9129
HTTP_TIMEOUT = 30
FAR_FUTURE = "9999-12-31"

log = logging.getLogger("class-action-settlements")
_running = True
_dry_run = False

def _stop(*_):
    global _running
    _running = False

def open_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (guid TEXT PRIMARY KEY, posted_at INTEGER)")
    return conn

def mark_seen(conn, guids):
    now = int(time.time())
    conn.executemany("INSERT OR IGNORE INTO seen VALUES (?, ?)", [(g, now) for g in guids])

def sort_key(item):
    deadline = settlements.parse_deadline(item[1]["deadline"])
    return (deadline.isoformat() if deadline else FAR_FUTURE, item[1]["name"])

def set_webhook_identity(webhook):
    avatar = base64.b64encode(AVATAR_FILE.read_bytes()).decode()
    payload = {"name": WEBHOOK_NAME, "avatar": f"data:image/png;base64,{avatar}"}
    try:
        r = requests.patch(webhook, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            log.warning("webhook patch %d: %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("webhook patch failed: %s", e)

def build_embed(rec):
    claim = [f"[File directly]({rec['official']})"] if rec["official"] else []
    if rec["sparrow"]:
        claim.append(f"[File via Sparrow]({rec['sparrow']})")
    description = " • ".join(claim)
    if rec["summary"]:
        description = f"{rec['summary'][:300]}\n\n{description}"
    return {
        "author": {"name": "New Class Action Settlement"},
        "title": rec["name"][:256],
        "url": rec["official"],
        "description": description[:2048],
        "color": EMBED_COLOR,
        "fields": [
            {"name": "Estimated payout", "value": rec["payout"] or "Varies", "inline": True},
            {"name": "Deadline", "value": rec["deadline"] or "Varies", "inline": True},
            {"name": "Proof required", "value": rec["proof"] or "Unknown", "inline": True},
            {"name": "​", "value": ATTRIBUTION, "inline": False},
        ],
    }

def header_float(headers, name, default):
    try:
        return float(headers.get(name, default))
    except (TypeError, ValueError):
        return float(default)

def post_to_discord(webhook, embed):
    if _dry_run:
        log.info("DRY RUN would post: %s", json.dumps(embed, indent=2))
        return True
    while _running:
        try:
            r = requests.post(webhook, json={"embeds": [embed]}, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            log.warning("discord post failed: %s", e)
            return False
        if r.status_code == 429:
            time.sleep(header_float(r.headers, "Retry-After", 1))
            continue
        if r.status_code >= 400:
            log.warning("discord %d: %s", r.status_code, r.text[:200])
            return False
        if header_float(r.headers, "X-RateLimit-Remaining", 1) <= 0:
            time.sleep(header_float(r.headers, "X-RateLimit-Reset-After", 0))
        return True
    return False

def post_all(conn, webhook, records):
    posted = 0
    for key, rec in records:
        if not _running:
            break
        if post_to_discord(webhook, build_embed(rec)):
            mark_seen(conn, [key])
            conn.commit()
            posted += 1
    return posted

def replay(conn, webhook, merged, days):
    cutoff = int(time.time()) - days * 86400
    seen_at = dict(conn.execute("SELECT guid, posted_at FROM seen WHERE posted_at >= ?", (cutoff,)))
    window = [(k, merged[k]) for k in seen_at if k in merged]
    window.sort(key=lambda item: (seen_at[item[0]], sort_key(item)))
    log.info("replay: %d settlements first seen in the last %d day(s)", len(window), days)
    post_all(conn, webhook, window)

def scan(conn, webhook, merged, complete):
    if conn.execute("SELECT 1 FROM seen LIMIT 1").fetchone() is None:
        if not complete:
            log.warning("not seeding: a source failed, and a partial seed would flood "
                        "the channel with everything it missed once it recovers")
            return
        mark_seen(conn, list(merged))
        conn.commit()
        log.info("first-encounter seeded %d settlements, posted 0", len(merged))
        return
    existing = {row[0] for row in conn.execute("SELECT guid FROM seen")}
    new = sorted(((k, r) for k, r in merged.items() if k not in existing), key=sort_key)
    posted = post_all(conn, webhook, new)
    log.info("posted %d of %d new settlements", posted, len(new))

def seconds_until(scan_at):
    hour, minute = (int(part) for part in scan_at.split(":"))
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

def main():
    global _dry_run
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(REPO / ".env")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    _dry_run = "--dry-run" in sys.argv or not webhook
    scan_at = os.environ.get("SCAN_AT") or DEFAULT_SCAN_AT
    try:
        seconds_until(scan_at)
    except ValueError:
        sys.exit(f"SCAN_AT must be HH:MM in 24-hour time, got {scan_at!r}")
    lookback_raw = os.environ.get("LOOKBACK_DAYS") or "0"
    if not lookback_raw.isdigit():
        sys.exit(f"LOOKBACK_DAYS must be a whole number of days, got {lookback_raw!r}")
    lookback_days = int(lookback_raw)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    conn = open_db()
    if not _dry_run:
        set_webhook_identity(webhook)
    log.info("starting: db=%s, scan_at=%s, lookback_days=%d%s", DB_FILE, scan_at, lookback_days,
             ", DRY RUN (no webhook set)" if not webhook else ", DRY RUN" if _dry_run else "")
    while _running:
        merged, complete = settlements.collect()
        if merged:
            scan(conn, webhook, merged, complete)
            if lookback_days:
                replay(conn, webhook, merged, lookback_days)
                lookback_days = 0
        for _ in range(int(seconds_until(scan_at))):
            if not _running:
                break
            time.sleep(1)
    conn.close()

if __name__ == "__main__":
    main()
