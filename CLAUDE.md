# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Local dev (the repo ships a `venv/`; activate it or use `python3` directly):

- Install deps: `pip install -r requirements.txt`
- Run the poller: `python main.py poll` (requires a populated `.env`; copy from `.env.example`)
- List NEXUS enrollment centers + IDs: `python main.py list-locations`
- Run all tests: `pytest`
- Run a single test: `pytest test_main.py::TestEarlierSlots::test_filter_earlier_only`

Docker:

- Build + run in background: `docker compose up -d --build`
- Tail logs: `docker compose logs -f`
- State persists to `./data/state.json` on the host (mounted to `/data` in the container).

## Architecture

Single-file polling daemon (`main.py`) that watches the CBP Trusted Traveler Programs scheduler API for NEXUS interview slots earlier than your current booked appointment and pushes alerts via [ntfy.sh](https://ntfy.sh).

Flow inside `run_poll`:

1. `fetch_slots(location_id)` → GET `https://ttp.cbp.dhs.gov/schedulerapi/slots` (no auth, public).
2. `earlier_slots(...)` filters to active slots strictly before `CURRENT_APPOINTMENT`, then applies `filter_by_day_and_date` (weekday allow-list ∩ `MIN_DATE` floor).
3. Diff against `known` set loaded from `STATE_PATH` (JSON list of `startTimestamp` strings). New keys → one ntfy POST containing all of them; persist union back to disk.
4. Slots that *disappeared* from the API are removed from `known` so a later re-appearance re-triggers an alert. This is intentional — books happen fast and the slot you missed today might come back tomorrow.

Things that look weird but aren't bugs:

- TTP returns **naive** local timestamps (e.g. `2026-05-15T09:00`); `parse_slot_time` slaps `tzinfo=UTC` on them. Comparisons are correct because `CURRENT_APPOINTMENT` and `MIN_DATE` go through the same naive→UTC coercion in `load_config`. **Don't** try to convert TTP times to a real timezone — the values aren't actually UTC, they're enrollment-center local, and there's no offset in the payload.
- Weekday config uses **1=Mon … 7=Sun** (matches ISO), not Python's `weekday()` 0=Mon. The conversion lives in `filter_by_day_and_date`.
- Notifier history: the repo previously used Pushover, then Twilio SMS, now ntfy. If you see commits or issues referencing those, ntfy is the current backend — don't reintroduce the old ones.

## Configuration

All runtime config comes from environment variables (loaded via `python-dotenv`). See `.env.example` for the full list. Required: `LOCATION_ID`, `CURRENT_APPOINTMENT`, `NTFY_TOPIC`. The ntfy topic is a shared secret — anyone who knows it can subscribe, so pick something unguessable.
