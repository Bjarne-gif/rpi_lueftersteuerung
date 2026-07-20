# Lüftersteuerung Rack

Eine Weboberfläche zum Schalten der 4 Rack-Lüfter (GPIO 17/27/22/23) inkl.
Editor für den stündlichen Cronjob, der sie bisher automatisch schaltet.

## 1. Dateien auf den Raspberry Pi kopieren

Kopiere den gesamten Ordner `luefter-steuerung/` z. B. nach `/home/pi/luefter-steuerung`.

## 2. Python-Umgebung einrichten

```bash
cd /home/pi/luefter-steuerung
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Sudo-Rechte ohne Passwort für die benötigten Befehle

Die Webseite läuft normalerweise als Benutzer `pi`, muss aber `gpioset`,
`gpioget` und `crontab` mit `sudo` ausführen können, ohne dass du jedes
Mal ein Passwort eingibst.

```bash
sudo visudo -f /etc/sudoers.d/luefter-steuerung
```

Dort folgende Zeile eintragen (Pfade ggf. mit `which gpioset` prüfen):

```
pi ALL=(ALL) NOPASSWD: /usr/bin/gpioset, /usr/bin/gpioget, /usr/bin/crontab
```

**Wichtig:** Damit darf der `pi`-User u. a. auch fremde Crontabs lesen/schreiben
(`crontab -u`). Falls dir das zu weitreichend ist, alternativ die App direkt
als `root` per systemd starten (siehe Schritt 4) und auf `sudo` in `app.py`
verzichten (dann `"sudo",` aus den Befehlslisten in `app.py` entfernen).

## 4. Autostart per systemd (empfohlen)

```bash
sudo nano /etc/systemd/system/luefter-steuerung.service
```

Inhalt:

```ini
[Unit]
Description=Luefter-Steuerung Web-Dashboard
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/luefter-steuerung
ExecStart=/home/pi/luefter-steuerung/venv/bin/python app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Aktivieren:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now luefter-steuerung
```

## 5. Aufrufen

Im Browser (im gleichen Netzwerk wie der Pi):

```
http://<IP-des-Pi>:5000
```

## Funktionen

- **Einzelschalter** pro Lüfter (GPIO 17 / 27 / 22 / 23) → ruft
  `sudo gpioset 0 <PIN>=<0|1>` auf
- **Master-Schalter** für alle 4 gleichzeitig → ein `gpioset`-Aufruf mit allen Pins
- **Live-Status** alle 4 Sekunden via `sudo gpioget 0 17 27 22 23`
- **Zeitplan-Editor**: legt fest, bei welcher Minute jeder Stunde die Lüfter
  an- bzw. ausgehen, und schreibt das direkt in `sudo crontab` – im selben
  Format wie dein bisheriger manueller Eintrag, nur mit Markierungen
  (`# LUEFTERSTEUERUNG RACK - START/END`), damit die App ihren Block beim
  nächsten Speichern zuverlässig wiederfindet und ersetzt, ohne andere
  Cronjobs anzufassen.

## Troubleshooting

- **"keine Verbindung zum Pi"** im Frontend → Backend läuft nicht oder
  Firewall blockiert Port 5000.
- **500-Fehler bei Schaltvorgängen** → meist fehlende sudo-Rechte (Schritt 3)
  oder falscher `gpioset`-Pfad (`which gpioset` prüfen und in `app.py` bei
  Bedarf anpassen).
- **Zeitplan wird nicht gespeichert** → prüfen, ob `pi` per sudoers auch
  `crontab -` (Schreiben) ausführen darf, nicht nur `-l` (Lesen).
