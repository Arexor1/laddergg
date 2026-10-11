#!/bin/bash
# Stellt ein Backup wieder her.
#
# ACHTUNG: Das überschreibt die aktuelle Datenbank mit dem Stand aus dem
# Backup. Nur benutzen, wenn wirklich was schiefgelaufen ist.
#
# Benutzung:
#   ./restore_db.sh backups/laddergg_2026-08-22_04-00-00.sql.gz

set -e

BOT_DIR="$HOME/ladderbot"

if [ -z "$1" ]; then
    echo "Benutzung: ./restore_db.sh <pfad-zum-backup.sql.gz>"
    echo ""
    echo "Verfügbare Backups:"
    ls -la "$BOT_DIR/backups/"*.sql.gz 2>/dev/null || echo "  (keine gefunden)"
    exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "$BACKUP_FILE" ]; then
    echo "❌ Datei nicht gefunden: $BACKUP_FILE"
    exit 1
fi

DATABASE_URL=$(grep -E '^DATABASE_URL=' "$BOT_DIR/.env" | cut -d '=' -f2- | tr -d '"' | tr -d "'")

if [ -z "$DATABASE_URL" ]; then
    echo "❌ DATABASE_URL nicht in .env gefunden"
    exit 1
fi

echo "⚠️  Das überschreibt die AKTUELLE Datenbank mit: $BACKUP_FILE"
echo "⚠️  Der Bot sollte dafür gestoppt sein (sudo systemctl stop laddergg)."
echo ""
echo "Wirklich fortfahren? Tippe 'JA' zum Bestätigen:"
read -r CONFIRM

if [ "$CONFIRM" != "JA" ]; then
    echo "Abgebrochen."
    exit 1
fi

echo "Stelle wieder her..."
gunzip -c "$BACKUP_FILE" | psql "$DATABASE_URL"

echo "✅ Wiederherstellung abgeschlossen. Bot wieder starten mit:"
echo "   sudo systemctl start laddergg"
