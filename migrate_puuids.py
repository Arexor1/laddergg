"""
Einmaliges Migrations-Script für LadderGG.

Hintergrund: Riot verschlüsselt PUUIDs teilweise pro API-Key-Kontext.
Nach einem Key-Wechsel (z.B. Development-Key -> Personal/Production-Key)
schlagen alle bestehenden, in der Datenbank gespeicherten PUUIDs mit
"400 Bad Request - Exception decrypting ..." fehl.

Dieses Script holt für jeden registrierten Account per Riot-ID (Name#Tag)
einen frischen, mit dem AKTUELLEN Key gültigen PUUID und ersetzt den alten
Wert überall in der Datenbank (riot_accounts + ladder_players), inklusive
einer frischen Ranked-Abfrage, damit die Daten direkt wieder stimmen.

Benutzung (einmalig, auf dem Server im ladderbot-Ordner):
    ./venv/bin/python migrate_puuids.py

Sicher zum mehrfachen Ausführen (idempotent): Accounts, deren PUUID sich
nicht geändert hat, werden übersprungen.
"""

import asyncio
import os

import aiohttp
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RIOT_API_KEY = os.getenv("RIOT_API_KEY", "").strip()

REGION_MAP = {
    "BR1": "americas", "EUN1": "europe", "EUW1": "europe", "JP1": "asia",
    "KR": "asia", "LA1": "americas", "LA2": "americas", "NA1": "americas",
    "OC1": "sea", "PH2": "sea", "RU": "europe", "SG2": "sea", "TH2": "sea",
    "TR1": "europe", "TW2": "sea", "VN2": "sea",
}

REQUEST_DELAY_SECONDS = 1.3


def regional_for_platform(platform: str) -> str:
    return REGION_MAP.get(platform.upper(), "europe")


async def fetch_fresh_account(
    session: aiohttp.ClientSession,
    game_name: str,
    tag_line: str,
    platform: str,
):
    regional = regional_for_platform(platform)
    url = (
        f"https://{regional}.api.riotgames.com/riot/account/v1/accounts/"
        f"by-riot-id/{game_name}/{tag_line}"
    )
    async with session.get(url) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise ValueError(f"{resp.status}: {text[:200]}")
        return await resp.json()


async def fetch_ranked_entry(
    session: aiohttp.ClientSession,
    puuid: str,
    platform: str,
):
    url = (
        f"https://{platform.lower()}.api.riotgames.com/lol/league/v4/"
        f"entries/by-puuid/{puuid}"
    )
    async with session.get(url) as resp:
        if resp.status != 200:
            return None
        entries = await resp.json()
        for entry in entries:
            if entry.get("queueType") == "RANKED_SOLO_5x5":
                return entry
        return None


async def migrate():
    if not DATABASE_URL:
        print("❌ DATABASE_URL fehlt in .env")
        return
    if not RIOT_API_KEY:
        print("❌ RIOT_API_KEY fehlt in .env")
        return

    pool = await asyncpg.create_pool(DATABASE_URL)
    accounts = await pool.fetch(
        "SELECT puuid, platform, game_name, tag_line FROM riot_accounts"
    )

    print(f"{len(accounts)} Account(s) in der Datenbank gefunden.\n")

    migrated = 0
    unchanged = 0
    failed = 0

    async with aiohttp.ClientSession(
        headers={"X-Riot-Token": RIOT_API_KEY},
        timeout=aiohttp.ClientTimeout(total=20),
    ) as session:
        for row in accounts:
            old_puuid = row["puuid"]
            platform = row["platform"]
            game_name = row["game_name"]
            tag_line = row["tag_line"]
            label = f"{game_name}#{tag_line} ({platform})"

            try:
                await asyncio.sleep(REQUEST_DELAY_SECONDS)
                account = await fetch_fresh_account(
                    session, game_name, tag_line, platform
                )
                new_puuid = account["puuid"]

                if new_puuid == old_puuid:
                    unchanged += 1
                    print(f"= unverändert: {label}")
                    continue

                await asyncio.sleep(REQUEST_DELAY_SECONDS)
                ranked = await fetch_ranked_entry(session, new_puuid, platform)

                async with pool.acquire() as conn:
                    async with conn.transaction():
                        # 1. Neuen Account-Datensatz anlegen (mit frischen
                        #    Ranked-Daten, falls vorhanden).
                        await conn.execute(
                            """
                            INSERT INTO riot_accounts (
                                puuid, platform, game_name, tag_line,
                                current_lp, current_label, tier, rank,
                                league_points, wins, losses,
                                last_riot_refresh, last_identity_refresh,
                                updated_at
                            )
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW(),NOW(),NOW())
                            ON CONFLICT (puuid, platform) DO UPDATE SET
                                game_name = EXCLUDED.game_name,
                                tag_line = EXCLUDED.tag_line,
                                updated_at = NOW()
                            """,
                            new_puuid,
                            platform,
                            account.get("gameName") or game_name,
                            account.get("tagLine") or tag_line,
                            ranked.get("leaguePoints") if ranked else None,
                            (
                                f"{ranked['tier'].title()} "
                                f"{ranked.get('rank','')} "
                                f"{ranked['leaguePoints']} LP"
                            ).strip() if ranked else None,
                            ranked.get("tier") if ranked else None,
                            ranked.get("rank") if ranked else None,
                            ranked.get("leaguePoints") if ranked else None,
                            ranked.get("wins") if ranked else None,
                            ranked.get("losses") if ranked else None,
                        )

                        # 2. Alle Referenzen in ladder_players umbiegen.
                        await conn.execute(
                            """
                            UPDATE ladder_players
                            SET puuid = $1, updated_at = NOW()
                            WHERE puuid = $2 AND platform = $3
                            """,
                            new_puuid,
                            old_puuid,
                            platform,
                        )

                        # 3. Alten Account-Datensatz entfernen.
                        await conn.execute(
                            """
                            DELETE FROM riot_accounts
                            WHERE puuid = $1 AND platform = $2
                            """,
                            old_puuid,
                            platform,
                        )

                migrated += 1
                print(f"✅ migriert: {label}")

            except Exception as exc:
                failed += 1
                print(f"❌ Fehler bei {label}: {exc}")

    await pool.close()

    print(
        f"\nFertig. {migrated} migriert, {unchanged} unverändert, "
        f"{failed} fehlgeschlagen."
    )


if __name__ == "__main__":
    asyncio.run(migrate())
