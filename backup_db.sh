#!/bin/bash
# Tägliches Backup der LadderGG-Datenbank.
#
# Legt komprimierte Dumps in ~/ladderbot/backups ab und behält die
# letzten 14 Tage (ältere werden automatisch gelöscht).
#
# Einrichtung (einmalig):
#   chmod +x backup_db.sh
#   crontab -e
#   Dann folgende Zeile einfügen (läuft täglich um 4:00 Uhr nachts):
#   0 4 * * * /home/ubuntu/ladderbot/backup_db.sh >> /home/ubuntu/ladderbot/backups/backup.log 2>&1

set -e

BOT_DIR="$HOME/ladderbot"
BACKUP_DIR="$BOT_DIR/backups"
KEEP_DAYS=14
TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)

mkdir -p "$BACKUP_DIR"

# DATABASE_URL aus der .env laden.
if [ ! -f "$BOT_DIR/.env" ]; then
    echo "❌ .env nicht gefunden unter $BOT_DIR/.env"
    exit 1
fi

DATABASE_URL=$(grep -E '^DATABASE_URL=' "$BOT_DIR/.env" | cut -d '=' -f2- | tr -d '"' | tr -d "'")

if [ -z "$DATABASE_URL" ]; then
    echo "❌ DATABASE_URL nicht in .env gefunden"
    exit 1
fi

BACKUP_FILE="$BACKUP_DIR/laddergg_$TIMESTAMP.sql.gz"

echo "[$TIMESTAMP] Starte Backup nach $BACKUP_FILE ..."

pg_dump "$DATABASE_URL" | gzip > "$BACKUP_FILE"

if [ -s "$BACKUP_FILE" ]; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "[$TIMESTAMP] ✅ Backup fertig ($SIZE)."
else
    echo "[$TIMESTAMP] ❌ Backup fehlgeschlagen oder leer."
    rm -f "$BACKUP_FILE"
    exit 1
fi

# Alte Backups aufräumen (älter als KEEP_DAYS Tage).
find "$BACKUP_DIR" -name "laddergg_*.sql.gz" -mtime +$KEEP_DAYS -delete

echo "[$TIMESTAMP] Aufräumen erledigt (behalte die letzten $KEEP_DAYS Tage)."
