#!/usr/bin/env python3
"""
Moodle-Deadline-Wächter (Kalender-Variante)

Liest den exportierten Moodle-Kalender (iCal-Link) und schickt eskalierende
Push-Nachrichten über ntfy aufs Handy. Je näher die Deadline, desto lauter.
Jeder Push hat einen Knopf "✅ Abgegeben": danach ist für diese Aufgabe Ruhe.

Nur Standardbibliothek, kein pip install nötig.

Aufruf:
  python moodle_waechter.py            # normaler Lauf
  python moodle_waechter.py --test     # Test-Push ans Handy
  python moodle_waechter.py --dry-run  # nur anzeigen, nichts senden
  python moodle_waechter.py --list     # alle gefundenen Deadlines anzeigen
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------

def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


SCRIPT_DIR = Path(__file__).resolve().parent
load_env_file(SCRIPT_DIR / ".env")

CALENDAR_URL = os.environ.get("MOODLE_CALENDAR_URL", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
DONE_TOPIC = f"{NTFY_TOPIC}-done"
PAUSE_TOPIC = f"{NTFY_TOPIC}-pause"
# In den letzten 30 Minuten: zusätzlicher Push nach so vielen Minuten (innerhalb eines Laufs)
LAST_LEVEL_EXTRA_MIN = float(os.environ.get("LAST_LEVEL_EXTRA_MIN", "7.5"))
STATE_FILE = Path(os.environ.get("STATE_FILE", SCRIPT_DIR / "state.json"))
NAG_FROM_HOURS = float(os.environ.get("NAG_FROM_HOURS", "2"))
TZ = ZoneInfo("Europe/Zurich")

# Nur Termine, deren Titel eines dieser Wörter enthält, gelten als Abgabe.
# (Moodle schreibt z. B. "LB2 Abgabe ist fällig" oder "Quiz 3 schliesst")
DEADLINE_KEYWORDS = [
    w.strip().lower() for w in os.environ.get(
        "DEADLINE_KEYWORDS",
        "fällig,faellig,schliesst,schließt,abgabe,due,closes,deadline",
    ).split(",") if w.strip()
]

# Eskalationsstufen: (Stunden vor Deadline, ntfy-Priorität, Tags, Titel-Präfix)
LEVELS = [
    (24.0, 4, ["warning"],                         "⚠️ Morgen fällig"),
    (6.0,  5, ["rotating_light"],                  "🚨 Noch 6 Stunden"),
    (2.0,  5, ["rotating_light", "fire"],          "🚨🔥 NOCH 2 STUNDEN"),
    (0.5,  5, ["skull", "rotating_light", "fire"], "💀 LETZTE 30 MINUTEN"),
]


# --------------------------------------------------------------------------
# Kalender (iCal) lesen
# --------------------------------------------------------------------------

def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "moodle-waechter"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def unescape_ics(text: str) -> str:
    return (text.replace("\\n", "\n").replace("\\N", "\n")
                .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def parse_ics_datetime(value: str, params: str) -> int | None:
    value = value.strip()
    try:
        if "VALUE=DATE" in params.upper() and len(value) == 8:
            # Ganztägig: Ende des Tages in Zürich
            d = datetime.strptime(value, "%Y%m%d").replace(hour=23, minute=59, tzinfo=TZ)
            return int(d.timestamp())
        if value.endswith("Z"):
            d = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return int(d.timestamp())
        tz = TZ
        m = re.search(r"TZID=([^;:]+)", params)
        if m:
            try:
                tz = ZoneInfo(m.group(1))
            except Exception:
                tz = TZ
        d = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=tz)
        return int(d.timestamp())
    except ValueError:
        return None


def parse_ics(text: str) -> list[dict]:
    # Zeilen "entfalten" (Fortsetzungszeilen beginnen mit Leerzeichen/Tab)
    lines: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)

    events, current = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            key_part, value = line.split(":", 1)
            key, _, params = key_part.partition(";")
            current[key.upper()] = (value, params)

    result = []
    for ev in events:
        if "DTSTART" not in ev:
            continue
        due = parse_ics_datetime(ev["DTSTART"][0], ev["DTSTART"][1])
        if due is None:
            continue
        summary = unescape_ics(ev.get("SUMMARY", ("Termin", ""))[0])
        result.append({
            "id": ev.get("UID", (f"{summary}-{due}", ""))[0],
            "name": summary,
            "course": unescape_ics(ev.get("CATEGORIES", ("", ""))[0]),
            "due": due,
            "url": ev.get("URL", ("", ""))[0],
        })
    return result


def is_deadline(event: dict) -> bool:
    name = event["name"].lower()
    return any(k in name for k in DEADLINE_KEYWORDS)


# --------------------------------------------------------------------------
# ntfy
# --------------------------------------------------------------------------

def send_push(title: str, message: str, priority: int, tags: list[str],
              event_id: str | None = None, click: str | None = None,
              dry_run: bool = False) -> None:
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags,
    }
    actions = []
    if event_id:
        # Knopf "Abgegeben": schickt die Aufgaben-ID an das -done-Topic
        actions.append({
            "action": "http",
            "label": "✅ Abgegeben",
            "url": f"{NTFY_SERVER}/{DONE_TOPIC}",
            "method": "POST",
            "body": event_id,
            "clear": True,
        })
    if click:
        payload["click"] = click
        actions.append({"action": "view", "label": "Moodle öffnen", "url": click})
    if actions:
        payload["actions"] = actions

    if dry_run:
        print("[DRY-RUN]", json.dumps(payload, ensure_ascii=False))
        return
    req = urllib.request.Request(
        NTFY_SERVER,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def fetch_topic_messages(topic: str) -> list[tuple[int, str]]:
    """Alle gespeicherten Nachrichten eines ntfy-Topics als (Zeit, Text), älteste zuerst."""
    url = f"{NTFY_SERVER}/{topic}/json?poll=1&since=all"
    try:
        text = http_get(url)
    except Exception as e:
        print(f"Konnte Topic {topic} nicht lesen: {e}")
        return []
    result = []
    for line in text.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("event") == "message" and msg.get("message"):
            result.append((int(msg.get("time", 0)), msg["message"].strip()))
    return sorted(result)


def fetch_done_ids() -> set[str]:
    """Liest, welche Aufgaben per Knopf als abgegeben markiert wurden."""
    return {m for _, m in fetch_topic_messages(DONE_TOPIC)}


def update_pause(state: dict) -> bool:
    """Pause-Schalter (Maustaste am PC): gilt nur für den heutigen Tag.
    Ab Mitternacht ist der Wächter automatisch wieder aktiv."""
    messages = fetch_topic_messages(PAUSE_TOPIC)
    if messages:
        ts, text = messages[-1]  # die neueste Nachricht entscheidet
        if text.lower() == "pause":
            state["pause_day"] = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")
        else:
            state["pause_day"] = ""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    return state.get("pause_day") == today


# --------------------------------------------------------------------------
# Zustand
# --------------------------------------------------------------------------

def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("levels", {})
    state.setdefault("done", {})
    state.setdefault("last_error_day", "")
    state.setdefault("pause_day", "")
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Logik
# --------------------------------------------------------------------------

def level_for(hours_left: float) -> int | None:
    reached = None
    for i, (threshold, *_rest) in enumerate(LEVELS):
        if hours_left <= threshold:
            reached = i
    return reached


def format_left(seconds: int) -> str:
    seconds = max(seconds, 0)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h} h {m:02d} min" if h else f"{m} min"


def report_error(state: dict, text: str, dry_run: bool) -> None:
    """Fehler höchstens einmal pro Tag melden, damit du merkst, wenn etwas kaputt ist."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    print(f"FEHLER: {text}")
    if state.get("last_error_day") != today:
        try:
            send_push("🛠️ Moodle-Wächter funktioniert nicht",
                      f"{text}\n\nPrüfe den Kalender-Link (Secret MOODLE_CALENDAR_URL).",
                      4, ["warning"], dry_run=dry_run)
            state["last_error_day"] = today
        except Exception as e:
            print(f"Konnte Fehler-Push nicht senden: {e}")


def push_round(deadlines: list[dict], state: dict, now: int, dry_run: bool,
               force: bool = False) -> set[str]:
    """Schickt fällige Pushes. Gibt die IDs zurück, die in der letzten Stufe sind."""
    last_level_ids = set()
    for d in deadlines:
        seconds_left = d["due"] - now
        if seconds_left <= 0 or d["id"] in state["done"]:
            continue
        lvl = level_for(seconds_left / 3600)
        if lvl is None:
            continue
        if lvl == len(LEVELS) - 1:
            last_level_ids.add(d["id"])

        last_lvl = state["levels"].get(d["id"], -1)
        nag = seconds_left <= NAG_FROM_HOURS * 3600
        if lvl <= last_lvl and not nag and not force:
            continue

        _, priority, tags, prefix = LEVELS[lvl]
        due_str = datetime.fromtimestamp(d["due"], TZ).strftime("%a %d.%m. %H:%M")
        title = f"{prefix}: {d['name']}"
        message = (f"{d['course']}\n"
                   f"Fällig: {due_str} (noch {format_left(seconds_left)})\n"
                   f"Abgegeben? Tippe auf «✅ Abgegeben», dann ist Ruhe.")
        send_push(title, message, priority, tags,
                  event_id=d["id"], click=d["url"] or None, dry_run=dry_run)
        print(f"Push: {title} | {due_str}")
        state["levels"][d["id"]] = lvl
    return last_level_ids


def main() -> None:
    args = set(sys.argv[1:])
    dry_run = "--dry-run" in args

    if not NTFY_TOPIC:
        sys.exit("Fehler: NTFY_TOPIC setzen.")

    if "--test" in args:
        send_push("🚨 Moodle-Wächter Test", "Wenn du das siehst, funktioniert der Push.",
                  5, ["rotating_light"], event_id="test", dry_run=dry_run)
        print("Test-Push gesendet.")
        return

    if not CALENDAR_URL:
        sys.exit("Fehler: MOODLE_CALENDAR_URL setzen.")

    state = load_state()
    now = int(time.time())

    try:
        ics = http_get(CALENDAR_URL)
        if "BEGIN:VCALENDAR" not in ics:
            raise ValueError("Antwort ist kein Kalender (Link abgelaufen oder ungültig?)")
        events = parse_ics(ics)
    except Exception as e:
        report_error(state, f"Kalender konnte nicht geladen werden: {e}", dry_run)
        if not dry_run:
            save_state(state)
        sys.exit(1)

    deadlines = [e for e in events if is_deadline(e)]

    if "--list" in args:
        for d in sorted(events, key=lambda x: x["due"]):
            mark = "✓ Deadline" if is_deadline(d) else "  ignoriert"
            when = datetime.fromtimestamp(d["due"], TZ).strftime("%d.%m.%Y %H:%M")
            print(f"{mark} | {when} | {d['course']} | {d['name']}")
        return

    # Per Knopf als abgegeben markierte Aufgaben merken
    for done_id in fetch_done_ids():
        state["done"].setdefault(done_id, now)

    if update_pause(state):
        print("Pausiert (Maustaste) – heute keine Pushes. Ab Mitternacht wieder aktiv.")
    else:
        last_level_ids = push_round(deadlines, state, now, dry_run)

        # Letzte 30 Minuten: nach 7.5 Minuten nochmals pushen (GitHub läuft nur alle 15 Min)
        if last_level_ids and "--no-wait" not in args:
            wait = int(LAST_LEVEL_EXTRA_MIN * 60)
            print(f"Letzte 30 Minuten: warte {LAST_LEVEL_EXTRA_MIN:g} Min für den Extra-Push …")
            time.sleep(0 if dry_run else wait)
            now2 = int(time.time())
            for done_id in fetch_done_ids():
                state["done"].setdefault(done_id, now2)
            if not update_pause(state):
                push_round([d for d in deadlines if d["id"] in last_level_ids],
                           state, now2, dry_run, force=True)

    # Aufräumen: Einträge älter als 30 Tage weg
    upcoming = {d["id"] for d in deadlines if d["due"] > now - 86400}
    state["levels"] = {k: v for k, v in state["levels"].items() if k in upcoming}
    state["done"] = {k: v for k, v in state["done"].items() if v > now - 30 * 86400}
    state["last_error_day"] = ""  # alles ok

    if not dry_run:
        save_state(state)
    if not deadlines:
        print("Keine Abgaben im Kalender gefunden.")


if __name__ == "__main__":
    main()
