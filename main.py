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
    pushover_token: str
    pushover_user: str
    poll_interval_s: int
    state_path: Path


def load_config() -> Config:
    load_dotenv()
    try:
        current_appt = datetime.fromisoformat(os.environ["CURRENT_APPOINTMENT"])
    except (KeyError, ValueError) as e:
        sys.exit(f"CURRENT_APPOINTMENT must be ISO8601 (e.g. 2026-06-08T09:00): {e}")
    if current_appt.tzinfo is None:
        current_appt = current_appt.replace(tzinfo=timezone.utc)
    return Config(
        location_id=int(os.environ["LOCATION_ID"]),
        current_appt=current_appt,
        pushover_token=os.environ["PUSHOVER_TOKEN"],
        pushover_user=os.environ["PUSHOVER_USER"],
        poll_interval_s=int(os.environ.get("POLL_INTERVAL_S", "120")),
        state_path=Path(os.environ.get("STATE_PATH", "state.json")),
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


def earlier_slots(slots: list[dict], current_appt: datetime) -> list[dict]:
    out = []
    for s in slots:
        ts = s.get("startTimestamp")
        if not ts or not s.get("active", True):
            continue
        if parse_slot_time(ts) < current_appt:
            out.append(s)
    return out


def load_known(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(json.loads(path.read_text()))


def save_known(path: Path, known: set[str]) -> None:
    path.write_text(json.dumps(sorted(known)))


def notify_pushover(token: str, user: str, title: str, message: str) -> None:
    r = requests.post(
        "https://api.pushover.net/1/messages.json",
        data={"token": token, "user": user, "title": title, "message": message, "priority": 1},
        timeout=10,
    )
    r.raise_for_status()


def run_poll(cfg: Config) -> None:
    log.info("Polling locationId=%s every %ss; current appt=%s", cfg.location_id, cfg.poll_interval_s, cfg.current_appt.isoformat())
    known = load_known(cfg.state_path)
    while True:
        try:
            slots = fetch_slots(cfg.location_id)
            earlier = earlier_slots(slots, cfg.current_appt)
            current_keys = {s["startTimestamp"] for s in earlier}
            new_keys = current_keys - known
            if new_keys:
                new_sorted = sorted(new_keys)
                log.info("Found %d new earlier slot(s): %s", len(new_sorted), new_sorted)
                msg = "Earlier NEXUS slot(s) open at Blaine:\n" + "\n".join(new_sorted)
                msg += f"\n\nBook: https://ttp.cbp.dhs.gov/schedulerui/schedule-interview/location?lang=en&vo=true&returnUrl=ttp-external&service=nexus"
                notify_pushover(cfg.pushover_token, cfg.pushover_user, "NEXUS slot available", msg)
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
