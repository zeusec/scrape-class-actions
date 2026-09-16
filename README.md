# rss-class-actions

Scrapes class-action settlements once a day and posts new ones to a Discord webhook. Each post has the estimated payout, the claim deadline, whether you need proof, and a link to file directly with the settlement administrator.

## Sources

| Site | What it contributes |
|---|---|
| [classaction.org/settlements](https://www.classaction.org/settlements) | Primary source, each with a payout, deadline, proof flag, and a link to the administrator |
| [usesparrow.com/class-actions](https://usesparrow.com/class-actions) | Mostly overlaps classaction.org, but often has a real dollar figure where classaction.org says "Varies" |

The two are merged on the administrator's domain, since that's the only identifier both publish. 
This is raw HTML scraping,  ~14 requests a day total.

## Running

```sh
cp .env.example .env   # add your Discord webhook URL
docker compose up -d --build
docker compose logs -f
```

Or without Docker: `uv sync && uv run run.py`. State lives in `settlements.db` (under `./data/` with Docker) and survives rebuilds.

## How it behaves

It scrapes once at startup and then daily at `SCAN_AT`. On the very first run it marks everything currently listed as seen and posts nothing, so a fresh database won't flood your channel. After that, only settlements that show up later get posted, soonest deadline first.

To warm up production, run it once with no webhook set. It'll seed the database and log what it would have posted. Then restart with the webhook and only new settlements go out.

`LOOKBACK_DAYS=N` re-posts everything the bot first saw in the last N days, then goes back to normal. Neither site publishes a date added, so this works off the bot's own records. Set it to 1 right after seeding and you'll get the whole current list replayed, which is useful for checking formatting in a test channel.

## Environment

- `DISCORD_WEBHOOK_URL`: where posts go. Leave it unset and the bot dry-runs, logging instead of posting.
- `SCAN_AT=12:00`: daily scrape time, 24-hour `HH:MM`, in the container's local time.
- `TZ`: set this in `docker-compose.yml` so `SCAN_AT` means what you think it means.
- `LOOKBACK_DAYS=0`: replay window in days, applied once at startup.
- `DB_FILE`: database path. Read from the real environment only, not `.env`. Docker sets it to `/data/settlements.db`.

`--dry-run` logs the embeds instead of posting them.
