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

    # Laufende Timer hätten sonst einen jetzt veralteten Vorzustand
    # gespeichert und würden ihn später fälschlich wiederherstellen.
    cancel_all_timers()

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

    if not enabled:
        # Explizites Deaktivieren ist eine bewusste "alles überschreiben"-
        # Aktion - ein laufender Timer würde sonst mit einer jetzt
        # veralteten Referenz weiterlaufen.
        cancel_all_timers()
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
    sofortigem Sync, statt sie hart auszuschalten."""
    released = False
    for p in entry["pins"]:
        if excluded_pins.pop(p, None) is not None:
            released = True
    if released:
        save_excluded_pins(excluded_pins)

    current = parse_cron_state(read_crontab_text())
    automation_now_active = bool(
        current["found"] and current["enabled"]
        and current["on_minute"] is not None and current["off_minute"] is not None
    )

    if automation_now_active:
        on_minute = current["on_minute"]
        off_minute = current["off_minute"]
        # Cronjob-Befehl neu schreiben, damit er ab sofort auch diese
        # Pins wieder normal mitsteuert (Ausnahme wurde ja gerade
        # aufgehoben).
        write_cron_block(on_minute, off_minute, enabled=True)
        apply_automation_sync(on_minute, off_minute, entry["pins"])
    else:
        apply_pin_values(entry["pins"], False)


def _timer_expired(scope):
    """Timer ist von selbst abgelaufen -> vorherigen Zustand wiederherstellen."""
    with timers_lock:
        entry = active_timers.pop(scope, None)
    if not entry:
        return
    _revert_to_previous(entry)


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
        else:
            # Erster Start für diesen Scope -> jetzigen Zustand als
            # "vorher" merken
            cron_state = parse_cron_state(read_crontab_text())
            was_automation_active = bool(cron_state["found"] and cron_state["enabled"])
            previous_pins = None
            on_minute = off_minute = None
            if was_automation_active:
                on_minute = cron_state["on_minute"]
                off_minute = cron_state["off_minute"]
            else:
                current_values, _source = get_current_pin_values()
                previous_pins = {p: current_values[p] for p in pins}

        ensure_automation_disabled()
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
    with timers_lock:
        result = {}
        for scope, entry in active_timers.items():
            remaining = int((entry["end_time"] - now).total_seconds())
            result[scope] = {
                "end_time": entry["end_time"].isoformat(),
                "remaining_seconds": max(0, remaining),
                "duration_minutes": entry.get("duration_minutes"),
                "will_reactivate_automation": bool(entry.get("was_automation_active")),
            }
    return jsonify({"timers": result})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
