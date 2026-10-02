# Ticket-drop tracker

Pulls US music + sports on-sales for the next 14 days from the Ticketmaster
Discovery API, scores each one 0–100 for resale potential, publishes a
mobile-first page, and pushes the best new ones to your phone.

Runs on GitHub Actions, so it works with your laptop closed. Standard-library
Python only — nothing to `pip install`.

## Files

| Path | What it is |
|---|---|
| `ticketdrops.py` | The whole tracker. |
| `config.json` | Thresholds, score weights, genre resale table. Safe to tune. |
| `venues.json` | Venue capacities. **Ticketmaster has none**, so we supply them. |
| `watchlist.json` | Artists that resell well. Checked outside the 14-day window and given a scoring bonus. |
| `.github/workflows/daily.yml` | The 12:40 UTC run, including the watchlist sync step. |
| `docs/index.html` | The published page. GitHub Pages serves this. |
| `docs/tickets.json` | Same data, machine-readable. |
| `docs/tickets-brief.md` | The block the morning brief renders. |
| `state.json` | Event ids already pushed, so you only get pinged once. Committed by CI. |
| `.env` / `.env.example` | Local secrets. `.env` is gitignored. |
| `run.sh` | Local/launchd entry point. |

## Running it by hand

```bash
cd ~/"sean personal life helper"/tickets
python3 ticketdrops.py --dry-run    # pull + score + write files, send nothing
python3 ticketdrops.py --test-ping  # one test notification, then exit
python3 ticketdrops.py --no-state   # re-push everything, ignore what's been seen
python3 ticketdrops.py --sync-watchlist  # fold in phone-pushed watchlist edits
python3 ticketdrops.py -v           # debug logging
```

Local runs write to `out/`. CI writes to `docs/`.

## Scoring

100 points, four components. Every component is written into `tickets.json`, so a
score can be argued with rather than trusted.

| Component | Max | What drives it |
|---|---|---|
| Demand | 40 | Upcoming dates for the act (log-scaled), plus **+12 if on your watchlist**. |
| Scarcity | 25 | Venue capacity, inverted — a 500-cap club scores far above a stadium. **No capacity at all scores 0**, not a free neutral. |
| Markup | 25 | Estimated resale multiple vs face. |
| Timing | 10 | Presales exist, and how soon the on-sale lands. |

`score_threshold` (60) governs the page and the morning brief.
`notify_threshold` (70) governs push only — see below.

### Measured distribution, 2026-10-01

898 real events in the window: **266 scored ≥60, 102 ≥65, 30 ≥70, 7 ≥75, 0 ≥80.**

Nothing reaches 80. That is not a bug — without live resale prices the score has
limited range, and the top end is reserved for when SeatGeek is wired in. Judge
picks relative to each other, not against 100.

### Three things the API cannot give you

**1. Venue capacity — not available anywhere.** Not on the events endpoint, not
on `/venues/{id}`, not for Madison Square Garden, SoFi Stadium or Sphere.
Verified. So `venues.json` supplies it: an exact table of ~60 well-known rooms,
then a coarse keyword guess from the venue's name ("Stadium" → 55,000,
"Theatre" → 2,200, "Club" → 800). Each event records whether its capacity was
`exact`, from `venue type`, or `unknown`. Add rooms you care about — it's just a
number in a JSON file.

**2. Sellout history — not exposed.** You asked for it as a scoring input and
Discovery simply doesn't carry it. Demand is proxied by the act's upcoming-date
count and presale count. Real sellout data needs SeatGeek or manual entry.

**3. Face prices are usually absent.** Ticketmaster withholds `priceRanges`
until the on-sale actually opens — on 2026-10-01 only 7 of 266 surfaced events
had one. That's why most rows read "Face TBA" and resale shows as a multiple
rather than dollars.

### The markup caveat

With SeatGeek off, the resale multiple is a **genre lookup table** in
`config.json`, nudged by routing and venue size. Good enough to rank events
against each other; **not** a number to underwrite a purchase with. That is why
every event currently reports `low` confidence.

Add `SEATGEEK_CLIENT_ID` and the markup component switches to real listing comps
with no code change — the hook is already in `estimate_resale_multiple()`.

## Watchlist

`watchlist.json`, seeded with Omar Adam. **Editable from the page**: tap ★ on any
act and the daily job folds it in.

How that works, since the page is static and has no backend: starring posts
`{"add":["Name"]}` (or `{"remove":[...]}`) to a **separate public ntfy topic**,
and the workflow's first step runs `--sync-watchlist`, which reads the topic and
rewrites `watchlist.json` before the pull. So a star you tap today is a tracked
artist from tomorrow's run.

That topic is embedded in the published page, so anyone who finds the page could
post to it. Two deliberate limits: it is a **different topic from your alert
feed** (which stays private), and the sync is **additive only** — a message can
add names or name removals but cannot replace the file, and the list caps at 50.
Worst case is junk entries you delete, not a wiped watchlist.

Entries take `aliases`, which matters here: Ticketmaster lists the seeded artist
as **Omer Adam**, and the "Omar" spelling returns nothing. Both are searched.

Watchlist artists are pulled by attraction id with **no on-sale date filter** —
the point is to catch them whenever they surface.

### Eventbrite

Wired but dormant. Eventbrite **retired public event search in 2019**;
`/v3/events/search/` returns 404 for everyone, and v3 now only exposes events for
organisations you own. The code calls it when `EVENTBRITE_TOKEN` is set, logs the
refusal once, and carries on. Your watchlist is Ticketmaster-only in practice.

## Notifications

ntfy. The topic is **not in this repo** — it lives in `.env` locally and in the
`NTFY_TOPIC` Actions secret. Anyone who knows the topic string can read your
alerts, which is what its random suffix is for.

Push is a tighter filter than the page:

- only events scoring **≥ `notify_threshold`** (70)
- **collapsed to one push per artist**, best-scoring date, with "+N more dates"
  — five pushes for one act on one tour is noise
- capped at `max_notifications_per_run` (6), then one "+N more acts" summary

On 2026-10-01 that turned 266 qualifying events into 9 acts and 7 pushes.

Tapping a notification opens the Ticketmaster page.

## Schedule and hosting

**GitHub Actions**, `40 12 * * *` (12:40 UTC), plus manual `workflow_dispatch`.
The job runs the tracker, commits `docs/` and `state.json`, and pushes. **GitHub
Pages** serves `docs/` — that's the URL you save to your home screen.

Two deliberate choices:

- **12:40 UTC, not 13:00.** GitHub's scheduler is best-effort and routinely runs
  late under load. The Morning Brief routine fetches the digest at 13:03 UTC, so
  the tracker needs a head start.
- **Cron is UTC and does not follow DST.** 12:40 UTC is 06:40 MDT and 05:40 MST.
  Same caveat already applies to your brief routines. Change to `40 13 * * *` if
  you want winter mornings to line up.

Scheduled workflows get disabled after 60 days of repo inactivity — this one
commits daily, so it stays awake.

`~/Library/LaunchAgents/com.sean.ticketdrops.plist` still exists but is
**unloaded**, to avoid double-notifying. To go back to local-only:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sean.ticketdrops.plist
```

## Morning brief

The brief routine runs in the cloud and cannot read these files, so the bridge is
ntfy: this script posts the digest to `<topic>-brief`, and the routine fetches
`https://ntfy.sh/<topic>-brief/json?poll=1` and renders a **Ticket drops
(tracked)** table after the Flip Radar section. If the digest is missing or
stale, the brief says "no digest today" rather than printing old picks.

## The page

Two tabs: **On sale today** and **Upcoming & presales**. Sort by score, on-sale or
concert date. Filter by score band, music/sports, watchlist, and **Best per act**
(on by default), which collapses a tour to its best date so one act can't fill the
screen.

Per act: **★** adds to the watchlist, **✕** hides it. Per show on the Upcoming tab:
the presale list with signup links where Ticketmaster provides them, and a
**Signed up** checkbox.

A **Status** row routes everything — *To do* / *Signed up* / *Trashed*. Ticking
"Signed up" moves that show out of To do and into Signed up; ✕ moves an act into
Trashed, where ✕ becomes an undo. All three live in `localStorage`, so they
survive the daily rebuild but are per-browser.

Note that signing up for one date does not hide the act — its other dates are
separate shows you have not registered for, so the next one takes its place.

## Presale access — why there are no codes here

The tracker does **not** look up or guess presale codes. Guessing is brute-forcing
an access control, and using a code you are not entitled to (Citi, fan club) is
misrepresenting eligibility — Ticketmaster cancels those orders and bans accounts,
which would cost the account this whole operation runs on.

It turns out that barely matters, because **almost none of these are code-gated**.
Measured across 1,460 live presales:

| Count | Access |
|---|---|
| 376 | Not published — assume a unique code |
| 288 | VIP package — open to all |
| 220 | Citi cardmember only |
| 199 | Artist's own list — sign up |
| 156 | Free — Live Nation account |
| 64 | Venue list — sign up |
| 60 | Verizon customers only |
| 42 | Amex cardmember only |
| 22 | Fan club members only |
| 21 | Spotify Premium only |
| 12 | Radio promo — code announced publicly |

Zero said "presale code required". They are gated by **who you are**, not by a
string you could find. So every presale carries an access label, and the
**Open to me** filter shows only shows with at least one presale you can enter
without a card or membership — 444 of 1,460.

The ones worth your time are **Live Nation** (free to join, and 144 of 145 carry a
signup link) and **VIP packages** (no gate at all).

## Gotchas worth knowing

- **Never add `sort=onSaleStartDate,asc` to the events query.** Ticketmaster
  stamps events with no public on-sale as `1900-01-01`, and that sort puts every
  one of them first — the entire first page comes back as sentinels. Measured:
  with that sort, 0/100 results were in-window; without it, 96/100 were. The
  query uses `onsaleOnAfterStartDate` plus a hard client-side window check.
- The on-sale window is also re-checked client-side in `onsale_in_window()`.
  The server-side filter alone is not trustworthy.
- Discovery paginates 1,000 results deep per query; `max_pages` is 5 × 200.
- Rate limit is 5 req/sec, 5,000/day. A run uses roughly 15.
