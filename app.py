#!/usr/bin/env python3
"""
Lüftersteuerung Rack – Backend
--------------------------------
- Zeigt/schaltet den Status der 4 Lüfter (gpioset/gpioget)
- Verwaltet den stündlichen An/Aus-Cronjob (crontab)
- NEU: Automatik-Pause – jede manuelle Schaltung (einzeln oder Master)
  kommentiert den Cronjob automatisch aus ("# STATUS: DISABLED"), damit
  der nächste stündliche Cron-Trigger die manuelle Einstellung nicht
  sofort wieder überschreibt. Die hinterlegten Uhrzeiten bleiben dabei
  erhalten und können jederzeit über den "Automatik"-Schalter wieder
  aktiviert werden.
"""

import os
import json
import datetime
import logging
import threading
import time
import uuid
from flask import Flask, jsonify, request, render_template
import subprocess

app = Flask(__name__)

# Verhindert, dass JEDE einzelne Statusabfrage (alle 4s vom Frontend)
# als eigene Logzeile im systemd-Journal landet - nur echte Fehler
# werden noch protokolliert.
logging.getLogger("werkzeug").setLevel(logging.ERROR)

STATE_FILE = os.environ.get(
    "STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fan_state.json"),
)
EXCLUDED_FILE = os.environ.get(
    "EXCLUDED_PINS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "excluded_pins.json"),
)
RUNTIME_FILE = os.environ.get(
    "RUNTIME_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fan_runtime.json"),
)
SEQUENCES_FILE = os.environ.get(
    "SEQUENCES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sequences.json"),
)

GPIOCHIP = os.environ.get("GPIOCHIP", "0")
PIN_ORDER = ["17", "27", "22", "23"]
PIN_LABELS = {
    "17": "Lüfter 1",
    "27": "Lüfter 2",
    "22": "Lüfter 3",
    "23": "Lüfter 4",
}

# Setze USE_SUDO=false in der Umgebung, wenn die App bereits als root läuft
# (z.B. im Docker-Container). Standard: sudo wird verwendet.
SUDO = [] if os.environ.get("USE_SUDO", "true").lower() == "false" else ["sudo"]

CRON_START = "# LUEFTERSTEUERUNG RACK - START"
CRON_END = "# LUEFTERSTEUERUNG RACK - END"

DEFAULT_ON_MINUTE = 25
DEFAULT_OFF_MINUTE = 40


def run_cmd(cmd, input_text=None):
    return subprocess.run(cmd, capture_output=True, text=True, input=input_text)


def _line_is_on_command(content):
    """Erkennt eine 'Anschalten'-Cronzeile, unabhängig davon, wie viele
    der 4 Pins gerade tatsächlich enthalten sind (manche können durch
    manuelle Ausnahmen fehlen)."""
    return (
        "gpioset" in content
        and any(f"{p}=1" in content for p in PIN_ORDER)
        and not any(f"{p}=0" in content for p in PIN_ORDER)
    )


def _line_is_off_command(content):
    """Erkennt eine 'Ausschalten'-Cronzeile, unabhängig davon, wie viele
    der 4 Pins gerade tatsächlich enthalten sind."""
    return (
        "gpioset" in content
        and any(f"{p}=0" in content for p in PIN_ORDER)
        and not any(f"{p}=1" in content for p in PIN_ORDER)
    )


def read_crontab_text():
    result = run_cmd(SUDO + ["crontab", "-l"])
    return result.stdout if result.returncode == 0 else ""


def parse_cron_state(crontab_text):
    """Liest Status/Uhrzeiten aus expliziten Marker-Kommentarzeilen
    (# STATUS/# ON_MINUTE/# OFF_MINUTE), die IMMER geschrieben werden -
    unabhängig davon, wie viele Pins der eigentliche gpioset-Befehl
    gerade enthält. Das verhindert, dass die Info verloren geht, wenn
    z.B. gerade alle 4 Pins temporär ausgeschlossen sind."""
    on_minute = off_minute = None
    enabled = True
    found_any = False
    inside = False

    for raw in crontab_text.splitlines():
        stripped = raw.strip()
        if stripped == CRON_START:
            inside = True
            found_any = True
            continue
        if stripped == CRON_END:
            inside = False
            continue
        if not inside:
            continue

        if stripped.startswith("# STATUS:"):
            enabled = "ENABLED" in stripped
        elif stripped.startswith("# ON_MINUTE:"):
            try:
                on_minute = str(int(stripped.split(":", 1)[1].strip()))
            except (ValueError, IndexError):
                pass
        elif stripped.startswith("# OFF_MINUTE:"):
            try:
                off_minute = str(int(stripped.split(":", 1)[1].strip()))
            except (ValueError, IndexError):
                pass

    return {
        "on_minute": on_minute,
        "off_minute": off_minute,
        "enabled": enabled,
        "found": found_any,
    }


def remove_managed_blocks(lines):
    """Entfernt JEDEN kompletten Block zwischen START- und END-Marker als
    Ganzes (nicht zeilenweise!) - robust gegen alte/kaputte/mehrfach
    gestapelte Blöcke, egal was genau dazwischen steht (inkl. STATUS-Zeile)."""
    result = []
    inside = False
    for raw in lines:
        stripped = raw.strip()
        if stripped == CRON_START:
            inside = True
            continue
        if stripped == CRON_END:
            inside = False
            continue
        if inside:
            continue
        result.append(raw)
    return result


def remove_legacy_fan_lines(lines):
    """Entfernt einzelne Lüfter-Cronzeilen/-Kommentare, die NICHT in einem
    verwalteten Block stehen (z.B. dein ursprünglicher manueller Eintrag
    von vor der App)."""
    cleaned = []
    for raw in lines:
        stripped = raw.strip()
        upper = stripped.upper()
        if "LUEFTERSTEUERUNG" in upper or "LÜFTERSTEUERUNG" in upper:
            continue
        if stripped.startswith("#") and "UHR" in upper and ("LUEFTER" in upper or "LÜFTER" in upper):
            continue
        content = stripped.lstrip("#").strip()
        if _line_is_on_command(content) or _line_is_off_command(content):
            continue
        cleaned.append(raw)
    return cleaned


def strip_fan_cron_lines(lines):
    """Entfernt zuverlässig ALLE Spuren früherer Lüftersteuerungs-Einträge:
    zuerst komplette verwaltete Blöcke, danach noch verbliebene einzelne
    Legacy-Zeilen außerhalb eines Blocks."""
    return remove_legacy_fan_lines(remove_managed_blocks(lines))


def build_block(on_minute, off_minute, enabled, included_pins):
    prefix = "" if enabled else "#"
    status = "ENABLED" if enabled else "DISABLED"
    lines = [
        CRON_START,
        f"# STATUS: {status}",
        f"# ON_MINUTE: {int(on_minute):02d}",
        f"# OFF_MINUTE: {int(off_minute):02d}",
    ]

    if included_pins:
        pin_on = " ".join(f"{p}=1" for p in included_pins)
        pin_off = " ".join(f"{p}=0" for p in included_pins)
        lines += [
            f"# Punkt :{int(on_minute):02d} Uhr: Luefter AN (1) - Pins: {','.join(included_pins)}",
            f"{prefix}{on_minute} * * * * /usr/bin/gpioset {GPIOCHIP} {pin_on}",
            f"# Punkt :{int(off_minute):02d} Uhr: Luefter AUS (0) - Pins: {','.join(included_pins)}",
            f"{prefix}{off_minute} * * * * /usr/bin/gpioset {GPIOCHIP} {pin_off}",
        ]
    else:
        lines.append("# (alle Pins aktuell manuell ausgenommen - kein aktiver Cron-Befehl)")

    lines.append(CRON_END)
    return lines


def write_cron_block(on_minute, off_minute, enabled, included_pins=None):
    """Schreibt den Cronjob-Block. Ohne explizite included_pins werden
    automatisch alle Pins AUSSER den aktuell ausgeschlossenen genutzt."""
    if included_pins is None:
        included_pins = [p for p in PIN_ORDER if p not in excluded_pins]

    existing = read_crontab_text()
    remaining = strip_fan_cron_lines(existing.splitlines())
    while remaining and remaining[-1].strip() == "":
        remaining.pop()

    block = build_block(on_minute, off_minute, enabled, included_pins)
    new_cron = "\n".join(remaining + [""] + block) + "\n"

    proc = run_cmd(SUDO + ["crontab", "-"], input_text=new_cron)
    return proc


def load_pin_state():
    """Lädt den zuletzt manuell gesetzten Zustand aus einer Datei –
    NICHT per gpioget, da das Lesen den Ausgang zerstören würde."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return {p: bool(data.get(p, False)) for p in PIN_ORDER}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {p: False for p in PIN_ORDER}


def save_pin_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


def load_excluded_pins():
    """Lädt {pin: iso_timestamp} für Pins, die aktuell temporär manuell
    vom Zeitplan ausgenommen sind."""
    try:
        with open(EXCLUDED_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {p: ts for p, ts in data.items() if p in PIN_ORDER}
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return {}


def save_excluded_pins(pins_dict):
    try:
        with open(EXCLUDED_FILE, "w") as f:
            json.dump(pins_dict, f)
    except OSError:
        pass


# Im Prozessspeicher gehaltener, zuletzt manuell gesetzter Zustand.
# Wird bei jedem gpioset-Aufruf der App aktualisiert und persistiert.
pin_state = load_pin_state()

# {pin: iso_timestamp} - temporär vom Zeitplan ausgenommene Pins.
excluded_pins = load_excluded_pins()


def load_runtime():
    """Lädt die bisher aufsummierte Laufzeit pro Pin in Sekunden."""
    try:
        with open(RUNTIME_FILE) as f:
            data = json.load(f)
        return {p: float(data.get(p, 0)) for p in PIN_ORDER}
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return {p: 0.0 for p in PIN_ORDER}


def save_runtime(data):
    try:
        with open(RUNTIME_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


# Aufsummierte Laufzeit pro Pin in Sekunden (persistiert).
runtime_seconds = load_runtime()
runtime_lock = threading.Lock()


def load_sequences():
    """Lädt die gespeicherten Sequenzen: {id: {"id", "name", "steps", "loop"}}."""
    try:
        with open(SEQUENCES_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return {}


def save_sequences(data):
    try:
        with open(SEQUENCES_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


# Gespeicherte Sequenz-Definitionen (persistiert).
sequences = load_sequences()

# Aktuell laufende Sequenz (nur eine gleichzeitig) - None wenn keine läuft.
sequence_lock = threading.RLock()
running_sequence = None


def _runtime_tracker_loop():
    """Läuft dauerhaft im Hintergrund und prüft alle 20s, welche Lüfter
    gerade laufen (über get_current_pin_values - funktioniert egal ob
    das über Zeitplan, manuell oder Timer zustande kommt) und zählt die
    Zeit hoch. Grobe Genauigkeit (20s-Raster) reicht für eine
    Wartungs-Schätzung völlig aus."""
    interval = 20
    last_check = time.monotonic()
    while True:
        time.sleep(interval)
        now = time.monotonic()
        elapsed = now - last_check
        last_check = now
        try:
            pins, _source = get_current_pin_values()
        except Exception:
            continue
        with runtime_lock:
            changed = False
            for pin, is_on in pins.items():
                if is_on:
                    runtime_seconds[pin] = runtime_seconds.get(pin, 0.0) + elapsed
                    changed = True
            if changed:
                save_runtime(runtime_seconds)


# Aktive Sicherheits-Timer: scope ("master" oder Pin-Nummer) -> Infos.
# RLock (nicht Lock), da get_current_pin_values() innerhalb eines schon
# gesperrten Abschnitts erneut daran vorbeikommen kann.
active_timers = {}
timers_lock = threading.RLock()


def compute_scheduled_state(on_minute, off_minute):
    """Berechnet rein rechnerisch (ohne Hardware-Zugriff), ob die Lüfter
    gerade laut Zeitplan an sein sollten - basierend auf der aktuellen
    Minute und den hinterlegten An-/Aus-Minuten."""
    now_minute = datetime.datetime.now().minute
    on_minute = int(on_minute)
    off_minute = int(off_minute)
    if on_minute == off_minute:
        return False
    if on_minute < off_minute:
        return on_minute <= now_minute < off_minute
    return now_minute >= on_minute or now_minute < off_minute


def apply_automation_sync(on_minute, off_minute, pins=None):
    """Setzt die Hardware SOFORT auf den Zustand, den der Zeitplan gerade
    vorschreiben würde - statt auf den nächsten stündlichen Cron-Trigger
    zu warten. Ohne explizite pins werden alle 4 Pins gesetzt."""
    if pins is None:
        pins = list(PIN_ORDER)
    if not pins:
        return subprocess.CompletedProcess(args=[], returncode=0)
    desired_on = compute_scheduled_state(on_minute, off_minute)
    return apply_pin_values(pins, desired_on)


def _most_recent_trigger_datetime(on_minute, off_minute, now=None):
    """Liefert den Zeitpunkt des letzten tatsächlichen An- ODER
    Aus-Triggers, der bereits vergangen ist."""
    now = now or datetime.datetime.now()
    candidates = []
    for m in (int(on_minute), int(off_minute)):
        candidate = now.replace(minute=m, second=0, microsecond=0)
        if candidate > now:
            candidate -= datetime.timedelta(hours=1)
        candidates.append(candidate)
    return max(candidates)


def refresh_excluded_pins(on_minute, off_minute):
    """Entfernt automatisch alle Ausnahmen, die VOR dem letzten
    tatsächlichen Zeitplan-Trigger gesetzt wurden - ab dem nächsten
    Trigger übernimmt der Zeitplan diese Pins wieder ganz normal. Pins,
    die aktuell von einem laufenden Timer gehalten werden, bleiben davon
    unberührt - der Timer entscheidet selbst, wann er sie freigibt."""
    if not excluded_pins:
        return

    with timers_lock:
        timer_held_pins = set()
        for entry in active_timers.values():
            timer_held_pins.update(entry["pins"])

    trigger_dt = _most_recent_trigger_datetime(on_minute, off_minute)
    changed = False
    pins_to_resync = []
    for pin in list(excluded_pins.keys()):
        if pin in timer_held_pins:
            continue
        try:
            excluded_at = datetime.datetime.fromisoformat(excluded_pins[pin])
        except (ValueError, TypeError):
            excluded_at = None
        if excluded_at is None or excluded_at < trigger_dt:
            del excluded_pins[pin]
            changed = True
            pins_to_resync.append(pin)

    if pins_to_resync:
        # WICHTIG: Der reguläre Cron-Befehl hat diese Pins ja bewusst
        # ausgeklammert, hat sie also beim letzten Trigger nie physisch
        # angefasst. Ohne diesen expliziten Nachzieh-Schritt würde die
        # Weboberfläche zwar korrekt "aus" (o.ä.) anzeigen, die Hardware
        # aber unverändert im alten manuellen Zustand verharren.
        desired_on = compute_scheduled_state(on_minute, off_minute)
        apply_pin_values(pins_to_resync, desired_on)

    if changed:
        save_excluded_pins(excluded_pins)
        # Cronjob-Befehl neu schreiben, damit er ab sofort wieder ALLE
        # (inkl. der gerade zurückgeholten) Pins normal mitsteuert -
        # sonst würde der nächste stündliche Trigger diesen Pin immer
        # noch überspringen.
        current = parse_cron_state(read_crontab_text())
        if current["found"]:
            write_cron_block(on_minute, off_minute, current["enabled"])


def exclude_pins_from_automation(pins):
    """Nimmt eine oder mehrere Pins temporär aus dem laufenden Zeitplan
    heraus - die Automatik läuft für die übrigen Pins unverändert weiter.
    Die Pins bleiben ausgeschlossen, bis der nächste Zeitplan-Trigger sie
    automatisch zurückholt."""
    if not pins:
        return
    cron_state = parse_cron_state(read_crontab_text())
    if cron_state["found"] and cron_state["on_minute"] is not None and cron_state["off_minute"] is not None:
        refresh_excluded_pins(cron_state["on_minute"], cron_state["off_minute"])

    now_iso = datetime.datetime.now().isoformat()
    for p in pins:
        excluded_pins[p] = now_iso
    save_excluded_pins(excluded_pins)

    if cron_state["found"]:
        on_minute = cron_state["on_minute"] or DEFAULT_ON_MINUTE
        off_minute = cron_state["off_minute"] or DEFAULT_OFF_MINUTE
        write_cron_block(on_minute, off_minute, cron_state["enabled"])


def get_current_pin_values():
    """Liefert den aktuell gültigen Zustand pro Pin. Läuft die Automatik,
    gilt der berechnete Zeitplan-Wert für alle Pins AUSSER den einzeln
    ausgeschlossenen - die behalten ihren zuletzt manuell gesetzten Wert,
    bis der nächste Zeitplan-Trigger sie automatisch zurückholt."""
    cron_state = parse_cron_state(read_crontab_text())
    if cron_state["found"] and cron_state["enabled"] and cron_state["on_minute"] is not None and cron_state["off_minute"] is not None:
        refresh_excluded_pins(cron_state["on_minute"], cron_state["off_minute"])
        is_on = compute_scheduled_state(cron_state["on_minute"], cron_state["off_minute"])
        result = {}
        for pin in PIN_ORDER:
            result[pin] = pin_state.get(pin, False) if pin in excluded_pins else is_on
        source = "automatik" if not excluded_pins else "automatik+manuell"
        return result, source
    return dict(pin_state), "manuell"


def apply_pin_values(pins, on):
    """Schaltet gezielt eine Liste von Pins auf denselben Wert und
    aktualisiert/persistiert den gespeicherten Zustand."""
    value = "1" if on else "0"
    args = [f"{p}={value}" for p in pins]
    result = run_cmd(SUDO + ["gpioset", GPIOCHIP] + args)
    if result.returncode == 0:
        for p in pins:
            pin_state[p] = on
        save_pin_state(pin_state)
    return result


def ensure_automation_disabled():
    """Wird vor jeder manuellen Schaltung aufgerufen. Pausiert den
    Cronjob, falls er gerade aktiv ist – Uhrzeiten bleiben erhalten."""
    state = parse_cron_state(read_crontab_text())
    if not state["found"]:
        # noch kein Zeitplan hinterlegt -> mit Defaults anlegen, aber
        # direkt pausiert, da wir gerade manuell schalten
        write_cron_block(DEFAULT_ON_MINUTE, DEFAULT_OFF_MINUTE, enabled=False)
        return
    if state["enabled"]:
        on_minute = state["on_minute"] or DEFAULT_ON_MINUTE
        off_minute = state["off_minute"] or DEFAULT_OFF_MINUTE
        # Übergangswert übernehmen, damit die Anzeige nach dem Pausieren
        # nicht auf einen veralteten manuellen Stand zurückfällt
        was_on = compute_scheduled_state(on_minute, off_minute)
        for pin in PIN_ORDER:
            pin_state[pin] = was_on
        save_pin_state(pin_state)
        write_cron_block(on_minute, off_minute, enabled=False)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/api/status", methods=["GET"])
def get_status():
    """Liefert den Lüfterstatus OHNE gpioget - ein Hardware-Read würde die
    Pin-Richtung auf Eingang umschalten und damit den Ausgang deaktivieren
    (genau das Problem, das die App vorher hatte)."""
    pins, source = get_current_pin_values()
    return jsonify({"pins": pins, "source": source, "excluded_pins": sorted(excluded_pins.keys())})


@app.route("/api/fan/<pin>", methods=["POST"])
def set_fan(pin):
    if pin not in PIN_ORDER:
        return jsonify({"error": "Ungültiger Pin"}), 400

    data = request.get_json(silent=True) or {}
    state = "1" if data.get("state") else "0"

    # Kein exclude_pin_from_timers(pin) mehr: ein einzelner Lüfter-Klick
    # soll einen laufenden Timer für genau diesen Lüfter nicht "abmelden"
    # - der Timer behält beim Ablauf/Abbrechen das letzte Wort.
    with timers_lock:
        held_by_timer = any(pin in entry["pins"] for entry in active_timers.values())

    if held_by_timer:
        # Timer hat für diesen Pin Vorrang - Zeitplan/Ausnahmen unberührt
        # lassen, nur physisch schalten. Der Timer bestimmt das Ende.
        pass
    else:
        # Nur DIESEN Pin temporär aus dem Zeitplan nehmen - die Automatik
        # bleibt für die anderen Pins (und generell) aktiv und holt sich
        # diesen Pin beim nächsten Trigger automatisch zurück.
        exclude_pins_from_automation([pin])

    cmd = SUDO + ["gpioset", GPIOCHIP, f"{pin}={state}"]
    result = run_cmd(cmd)
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip() or "gpioset fehlgeschlagen"}), 500

    pin_state[pin] = (state == "1")
    save_pin_state(pin_state)

    return jsonify({"pin": pin, "state": state == "1"})


@app.route("/api/all", methods=["POST"])
def set_all():
    data = request.get_json(silent=True) or {}
    state = "1" if data.get("state") else "0"

    with timers_lock:
        held_by_timer = set()
        for entry in active_timers.values():
            held_by_timer.update(entry["pins"])

    pins_to_exclude = [p for p in PIN_ORDER if p not in held_by_timer]
    if pins_to_exclude:
        # Nur diese Pins temporär aus dem Zeitplan nehmen - der Zeitplan
        # selbst bleibt aktiv und holt sie sich beim nächsten Trigger
        # automatisch zurück (genau wie beim Einzelschalter).
        exclude_pins_from_automation(pins_to_exclude)

    args = [f"{pin}={state}" for pin in PIN_ORDER]
    cmd = SUDO + ["gpioset", GPIOCHIP] + args
    result = run_cmd(cmd)
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip() or "gpioset fehlgeschlagen"}), 500

    for pin in PIN_ORDER:
        pin_state[pin] = (state == "1")
    save_pin_state(pin_state)

    return jsonify({"state": state == "1"})


@app.route("/api/schedule", methods=["GET"])
def get_schedule():
    state = parse_cron_state(read_crontab_text())
    return jsonify({
        "on_minute": state["on_minute"],
        "off_minute": state["off_minute"],
        "enabled": state["enabled"] if state["found"] else None,
    })


@app.route("/api/schedule", methods=["POST"])
def set_schedule():
    data = request.get_json(silent=True) or {}
    try:
        on_minute = int(data.get("on_minute"))
        off_minute = int(data.get("off_minute"))
        assert 0 <= on_minute <= 59 and 0 <= off_minute <= 59
    except (TypeError, ValueError, AssertionError):
        return jsonify({"error": "Minuten müssen zwischen 0 und 59 liegen"}), 400

    # Laufende Timer/Sequenzen hätten sonst einen jetzt veralteten
    # Vorzustand gespeichert und würden ihn später fälschlich wiederherstellen.
    cancel_all_timers()
    stop_running_sequence()

    # aktuellen Enabled-Status beibehalten (Standard: aktiv, falls noch nichts existiert)
    current = parse_cron_state(read_crontab_text())
    enabled = current["enabled"] if current["found"] else True

    # Explizites Speichern ist eine bewusste "das ist jetzt DER Zeitplan"-
    # Aktion - alle temporären Pin-Ausnahmen zurücksetzen und garantiert
    # alle 4 Pins schreiben (verhindert einen "leeren" Cron-Befehl).
    excluded_pins.clear()
    save_excluded_pins(excluded_pins)

    proc = write_cron_block(on_minute, off_minute, enabled, included_pins=list(PIN_ORDER))
    if proc.returncode != 0:
        return jsonify({"error": proc.stderr.strip() or "crontab-Update fehlgeschlagen"}), 500

    if enabled:
        apply_automation_sync(on_minute, off_minute)

    return jsonify({"on_minute": on_minute, "off_minute": off_minute, "enabled": enabled})


@app.route("/api/automation", methods=["GET"])
def get_automation():
    state = parse_cron_state(read_crontab_text())
    return jsonify({
        # Wichtig: Wenn KEIN Zeitplan gefunden wurde, ist die Automatik
        # faktisch NICHT aktiv (vorher fälschlich "True" -> UI zeigte
        # "an", obwohl real gar kein Cronjob existierte).
        "enabled": state["enabled"] if state["found"] else False,
        "on_minute": state["on_minute"],
        "off_minute": state["off_minute"],
        "configured": state["found"],
    })


@app.route("/api/automation", methods=["POST"])
def set_automation():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))

    with timers_lock:
        timer_held_pins = set()
        for entry in active_timers.values():
            timer_held_pins.update(entry["pins"])

    with sequence_lock:
        sequence_running = running_sequence is not None

    if timer_held_pins or sequence_running:
        # Solange ein Timer oder eine Sequenz läuft, hat der
        # Automatik-Schalter keine Funktion - er wird danach automatisch
        # wieder normal nutzbar. Das Frontend sperrt den Schalter dafür
        # bereits, das hier ist nur die serverseitige Absicherung.
        return jsonify({"error": "Automatik kann nicht geändert werden, solange ein Timer/eine Sequenz läuft"}), 409

    if not enabled:
        # Explizites Deaktivieren ist eine bewusste "alles überschreiben"-
        # Aktion - ein laufender Timer würde sonst mit einer jetzt
        # veralteten Referenz weiterlaufen.
        cancel_all_timers()
        stop_running_sequence()
        timer_held_pins = set()

    current = parse_cron_state(read_crontab_text())
    on_minute = current["on_minute"] or DEFAULT_ON_MINUTE
    off_minute = current["off_minute"] or DEFAULT_OFF_MINUTE

    if not enabled and current["found"] and current["enabled"]:
        # War gerade aktiv -> beim expliziten Ausschalten sollen die
        # Lüfter tatsächlich ausgehen, nicht im aktuellen Zustand
        # "eingefroren" bleiben.
        result = apply_pin_values(PIN_ORDER, False)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip() or "gpioset fehlgeschlagen"}), 500

    if enabled:
        # Alle Ausnahmen zurücksetzen - AUSSER den Pins, die gerade von
        # einem laufenden Timer gehalten werden. Der Timer behält für
        # seine Restlaufzeit das letzte Wort; die Automatik wird schon
        # jetzt als "aktiv" gespeichert und übernimmt diese Pins
        # automatisch, sobald der Timer sie freigibt.
        now_iso = datetime.datetime.now().isoformat()
        for p in list(excluded_pins.keys()):
            if p not in timer_held_pins:
                del excluded_pins[p]
        for p in timer_held_pins:
            excluded_pins[p] = now_iso
        save_excluded_pins(excluded_pins)

    included_pins = list(PIN_ORDER) if not enabled else [p for p in PIN_ORDER if p not in timer_held_pins]

    proc = write_cron_block(on_minute, off_minute, enabled, included_pins=included_pins)
    if proc.returncode != 0:
        return jsonify({"error": proc.stderr.strip() or "crontab-Update fehlgeschlagen"}), 500

    if enabled:
        sync_pins = [p for p in PIN_ORDER if p not in timer_held_pins]
        sync_result = apply_automation_sync(on_minute, off_minute, sync_pins)
        if sync_result.returncode != 0:
            return jsonify({
                "error": sync_result.stderr.strip() or "gpioset-Synchronisation fehlgeschlagen",
                "enabled": enabled,
            }), 500

    return jsonify({"enabled": enabled, "on_minute": on_minute, "off_minute": off_minute})


def _cancel_timer_locked(scope):
    """Bricht den Countdown ab OHNE etwas wiederherzustellen. Nur für den
    Fall gedacht, dass direkt danach ohnehin ein neuer Zustand gesetzt
    wird (normales manuelles Schalten, neuer Timer für denselben Scope)."""
    entry = active_timers.pop(scope, None)
    if entry:
        entry["timer"].cancel()
    return entry


def cancel_all_timers():
    """Bricht JEDEN laufenden Timer ab. Wird beim Master-Schalter sowie
    bei Automatik-/Zeitplan-Änderungen aufgerufen - das sind bewusst
    "alles überschreiben"-Aktionen."""
    with timers_lock:
        for scope in list(active_timers.keys()):
            _cancel_timer_locked(scope)


def exclude_pin_from_timers(pin):
    """Nimmt genau EINEN Pin aus jedem laufenden Timer heraus, der ihn
    aktuell verwaltet - der Timer läuft für die übrigen Pins ganz normal
    weiter. Wird bei jeder EINZELNEN Lüfter-Schaltung aufgerufen (statt
    cancel_all_timers), damit ein einzelner Lüfter-Klick nie den Timer
    für die anderen Lüfter mit killt - egal ob an oder aus geklickt wird.
    Der ausgenommene Pin wird beim späteren Ablauf/Abbrechen des Timers
    nicht mehr angefasst (bleibt exakt so, wie er manuell gesetzt wurde).

    Einschränkung: War der Timer als Rückkehr-Aktion "Automatik
    reaktivieren" hinterlegt (nicht "vorherigen Zustand wiederherstellen"),
    gilt das nur pauschal für alle 4 Pins zusammen - ein einzeln
    ausgenommener Pin wird dann beim Reaktivieren der Automatik trotzdem
    wieder vom Zeitplan mitgesteuert, da der Zeitplan technisch nicht pro
    Pin einzeln funktioniert."""
    with timers_lock:
        for scope in list(active_timers.keys()):
            entry = active_timers[scope]
            if pin not in entry["pins"]:
                continue
            entry["pins"] = [p for p in entry["pins"] if p != pin]
            if entry.get("previous_pins"):
                entry["previous_pins"].pop(pin, None)
            if not entry["pins"]:
                # nichts mehr übrig, das dieser Timer verwalten müsste
                entry["timer"].cancel()
                active_timers.pop(scope, None)


def _revert_to_previous(entry):
    """Gibt die vom Timer gehaltenen Pins wieder frei und stellt den
    passenden Zielzustand her. Prüft dabei den AKTUELLEN Automatik-Status
    (nicht den von Timer-Start!) - wurde die Automatik z.B. WÄHREND der
    Timer lief aktiviert, übernimmt sie diese Pins jetzt direkt mit
    sofortigem Sync, statt sie hart auszuschalten. Pins, die schon VOR
    dem Timer individuell ausgenommen waren (z.B. ein manuell
    ausgeschalteter Lüfter), kehren in genau diesen Zustand zurück,
    statt pauschal dem Zeitplan zu folgen."""
    for p in entry["pins"]:
        excluded_pins.pop(p, None)

    current = parse_cron_state(read_crontab_text())
    automation_now_active = bool(
        current["found"] and current["enabled"]
        and current["on_minute"] is not None and current["off_minute"] is not None
    )

    if automation_now_active:
        on_minute = current["on_minute"]
        off_minute = current["off_minute"]
        pre_existing = entry.get("pre_existing_exclusions") or {}

        if pre_existing:
            now_iso = datetime.datetime.now().isoformat()
            pins_on = [p for p, v in pre_existing.items() if v]
            pins_off = [p for p, v in pre_existing.items() if not v]
            if pins_on:
                apply_pin_values(pins_on, True)
            if pins_off:
                apply_pin_values(pins_off, False)
            for p in pre_existing:
                excluded_pins[p] = now_iso

        save_excluded_pins(excluded_pins)

        # Cronjob-Befehl neu schreiben (berücksichtigt die gerade wieder
        # gesetzten Ausnahmen), dann die übrigen Pins direkt auf den
        # aktuellen Zeitplan-Wert bringen.
        write_cron_block(on_minute, off_minute, enabled=True)
        sync_pins = [p for p in entry["pins"] if p not in pre_existing]
        if sync_pins:
            apply_automation_sync(on_minute, off_minute, sync_pins)
    else:
        save_excluded_pins(excluded_pins)
        apply_pin_values(entry["pins"], False)


def _timer_expired(scope):
    """Timer ist von selbst abgelaufen -> vorherigen Zustand wiederherstellen."""
    with timers_lock:
        entry = active_timers.pop(scope, None)
    if not entry:
        return
    _revert_to_previous(entry)


def stop_running_sequence():
    """Bricht eine ggf. laufende Sequenz ab und stellt den Zustand von
    davor wieder her (gleiche Logik wie beim Timer). Hält die Sperre für
    die GESAMTE Dauer (inkl. Revert), damit der Hintergrund-Thread der
    Sequenz nicht währenddessen noch einen eigenen Schreibvorgang
    durchdrücken kann (Race Condition)."""
    global running_sequence
    with sequence_lock:
        info = running_sequence
        if not info:
            return
        info["stop_event"].set()
        running_sequence = None
        _revert_to_previous({
            "pins": info["participating_pins"],
            "pre_existing_exclusions": info["pre_existing_exclusions"],
        })


def _run_sequence(seq_id, stop_event, steps, loop, participating_pins):
    """Läuft in einem eigenen Hintergrund-Thread: arbeitet die Schritte
    der Reihe nach ab (optional als Endlos-Schleife). Prüfung UND
    Pin-Schreibvorgang passieren als Einheit innerhalb derselben Sperre
    wie ein externer Stop - dadurch ist ausgeschlossen, dass hier noch
    etwas geschrieben wird, nachdem/während von außen gestoppt wurde."""
    global running_sequence
    while True:
        for idx, step in enumerate(steps):
            with sequence_lock:
                if running_sequence is None or running_sequence.get("id") != seq_id:
                    return
                running_sequence["current_step_index"] = idx
                running_sequence["step_end_time"] = (
                    datetime.datetime.now() + datetime.timedelta(minutes=step["duration_minutes"])
                )
                step_pins = step["pins"]
                off_pins = [p for p in participating_pins if p not in step_pins]
                if step_pins:
                    apply_pin_values(step_pins, True)
                if off_pins:
                    apply_pin_values(off_pins, False)

            remaining = step["duration_minutes"] * 60
            while remaining > 0:
                if stop_event.wait(timeout=min(1, remaining)):
                    return  # extern gestoppt - Aufräumen übernimmt der Stopper
                remaining -= 1

        if not loop:
            break

    # Sequenz ist von selbst (ohne Loop) zu Ende -> hier selbst aufräumen,
    # ebenfalls unter derselben Sperre (gleicher Grund wie oben).
    with sequence_lock:
        info = running_sequence
        if info and info.get("id") == seq_id:
            running_sequence = None
            _revert_to_previous({
                "pins": info["participating_pins"],
                "pre_existing_exclusions": info["pre_existing_exclusions"],
            })


@app.route("/api/timer/start", methods=["POST"])
def start_timer():
    data = request.get_json(silent=True) or {}
    scope = data.get("scope")

    if scope != "master" and scope not in PIN_ORDER:
        return jsonify({"error": "Ungültiger scope"}), 400
    try:
        duration_minutes = float(data.get("duration_minutes"))
        assert 0 < duration_minutes <= 24 * 60
    except (TypeError, ValueError, AssertionError):
        return jsonify({"error": "Dauer muss zwischen 1 und 1440 Minuten liegen"}), 400

    pins = list(PIN_ORDER) if scope == "master" else [scope]

    # Timer und Sequenz schließen sich gegenseitig aus - eine laufende
    # Sequenz sauber beenden (mit Wiederherstellung), bevor der Timer startet.
    stop_running_sequence()

    with timers_lock:
        existing = active_timers.get(scope)

        if existing:
            # Für diesen Scope läuft schon ein Timer -> nur die Dauer
            # ändern. Den ECHTEN Ursprungszustand (von vor dem allerersten
            # Start) unverändert übernehmen, sonst würde hier fälschlich
            # der aktuelle (durch den Timer bereits erzwungene) Zustand
            # als "vorher" gespeichert werden.
            existing["timer"].cancel()
            was_automation_active = existing["was_automation_active"]
            on_minute = existing["on_minute"]
            off_minute = existing["off_minute"]
            previous_pins = existing["previous_pins"]
            pre_existing_exclusions = existing.get("pre_existing_exclusions") or {}
        else:
            # Erster Start für diesen Scope -> jetzigen Zustand als
            # "vorher" merken
            cron_state = parse_cron_state(read_crontab_text())
            was_automation_active = bool(cron_state["found"] and cron_state["enabled"])
            previous_pins = None
            pre_existing_exclusions = {}
            on_minute = off_minute = None
            if was_automation_active:
                on_minute = cron_state["on_minute"]
                off_minute = cron_state["off_minute"]
                # Pins merken, die schon VOR dem Timer individuell
                # ausgenommen waren (z.B. ein manuell ausgeschalteter
                # Lüfter) - die sollen nach Timer-Ende wieder in genau
                # diesen Zustand zurückkehren, statt pauschal dem
                # Zeitplan zu folgen.
                pre_existing_exclusions = {
                    p: pin_state.get(p, False) for p in pins if p in excluded_pins
                }
            else:
                current_values, _source = get_current_pin_values()
                previous_pins = {p: current_values[p] for p in pins}

        if was_automation_active:
            # NUR diese Pins aus dem Zeitplan ausnehmen - der Zeitplan
            # selbst bleibt "ENABLED" und reaktiviert sie beim Timer-Ende
            # automatisch wieder (siehe _revert_to_previous).
            exclude_pins_from_automation(pins)

        result = apply_pin_values(pins, True)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip() or "gpioset fehlgeschlagen"}), 500

        end_time = datetime.datetime.now() + datetime.timedelta(minutes=duration_minutes)
        timer_obj = threading.Timer(duration_minutes * 60, _timer_expired, args=(scope,))
        timer_obj.daemon = True
        timer_obj.start()

        active_timers[scope] = {
            "timer": timer_obj,
            "end_time": end_time,
            "pins": pins,
            "duration_minutes": duration_minutes,
            "was_automation_active": was_automation_active,
            "pre_existing_exclusions": pre_existing_exclusions,
            "on_minute": on_minute,
            "off_minute": off_minute,
            "previous_pins": previous_pins,
        }

    return jsonify({
        "scope": scope,
        "end_time": end_time.isoformat(),
        "remaining_seconds": int(duration_minutes * 60),
        "duration_minutes": duration_minutes,
    })


@app.route("/api/timer/cancel", methods=["POST"])
def cancel_timer():
    data = request.get_json(silent=True) or {}
    scope = data.get("scope")
    with timers_lock:
        entry = _cancel_timer_locked(scope)
    if entry:
        _revert_to_previous(entry)
    return jsonify({"cancelled": True, "scope": scope})


@app.route("/api/timer/status", methods=["GET"])
def timer_status():
    now = datetime.datetime.now()
    # Live-Status statt des eingefrorenen Werts von Timer-Start: wurde die
    # Automatik WÄHREND der Timer lief aktiviert, muss die Anzeige das
    # sofort als "reaktiviert sich" zeigen, nicht erst nach Timer-Ende.
    current = parse_cron_state(read_crontab_text())
    automation_currently_active = bool(current["found"] and current["enabled"])

    with timers_lock:
        result = {}
        for scope, entry in active_timers.items():
            remaining = int((entry["end_time"] - now).total_seconds())
            result[scope] = {
                "end_time": entry["end_time"].isoformat(),
                "remaining_seconds": max(0, remaining),
                "duration_minutes": entry.get("duration_minutes"),
                "will_reactivate_automation": automation_currently_active,
            }
    return jsonify({"timers": result})


@app.route("/api/runtime", methods=["GET"])
def get_runtime():
    with runtime_lock:
        hours = {p: round(runtime_seconds.get(p, 0.0) / 3600, 1) for p in PIN_ORDER}
    return jsonify({"hours": hours})


@app.route("/api/runtime/reset", methods=["POST"])
def reset_runtime():
    data = request.get_json(silent=True) or {}
    pin = data.get("pin")
    with runtime_lock:
        if pin == "all":
            for p in PIN_ORDER:
                runtime_seconds[p] = 0.0
        elif pin in PIN_ORDER:
            runtime_seconds[pin] = 0.0
        else:
            return jsonify({"error": "Ungültiger Pin"}), 400
        save_runtime(runtime_seconds)
        hours = {p: round(runtime_seconds.get(p, 0.0) / 3600, 1) for p in PIN_ORDER}
    return jsonify({"hours": hours})


def _validate_sequence_payload(data):
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Bitte einen Namen vergeben")

    steps_in = data.get("steps") or []
    if not isinstance(steps_in, list) or not steps_in:
        raise ValueError("Mindestens ein Schritt nötig")

    steps = []
    for raw_step in steps_in:
        pins = [p for p in (raw_step.get("pins") or []) if p in PIN_ORDER]
        if not pins:
            raise ValueError("Jeder Schritt braucht mindestens einen Lüfter")
        try:
            duration = float(raw_step.get("duration_minutes"))
            assert 0 < duration <= 24 * 60
        except (TypeError, ValueError, AssertionError):
            raise ValueError("Ungültige Dauer in einem Schritt (1–1440 Minuten)")
        steps.append({"pins": pins, "duration_minutes": duration})

    loop = bool(data.get("loop"))
    return name, steps, loop


@app.route("/api/sequences", methods=["GET"])
def list_sequences():
    return jsonify({"sequences": sequences})


@app.route("/api/sequences", methods=["POST"])
def save_sequence():
    data = request.get_json(silent=True) or {}
    try:
        name, steps, loop = _validate_sequence_payload(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    seq_id = data.get("id") or uuid.uuid4().hex[:8]
    sequences[seq_id] = {"id": seq_id, "name": name, "steps": steps, "loop": loop}
    save_sequences(sequences)
    return jsonify({"sequence": sequences[seq_id]})


@app.route("/api/sequences/<seq_id>", methods=["DELETE"])
def delete_sequence(seq_id):
    if seq_id not in sequences:
        return jsonify({"error": "Sequenz nicht gefunden"}), 404
    with sequence_lock:
        if running_sequence and running_sequence.get("id") == seq_id:
            stop_running_sequence()
    sequences.pop(seq_id, None)
    save_sequences(sequences)
    return jsonify({"deleted": True})


@app.route("/api/sequences/<seq_id>/start", methods=["POST"])
def start_sequence(seq_id):
    global running_sequence

    seq = sequences.get(seq_id)
    if not seq:
        return jsonify({"error": "Sequenz nicht gefunden"}), 404

    steps = seq["steps"]
    loop = seq.get("loop", False)
    # Die Sequenz kontrolliert IMMER alle 4 Pins (wie der Master-Timer),
    # nicht nur die in den Schritten genannten - jeder Schritt schaltet
    # so konsequent alle anderen Lüfter aus, statt sie unangetastet zu
    # lassen (überschreibt damit auch manuell gesetzte Zustände).
    participating_pins = list(PIN_ORDER)

    # Timer und Sequenz schließen sich gegenseitig aus.
    cancel_all_timers()
    # Eine ggf. schon laufende (andere) Sequenz sauber beenden.
    stop_running_sequence()

    with sequence_lock:
        cron_state = parse_cron_state(read_crontab_text())
        was_automation_active = bool(cron_state["found"] and cron_state["enabled"])
        pre_existing_exclusions = {}
        if was_automation_active:
            pre_existing_exclusions = {
                p: pin_state.get(p, False) for p in participating_pins if p in excluded_pins
            }
            exclude_pins_from_automation(participating_pins)

        stop_event = threading.Event()
        running_sequence = {
            "id": seq_id,
            "name": seq["name"],
            "steps": steps,
            "loop": loop,
            "participating_pins": participating_pins,
            "pre_existing_exclusions": pre_existing_exclusions,
            "current_step_index": 0,
            "step_end_time": None,
            "stop_event": stop_event,
        }
        thread = threading.Thread(
            target=_run_sequence,
            args=(seq_id, stop_event, steps, loop, participating_pins),
            daemon=True,
        )
        thread.start()

    return jsonify({"started": True, "id": seq_id, "name": seq["name"]})


@app.route("/api/sequences/stop", methods=["POST"])
def stop_sequence():
    with sequence_lock:
        was_running = running_sequence is not None
    stop_running_sequence()
    return jsonify({"stopped": was_running})


@app.route("/api/sequences/status", methods=["GET"])
def sequence_status():
    with sequence_lock:
        info = running_sequence
        if not info:
            return jsonify({"running": None})
        remaining = None
        if info.get("step_end_time"):
            remaining = max(0, int((info["step_end_time"] - datetime.datetime.now()).total_seconds()))
        return jsonify({
            "running": {
                "id": info["id"],
                "name": info["name"],
                "current_step_index": info["current_step_index"],
                "total_steps": len(info["steps"]),
                "current_step_pins": info["steps"][info["current_step_index"]]["pins"],
                "remaining_seconds": remaining,
                "loop": info["loop"],
            }
        })


# Laufzeit-Tracker im Hintergrund starten (läuft dauerhaft mit der App).
runtime_thread = threading.Thread(target=_runtime_tracker_loop, daemon=True)
runtime_thread.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)