"""Poll the Trusted Traveler Programs scheduler API for earlier NEXUS slots and push alerts."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

TTP_BASE = "https://ttp.cbp.dhs.gov/schedulerapi"
USER_AGENT = "nexus-notifications/1.0 (personal use)"

log = logging.getLogger("nexus")


@dataclass
class Config:
    location_id: int
    current_appt: datetime
    ntfy_topic: str
    poll_interval_s: int
    state_path: Path
    allowed_weekdays: set[int]
    min_date: datetime


def load_config() -> Config:
    load_dotenv()
    try:
        current_appt = datetime.fromisoformat(os.environ["CURRENT_APPOINTMENT"])
    except (KeyError, ValueError) as e:
        sys.exit(f"CURRENT_APPOINTMENT must be ISO8601 (e.g. 2026-06-08T09:00): {e}")
    if current_appt.tzinfo is None:
        current_appt = current_appt.replace(tzinfo=timezone.utc)
    try:
        min_date = datetime.fromisoformat(os.environ.get("MIN_DATE", "2026-01-01T00:00"))
    except ValueError as e:
        sys.exit(f"MIN_DATE must be ISO8601 (e.g. 2026-05-01T00:00): {e}")
    if min_date.tzinfo is None:
        min_date = min_date.replace(tzinfo=timezone.utc)
    # Parse allowed weekdays: comma-separated, 1=Mon, 2=Tue, ..., 7=Sun
    weekdays_str = os.environ.get("ALLOWED_WEEKDAYS", "5,6,7,1")  # Fri, Sat, Sun, Mon
    try:
        allowed_weekdays = {int(d.strip()) for d in weekdays_str.split(",")}
        if not (allowed_weekdays <= {1, 2, 3, 4, 5, 6, 7}):
            raise ValueError("weekdays must be 1-7")
    except ValueError as e:
        sys.exit(f"ALLOWED_WEEKDAYS must be comma-separated 1-7 (1=Mon, 7=Sun): {e}")
    return Config(
        location_id=int(os.environ["LOCATION_ID"]),
        current_appt=current_appt,
        ntfy_topic=os.environ["NTFY_TOPIC"],
        poll_interval_s=int(os.environ.get("POLL_INTERVAL_S", "120")),
        state_path=Path(os.environ.get("STATE_PATH", "state.json")),
        allowed_weekdays=allowed_weekdays,
        min_date=min_date,
    )


def fetch_slots(location_id: int) -> list[dict]:
    r = requests.get(
        f"{TTP_BASE}/slots",
        params={"orderBy": "soonest", "limit": 50, "locationId": location_id, "minimum": 1},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def fetch_nexus_locations() -> list[dict]:
    r = requests.get(
        f"{TTP_BASE}/locations",
        params={"temporary": "false", "inviteOnly": "false", "operational": "true", "serviceName": "NEXUS"},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def parse_slot_time(ts: str) -> datetime:
    # TTP returns naive local times like "2026-05-15T09:00". Treat as naive-UTC for ordering only.
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)


def filter_by_day_and_date(slot_time: datetime, allowed_weekdays: set[int], min_date: datetime) -> bool:
    # weekday(): Mon=0, Tue=1, ..., Sun=6. Convert to: Mon=1, ..., Sun=7 for readability.
    weekday = (slot_time.weekday() + 1) % 7 or 7  # 0->7 (Sun), 1->1 (Mon), etc.
    return slot_time.date() >= min_date.date() and weekday in allowed_weekdays


def earlier_slots(slots: list[dict], current_appt: datetime, allowed_weekdays: set[int] | None = None, min_date: datetime | None = None) -> list[dict]:
    if allowed_weekdays is None:
        allowed_weekdays = {1, 2, 3, 4, 5, 6, 7}  # All days by default.
    if min_date is None:
        min_date = datetime.min.replace(tzinfo=timezone.utc)
    out = []
    for s in slots:
        ts = s.get("startTimestamp")
        if not ts or not s.get("active", True):
            continue
        slot_time = parse_slot_time(ts)
        if slot_time < current_appt and filter_by_day_and_date(slot_time, allowed_weekdays, min_date):
            out.append(s)
    return out


def load_known(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def save_known(path: Path, known: set[str]) -> None:
    path.write_text(json.dumps(sorted(known)))


def notify_ntfy(cfg: Config, title: str, body: str) -> None:
    r = requests.post(
        f"https://ntfy.sh/{cfg.ntfy_topic}",
        headers={"Title": title, "Priority": "high"},
        data=body.encode("utf-8"),
        timeout=10,
    )
    r.raise_for_status()


def run_poll(cfg: Config) -> None:
    log.info("Polling locationId=%s every %ss; current appt=%s; weekdays=%s; min_date=%s", cfg.location_id, cfg.poll_interval_s, cfg.current_appt.isoformat(), sorted(cfg.allowed_weekdays), cfg.min_date.date())
    known = load_known(cfg.state_path)
    while True:
        try:
            slots = fetch_slots(cfg.location_id)
            earlier = earlier_slots(slots, cfg.current_appt, cfg.allowed_weekdays, cfg.min_date)
            current_keys = {s["startTimestamp"] for s in earlier}
            new_keys = current_keys - known
            if new_keys:
                new_sorted = sorted(new_keys)
                log.info("Found %d new earlier slot(s): %s", len(new_sorted), new_sorted)
                body = "\n".join(new_sorted)
                notify_ntfy(cfg, "NEXUS slot available", body + "\n\nBook: https://ttp.cbp.dhs.gov/schedulerui/")
                known |= new_keys
                save_known(cfg.state_path, known)
            else:
                log.debug("No new earlier slots (visible earlier=%d)", len(earlier))
            # Forget disappeared slots so re-appearances trigger a new alert.
            gone = known - current_keys
            if gone:
                known -= gone
                save_known(cfg.state_path, known)
        except requests.HTTPError as e:
            log.warning("HTTP error: %s", e)
        except requests.RequestException as e:
            log.warning("Network error: %s", e)
        time.sleep(cfg.poll_interval_s)


def cmd_list_locations(_: argparse.Namespace) -> None:
    for loc in sorted(fetch_nexus_locations(), key=lambda l: (l.get("state") or "", l.get("name") or "")):
        print(f"{loc['id']:>6}  {loc.get('state',''):>3}  {loc.get('name','')}  —  {loc.get('city','')}")


def cmd_poll(_: argparse.Namespace) -> None:
    run_poll(load_config())


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="NEXUS interview slot notifier")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll", help="Run the polling loop").set_defaults(func=cmd_poll)
    sub.add_parser("list-locations", help="List NEXUS enrollment centers with IDs").set_defaults(func=cmd_list_locations)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
