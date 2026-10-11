# LadderGG

Discord-Bot für League-of-Legends-Communities: Ranglisten und Peak-Rennen innerhalb eines Discord-Servers.

Spieler verknüpfen ihre Riot ID, der Bot erfasst Rang, LP und Peak automatisch über die Riot API und stellt die Ergebnisse als Leaderboard in Discord dar.

## Funktionen

- Verknüpfung von Riot IDs mit Discord-Accounts
- Automatische Aktualisierung von Rang, LP und Peak
- Ranglisten und Peak-Rennen pro Server
- Einrichtung direkt in Discord per `/config setup`
- Zweisprachig (Deutsch / Englisch)
- Tägliche Zusammenfassung zu einer festen Uhrzeit
- Optionaler Export nach Google Sheets
- Fehlermeldungen per Discord-Webhook (Monitoring)

## Technik

- **Sprache:** Python 3
- **Datenbank:** PostgreSQL
- **Betrieb:** Linux-Server (Ubuntu), Bot läuft als systemd-Dienst
- **Backups:** tägliches Datenbank-Backup per Cronjob

## Installation

```bash
git clone git@github.com:Arexor1/laddergg.git
cd laddergg
python3 -m venv venv
source venv/bin/activate
pip install -r requirements_LadderGG.txt
cp .env.example .env
```

Anschließend die Werte in `.env` eintragen (mindestens `DISCORD_TOKEN`, `RIOT_API_KEY` und `DATABASE_URL`). Für den Google-Sheets-Export wird zusätzlich eine `google_credentials.json` (Service Account) benötigt.

**Wichtig:** `.env` und `google_credentials.json` enthalten Zugangsdaten und dürfen nie ins Repository. Beide sind in der `.gitignore` ausgeschlossen.

## Betrieb als systemd-Dienst

Konfiguration in `/etc/systemd/system/laddergg.service`:

```ini
[Unit]
Description=LadderGG Discord Bot
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ladderbot
ExecStart=/home/ubuntu/ladderbot/venv/bin/python /home/ubuntu/ladderbot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
````

Wichtige Befehle:

```bash
sudo systemctl enable --now laddergg   # starten und beim Boot aktivieren
sudo systemctl restart laddergg        # nach einem Update neu starten
sudo systemctl status laddergg         # Status prüfen
journalctl -u laddergg -f              # Logs live ansehen
```

## Backups

`backup_db.sh` erstellt ein komprimiertes Backup der Datenbank. Es läuft täglich um 4 Uhr (Serverzeit) per Cronjob:

```
0 4 * * * /home/ubuntu/ladderbot/backup_db.sh >> /home/ubuntu/ladderbot/backups/backup.log 2>&1
```

Zur Wiederherstellung dient `restore_db.sh`. Die Backups liegen in `backups/` und werden nicht ins Repository übernommen, da sie Spielerdaten enthalten.

## Weitere Skripte

- `migrate_puuids.py` – Datenmigration nach einem Wechsel des Riot-API-Keys: Riot verschlüsselt PUUIDs pro Key, daher holt das Skript für alle registrierten Accounts neue PUUIDs und ersetzt sie in allen betroffenen Tabellen. Idempotent, kann also gefahrlos mehrfach ausgeführt werden.
  Aufruf: `./venv/bin/python migrate_puuids.py`

## Update-Ablauf

```bash
cd /home/ubuntu/ladderbot
git pull
source venv/bin/activate
pip install -r requirements_LadderGG.txt
sudo systemctl restart laddergg
```
