import os
import asyncio
import re
import sys
import logging
import json
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import aiohttp
from urllib.parse import quote
import asyncpg
import discord
import gspread
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("LadderGG")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
RIOT_API_KEY = os.getenv("RIOT_API_KEY", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# 0 = globale Commands (für Public-Betrieb). Für lokale Entwicklung kann
# hier testweise eine eigene Guild-ID gesetzt werden (TEST_GUILD_ID=123...
# in der .env), damit Slash-Command-Änderungen sofort sichtbar sind statt
# der bis zu 1 Stunde dauernden globalen Discord-Propagation.
#
# WICHTIG: Vorher stand hier eine feste, fremde Test-Guild-ID als Default.
# Falls TEST_GUILD_ID in der .env NICHT gesetzt war, wären Slash Commands
# NUR in dieser einen Guild sichtbar gewesen - auf allen anderen Servern
# (also für alle echten Nutzer) hätte der Bot keine Commands gezeigt.
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0"))

# Discord-Webhook-URL (Channel-Einstellungen -> Integrationen -> Webhooks)
# für Online/Offline-Benachrichtigungen. Leer lassen zum Deaktivieren.
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()

UPDATE_MINUTES = max(1, int(os.getenv("UPDATE_MINUTES", "10")))
MATCH_TRACKING_ENABLED = os.getenv(
    "MATCH_TRACKING_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}

MATCH_LOOKBACK_COUNT = max(
    1,
    min(int(os.getenv("MATCH_LOOKBACK_COUNT", "5")), 20),
)

MATCH_ALERTS_ENABLED_DEFAULT = os.getenv(
    "MATCH_ALERTS_ENABLED_DEFAULT", "false"
).strip().lower() in {"1", "true", "yes", "on"}

DAILY_SUMMARY_HOUR_UTC = max(
    0,
    min(int(os.getenv("DAILY_SUMMARY_HOUR_UTC", "0")), 23),
)
LEADERBOARD_PAGE_SIZE = max(1, int(os.getenv("LEADERBOARD_PAGE_SIZE", "15")))
MAX_MESSAGE_LENGTH = 1900
CHALLENGER_MULTIPLIER = 1.30

# Development Keys haben enge Limits. Der Default entspricht grob dem alten,
# vorsichtigen Verhalten. Mit einem Production Key kann dieser Wert angepasst werden.
RIOT_REQUEST_DELAY_SECONDS = max(
    0.0, float(os.getenv("RIOT_REQUEST_DELAY_SECONDS", "1.2"))
)

GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_FILE", "google_credentials.json"
).strip()

# clear + update = zwei Write-Requests je Sheet.
# 2.5 s Abstand lässt bei 50 Sheets Reserve unter 60 Write-Requests/min.
SHEET_SYNC_DELAY_SECONDS = max(
    0.0, float(os.getenv("SHEET_SYNC_DELAY_SECONDS", "2.5"))
)

REGION_MAP = {
    "BR1": "americas",
    "EUN1": "europe",
    "EUW1": "europe",
    "JP1": "asia",
    "KR": "asia",
    "LA1": "americas",
    "LA2": "americas",
    "NA1": "americas",
    "OC1": "sea",
    "PH2": "sea",
    "RU": "europe",
    "SG2": "sea",
    "TH2": "sea",
    "TR1": "europe",
    "TW2": "sea",
    "VN2": "sea",
}

REGION_ALIASES = {
    "EUW": "EUW1",
    "EUNE": "EUN1",
    "EUN": "EUN1",
    "NA": "NA1",
    "BR": "BR1",
    "JP": "JP1",
    "LAN": "LA1",
    "LAS": "LA2",
    "OCE": "OC1",
    "PH": "PH2",
    "SG": "SG2",
    "TH": "TH2",
    "TR": "TR1",
    "TW": "TW2",
    "VN": "VN2",
}

TIERS = {
    "IRON": 0,
    "BRONZE": 400,
    "SILVER": 800,
    "GOLD": 1200,
    "PLATINUM": 1600,
    "EMERALD": 2000,
    "DIAMOND": 2400,
    "MASTER": 2800,
    "GRANDMASTER": 2800,
    "CHALLENGER": 2800,
}
DIVISIONS = {"IV": 0, "III": 100, "II": 200, "I": 300}


LADDER_TYPES = {"normal", "challenge"}

DEFAULT_CHALLENGE_RULES = {
    "scoring": {
        "mode": "peak_gain",
        "allow_negative": True,
        "floor": None,
        "cap": None,
    },
    "start": {
        "mode": "all_time_peak",
    },
    "multipliers": [
        {
            "tier": "MASTER",
            "multiplier": 1.0,
            "only_after_entry": False,
        },
        {
            "tier": "GRANDMASTER",
            "multiplier": 1.0,
            "only_after_entry": False,
        },
        {
            "tier": "CHALLENGER",
            "multiplier": 1.3,
            "only_after_entry": True,
        },
    ],
    "bonuses": [],
    "penalties": [],
    "tiebreakers": ["score", "new_peak", "current_lp"],
    "join": {
        "allow_after_start": True,
        "late_join_start_mode": "join_time",
    },
    "display": {
        "show_current": True,
        "show_new_peak": True,
        "show_all_time_peak": True,
        "show_ladder_start": False,
    },
    "features": {
        "milestones_enabled": False,
        "goal": {
            "type": "none",
            "target": None,
            "auto_finish": False
        },
        "eligibility": {
            "required_role_id": None,
            "min_lp": None,
            "max_lp": None,
            "minimum_games": 0
        },
        "competition": {
            "format": "solo",
            "team_score_mode": "sum",
            "auto_start": True,
            "auto_finish_at_end": True,
            "announce_leader_change": False,
            "registration_mode": "public",
            "registration_closes_at_start": False,
            "reward_text": ""
        },
        "scoring_extras": {
            "points_per_win": 0.0,
            "points_per_loss": 0.0,
            "promotion_bonus": 0.0,
            "minimum_games_for_score": 0
        }
    },
}


def _normalize_multiplier_rules(rules: dict) -> dict:
    tiers = [
        "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM",
        "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER",
    ]
    existing = {
        str(item.get("tier", "")).upper(): item
        for item in (rules.get("multipliers") or [])
        if isinstance(item, dict)
    }

    normalized = []
    for tier in tiers:
        item = existing.get(tier) or {}
        try:
            multiplier = float(item.get("multiplier", 1.0))
        except (TypeError, ValueError):
            multiplier = 1.0

        normalized.append({
            "tier": tier,
            "multiplier": multiplier,
            # Kept for backward-compatible rule JSON, although V10.9's
            # simple tier multiplier UI no longer exposes this confusing flag.
            "only_after_entry": bool(item.get("only_after_entry", False)),
        })

    rules["multipliers"] = normalized
    return rules


def clone_default_challenge_rules() -> dict:
    return _normalize_multiplier_rules(
        json.loads(json.dumps(DEFAULT_CHALLENGE_RULES))
    )


def parse_rules_jsonb(value, default=None) -> dict:
    fallback = {} if default is None else json.loads(json.dumps(default))
    if value is None:
        return fallback
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return fallback
    result = value if isinstance(value, dict) else fallback
    if isinstance(result, dict) and (
        "multipliers" in result
        or "scoring" in result
        or "start" in result
    ):
        _normalize_multiplier_rules(result)
    return result


def normalize_platform(value: str) -> str:
    platform = value.strip().upper()
    platform = REGION_ALIASES.get(platform, platform)
    if platform not in REGION_MAP:
        allowed = ", ".join(sorted(REGION_MAP.keys()))
        raise ValueError(
            f"Unbekannte Region `{value}`. Erlaubt sind z. B. EUW, EUW1, EUN1, NA1, KR. "
            f"Technische Werte: {allowed}"
        )
    return platform.lower()


def regional_for_platform(platform: str) -> str:
    return REGION_MAP[platform.upper()]


@dataclass(frozen=True)
class RankedEntry:
    tier: str
    rank: str
    lp: int
    wins: int
    losses: int

    @property
    def absolute_lp(self) -> int:
        if self.tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
            return TIERS[self.tier] + self.lp
        return TIERS[self.tier] + DIVISIONS.get(self.rank, 0) + self.lp

    @property
    def label(self) -> str:
        if self.tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
            return f"{self.tier.title()} {self.lp} LP"
        return f"{self.tier.title()} {self.rank} {self.lp} LP"


_PEAK_PARSE_ERRORS = {
    "de": {
        "empty": "Bitte gib deine All Time Peak Elo an, z. B. `Emerald I 75 LP`.",
        "unknown_tier": "Unbekannter Rang. Beispiel: `Emerald I 75 LP` oder `Master 120 LP`.",
        "too_many_master": (
            "Für Master/Grandmaster/Challenger nur Rang + LP angeben, "
            "z. B. `Master 120 LP`."
        ),
        "missing_division": "Bitte Division angeben, z. B. `Emerald I 75 LP`.",
        "too_many": "Zu viele Angaben. Beispiel: `Emerald I 75 LP`.",
        "unknown_tier_short": "Unbekannter Rang.",
        "unknown_division": "Unbekannte Division. Nutze 1-4 oder I-IV.",
        "negative_lp": "LP darf nicht negativ sein.",
        "lp_range": "Unter Master müssen LP zwischen 0 und 100 liegen.",
    },
    "en": {
        "empty": "Please enter your all-time peak rank, e.g. `Emerald I 75 LP`.",
        "unknown_tier": "Unknown rank. Example: `Emerald I 75 LP` or `Master 120 LP`.",
        "too_many_master": (
            "For Master/Grandmaster/Challenger, only give rank + LP, "
            "e.g. `Master 120 LP`."
        ),
        "missing_division": "Please include a division, e.g. `Emerald I 75 LP`.",
        "too_many": "Too many values. Example: `Emerald I 75 LP`.",
        "unknown_tier_short": "Unknown rank.",
        "unknown_division": "Unknown division. Use 1-4 or I-IV.",
        "negative_lp": "LP can't be negative.",
        "lp_range": "Below Master, LP must be between 0 and 100.",
    },
}


def _peak_error(lang: str, code: str) -> str:
    return _PEAK_PARSE_ERRORS.get(lang, _PEAK_PARSE_ERRORS["de"])[code]


def parse_alltime_peak(value: str, lang: str = "de") -> Tuple[int, str]:
    raw = value.strip().upper().replace("LP", "").replace("_", " ")
    raw = re.sub(r"\s+", " ", raw)

    if not raw:
        raise ValueError(_peak_error(lang, "empty"))

    aliases = {
        "I": "IRON",
        "B": "BRONZE",
        "S": "SILVER",
        "G": "GOLD",
        "P": "PLATINUM",
        "E": "EMERALD",
        "D": "DIAMOND",
        "M": "MASTER",
        "GM": "GRANDMASTER",
        "C": "CHALLENGER",
    }

    compact = re.match(
        r"^(GM|[IBSGPEDMC])\s*([1-4]|IV|III|II|I)?\s*(\d+)?$", raw
    )

    if compact:
        tier = aliases[compact.group(1)]
        div_raw = compact.group(2) or "I"
        lp = int(compact.group(3) or 0)
    else:
        parts = raw.split()
        tier = parts[0]

        if tier not in TIERS:
            raise ValueError(_peak_error(lang, "unknown_tier"))

        div_raw = "I"
        lp = 0

        if tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
            if len(parts) >= 2:
                lp = int(parts[1])
            if len(parts) > 2:
                raise ValueError(_peak_error(lang, "too_many_master"))
        else:
            if len(parts) < 2:
                raise ValueError(_peak_error(lang, "missing_division"))
            div_raw = parts[1]
            if len(parts) >= 3:
                lp = int(parts[2])
            if len(parts) > 3:
                raise ValueError(_peak_error(lang, "too_many"))

    roman_map = {
        "1": "I",
        "2": "II",
        "3": "III",
        "4": "IV",
        "I": "I",
        "II": "II",
        "III": "III",
        "IV": "IV",
    }
    division = roman_map.get(div_raw, div_raw)

    if tier not in TIERS:
        raise ValueError(_peak_error(lang, "unknown_tier_short"))

    if (
        tier not in {"MASTER", "GRANDMASTER", "CHALLENGER"}
        and division not in DIVISIONS
    ):
        raise ValueError(_peak_error(lang, "unknown_division"))

    if lp < 0:
        raise ValueError(_peak_error(lang, "negative_lp"))

    if tier not in {"MASTER", "GRANDMASTER", "CHALLENGER"} and lp > 100:
        raise ValueError(_peak_error(lang, "lp_range"))

    score = TIERS[tier] + (
        0
        if tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}
        else DIVISIONS[division]
    ) + lp

    label = (
        f"{tier.title()} {lp} LP"
        if tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}
        else f"{tier.title()} {division} {lp} LP"
    )

    return score, label


def tier_multiplier_for_entry(entry: RankedEntry, rules: dict) -> float:
    multipliers = rules.get("multipliers") or []
    for rule in multipliers:
        if str(rule.get("tier", "")).upper() == entry.tier.upper():
            try:
                return float(rule.get("multiplier", 1.0))
            except (TypeError, ValueError):
                return 1.0
    return 1.0


def calculate_challenge_score(
    row,
    rules: dict,
) -> float:
    mode = (rules.get("scoring") or {}).get("mode", "peak_gain")
    start_lp = int(row["ladder_start_lp"] or 0)
    current_lp = int(row["current_lp"] or 0)
    new_peak_lp = int(row["new_peak_lp"] or current_lp)
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    games = wins + losses

    if mode == "current_gain":
        base = current_lp - start_lp
    elif mode == "absolute_rank":
        base = current_lp
    elif mode == "wins":
        base = wins
    elif mode == "winrate":
        base = (wins / games * 100.0) if games else 0.0
    elif mode == "games":
        base = games
    else:
        base = new_peak_lp - start_lp

    scoring = rules.get("scoring") or {}
    if not scoring.get("allow_negative", True):
        base = max(0, base)

    extras = ((rules.get("features") or {}).get("scoring_extras") or {})
    minimum_games = int(extras.get("minimum_games_for_score") or 0)
    if games < minimum_games:
        return 0.0

    base += wins * float(extras.get("points_per_win") or 0.0)
    base += losses * float(extras.get("points_per_loss") or 0.0)

    floor = scoring.get("floor")
    cap = scoring.get("cap")
    if floor is not None:
        base = max(float(floor), base)
    if cap is not None:
        base = min(float(cap), base)

    current_label = str(row["current_label"] or "")
    tier = current_label.split(" ", 1)[0].upper() if current_label else ""

    multiplier = 1.0
    if tier:
        _normalize_multiplier_rules(rules)
        for rule in rules.get("multipliers") or []:
            if str(rule.get("tier", "")).upper() == tier:
                multiplier = float(rule.get("multiplier", 1.0))
                break

    return round(base * multiplier, 2)



RANK_EMOJI = {
    "IRON":"⚙️","BRONZE":"🟤","SILVER":"⚪","GOLD":"🟡",
    "PLATINUM":"🔷","EMERALD":"💚","DIAMOND":"💎",
    "MASTER":"🟣","GRANDMASTER":"🔴","CHALLENGER":"🏆",
}
def rank_emoji(label: Optional[str]) -> str:
    if not label:
        return "▫️"
    return RANK_EMOJI.get(str(label).split(" ",1)[0].upper(), "▫️")

def pretty_scoring_mode(mode: str) -> str:
    return {
        "peak_gain":"Peak Gain",
        "current_gain":"Current Gain",
        "absolute_rank":"Absolute Rank",
        "wins":"Wins",
        "winrate":"Winrate",
        "games":"Games",
    }.get(mode, mode.replace("_"," ").title())

def progress_bar(value: float, target: float, width: int = 10) -> str:
    if target <= 0:
        return ""
    ratio=max(0.0,min(value/target,1.0))
    filled=int(round(ratio*width))
    return "▰"*filled+"▱"*(width-filled)

def achievement_badges(row, position: Optional[int] = None) -> list[str]:
    badges: list[str] = []
    current_lp = int(row["current_lp"] or 0)
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    games = wins + losses
    winrate = (wins / games * 100) if games else 0.0
    start_lp = int(row["ladder_start_lp"] or 0)
    peak_lp = int(row["new_peak_lp"] or current_lp)
    gain = peak_lp - start_lp

    if position == 1:
        badges.append("👑")
    elif position in {2, 3}:
        badges.append("🏅")
    if gain >= 500:
        badges.append("🚀")
    elif gain >= 250:
        badges.append("📈")
    if current_lp >= TIERS["CHALLENGER"]:
        badges.append("🏆")
    elif current_lp >= TIERS["MASTER"]:
        badges.append("🏔️")
    if games >= 250:
        badges.append("🕹️")
    elif games >= 100:
        badges.append("🎮")
    if games >= 20 and winrate >= 65:
        badges.append("🔥")
    elif games >= 20 and winrate >= 60:
        badges.append("✨")
    if wins >= 100:
        badges.append("💯")
    return badges



def feature_rules(rules: dict) -> dict:
    features = rules.setdefault("features", {})
    features.setdefault("milestones_enabled", False)

    features.setdefault(
        "goal",
        {
            "type": "none",
            "target": None,
            "auto_finish": False,
        },
    )

    eligibility = features.setdefault("eligibility", {})
    eligibility.setdefault("required_role_id", None)
    eligibility.setdefault("min_lp", None)
    eligibility.setdefault("max_lp", None)
    eligibility.setdefault("minimum_games", 0)

    competition = features.setdefault("competition", {})
    competition.setdefault("auto_start", True)
    competition.setdefault("auto_finish_at_end", True)
    competition.setdefault("announce_leader_change", False)
    competition.setdefault("registration_mode", "public")
    competition.setdefault("registration_open", True)
    competition.setdefault("registration_closes_at_start", False)
    competition.setdefault("reward_1", "")
    competition.setdefault("reward_2", "")
    competition.setdefault("reward_3", "")

    scoring_extras = features.setdefault("scoring_extras", {})
    scoring_extras.setdefault("points_per_win", 0.0)
    scoring_extras.setdefault("points_per_loss", 0.0)
    scoring_extras.setdefault("minimum_games_for_score", 0)

    return features


def parse_goal_text(raw: str, lang: str = "de") -> dict:
    value = raw.strip()
    if not value or value.lower() in {"none", "kein", "keins", "aus"}:
        return {"type": "none", "target": None, "auto_finish": False}

    if ":" not in value:
        raise ValueError(
            "Enter a goal as `rank:Master 0 LP`, `score:500` or `wins:100`."
            if lang == "en"
            else "Ziel als `rank:Master 0 LP`, `score:500` oder `wins:100` eingeben."
        )

    goal_type, target = value.split(":", 1)
    goal_type = goal_type.strip().lower()
    target = target.strip()

    if goal_type == "rank":
        lp, label = parse_alltime_peak(target, lang)
        return {
            "type": "rank",
            "target": lp,
            "label": label,
            "auto_finish": True,
        }

    if goal_type in {"score", "wins"}:
        number = float(target) if goal_type == "score" else int(target)
        if number <= 0:
            raise ValueError(
                "Target value must be greater than 0."
                if lang == "en"
                else "Zielwert muss größer als 0 sein."
            )
        return {
            "type": goal_type,
            "target": number,
            "auto_finish": True,
        }

    raise ValueError(
        "Goal type must be `rank`, `score`, `wins` or `none`."
        if lang == "en"
        else "Zieltyp muss `rank`, `score`, `wins` oder `none` sein."
    )


async def member_is_ladder_admin(
    interaction: discord.Interaction,
) -> bool:
    if interaction.guild is None:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    config = await db.get_guild_config(interaction.guild.id)
    if not config:
        return False
    role_id = config["ladder_admin_role_id"]
    if not role_id:
        return False
    return any(
        int(role.id) == int(role_id)
        for role in getattr(interaction.user, "roles", [])
    )


async def require_ladder_admin(
    interaction: discord.Interaction,
) -> bool:
    if await member_is_ladder_admin(interaction):
        return True
    await interaction.response.send_message(
        "❌ You need Administrator or the configured Ladder Admin role.",
        ephemeral=True,
    )
    return False


async def is_ladder_admin_check(
    interaction: discord.Interaction,
) -> bool:
    """
    Einheitliche Rechte-Prüfung für alle Admin-Commands.

    Ersetzt @app_commands.check(is_ladder_admin_check),
    das die konfigurierbare "Ladder Admin"-Rolle (settings -> Ladder Admin
    Role) komplett ignoriert hat. Damit respektieren jetzt ALLE
    Admin-Commands konsistent entweder echte Discord-Administrator-Rechte
    ODER die pro Server konfigurierte Ladder-Admin-Rolle.
    """
    if await member_is_ladder_admin(interaction):
        return True
    raise app_commands.CheckFailure(
        "Du brauchst Administrator-Rechte oder die konfigurierte "
        "Ladder-Admin-Rolle für diesen Command."
    )


def score_label(score: float) -> str:
    if float(score).is_integer():
        score = int(score)
    prefix = "+" if score > 0 else ""
    return f"{prefix}{score} pts"


def format_delta(delta: int) -> str:
    return f"+{delta} LP" if delta > 0 else f"{delta} LP"


def weighted_delta(
    ladder_start_lp: int,
    new_peak_lp: int,
    ladder_start_label: str,
    new_peak_label: str,
    challenger_entry_lp: int,
) -> int:
    """
    Delta = New Peak - Ladder Start Peak.
    Nur positive LP, die tatsächlich in Challenger verdient wurden,
    erhalten den 1.30-Multiplikator.
    """
    normal_delta = new_peak_lp - ladder_start_lp

    if normal_delta <= 0:
        return normal_delta

    ladder_started_in_challenger = ladder_start_label.lower().startswith(
        "challenger"
    )
    new_peak_is_challenger = new_peak_label.lower().startswith("challenger")

    if not new_peak_is_challenger:
        return normal_delta

    if ladder_started_in_challenger:
        return round(normal_delta * CHALLENGER_MULTIPLIER)

    if challenger_entry_lp <= 0 or new_peak_lp <= challenger_entry_lp:
        return normal_delta

    normal_part = max(0, challenger_entry_lp - ladder_start_lp)
    challenger_part = new_peak_lp - challenger_entry_lp

    return normal_part + round(challenger_part * CHALLENGER_MULTIPLIER)


class RiotRateLimiter:
    """
    Adaptiver Sliding-Window Rate-Limiter.

    Liest Riots "X-App-Rate-Limit"-Header (Format z.B. "20:1,100:120")
    aus jeder Antwort und passt sich automatisch an die tatsächlichen
    Limits des aktuell verwendeten Keys an - inklusive automatischem
    Wechsel auf höhere Limits, sobald ein Production-Key aktiv ist,
    ohne Code-Änderung.

    Ersetzt die vorherige feste Pause (RIOT_REQUEST_DELAY_SECONDS) vor
    JEDEM Request, die bei kleinen Datenmengen unnötig langsam war und
    bei großen Datenmengen (viele Server/Accounts) den 10-Minuten-
    Update-Zyklus hätte sprengen können.
    """

    # Konservativer Startwert, bis der erste echte Header da ist
    # (Personal-Key-Standardlimits, mit Sicherheitsmarge).
    DEFAULT_WINDOWS = [(18, 1), (90, 120)]
    SAFETY_MARGIN = 0.9  # nur 90% des offiziellen Limits ausnutzen

    def __init__(self):
        self._windows: list[tuple[int, int]] = list(self.DEFAULT_WINDOWS)
        self._request_times: dict[int, list[float]] = {}
        self._lock = asyncio.Lock()

    def update_from_header(self, header_value: Optional[str]):
        if not header_value:
            return
        try:
            windows = []
            for part in header_value.split(","):
                limit_str, period_str = part.split(":")
                limit = max(1, int(int(limit_str) * self.SAFETY_MARGIN))
                windows.append((limit, int(period_str)))
            if windows:
                self._windows = windows
        except (ValueError, IndexError):
            pass

    async def acquire(self):
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait_time = 0.0

            for limit, period in self._windows:
                times = self._request_times.setdefault(period, [])
                cutoff = now - period
                while times and times[0] < cutoff:
                    times.pop(0)
                if len(times) >= limit:
                    needed = times[0] + period - now
                    wait_time = max(wait_time, needed)

            if wait_time > 0:
                await asyncio.sleep(wait_time)
                now = loop.time()

            for limit, period in self._windows:
                self._request_times.setdefault(period, []).append(now)


class RiotClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None
        self._rate_limiter = RiotRateLimiter()
        self._entry_cache: dict = {}
        self.key_invalid = False

    async def start(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            self.session = aiohttp.ClientSession(
                headers={"X-Riot-Token": self.api_key},
                timeout=timeout,
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get(self, url: str) -> dict | list:
        if self.session is None or self.session.closed:
            await self.start()

        assert self.session is not None

        for attempt in range(4):
            await self._rate_limiter.acquire()

            try:
                async with self.session.get(url) as resp:
                    self._rate_limiter.update_from_header(
                        resp.headers.get("X-App-Rate-Limit")
                    )

                    if resp.status == 404:
                        raise ValueError(
                            "Riot account not found. / "
                            "Riot Account nicht gefunden."
                        )

                    if resp.status == 403:
                        self.key_invalid = True
                        raise ValueError(
                            "Riot API key invalid or expired. / "
                            "Riot API Key ungültig oder abgelaufen."
                        )

                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", "2"))
                        retry_after = max(1.0, min(retry_after, 120.0))
                        if attempt < 3:
                            logger.warning(
                                "Riot Rate Limit erreicht. Warte %.1f Sekunden.",
                                retry_after,
                            )
                            await asyncio.sleep(retry_after)
                            continue
                        raise ValueError(
                            "Riot rate limit reached. Please try again "
                            "later. / Riot Rate Limit erreicht. Später "
                            "erneut versuchen."
                        )

                    if resp.status >= 500:
                        if attempt < 3:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        raise ValueError(
                            f"Riot API is currently unreachable ({resp.status}). "
                            f"/ Riot API ist momentan nicht erreichbar ({resp.status})."
                        )

                    if resp.status >= 400:
                        text = await resp.text()
                        raise ValueError(
                            f"Riot API error {resp.status}: {text[:200]}"
                        )

                    self.key_invalid = False
                    return await resp.json()

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt >= 3:
                    raise ValueError(
                        "Riot API konnte nicht erreicht werden."
                    ) from exc
                await asyncio.sleep(2 ** attempt)

        raise ValueError("Riot API Anfrage fehlgeschlagen.")

    async def get_account_by_riot_id(
        self, game_name: str, tag_line: str, platform: str
    ) -> dict:
        regional = regional_for_platform(platform)
        url = (
            f"https://{regional}.api.riotgames.com/riot/account/v1/accounts/"
            f"by-riot-id/{quote(game_name, safe='')}/{quote(tag_line, safe='')}"
        )
        data = await self._get(url)
        if not isinstance(data, dict) or not data.get("puuid"):
            raise ValueError("Riot Account Antwort enthält keine PUUID.")
        return data

    async def get_account_by_puuid(self, puuid: str, platform: str) -> dict:
        regional = regional_for_platform(platform)
        url = (
            f"https://{regional}.api.riotgames.com/riot/account/v1/accounts/"
            f"by-puuid/{puuid}"
        )
        data = await self._get(url)
        if not isinstance(data, dict):
            raise ValueError("Ungültige Riot Account Antwort.")
        return data

    async def get_soloq_entry_by_puuid(
        self, puuid: str, platform: str
    ) -> Optional[RankedEntry]:
        platform = normalize_platform(platform)
        url = (
            f"https://{platform}.api.riotgames.com/lol/league/v4/entries/"
            f"by-puuid/{puuid}"
        )
        entries = await self._get(url)

        if not isinstance(entries, list):
            raise ValueError("Ungültige Riot Ranked Antwort.")

        result = None
        for entry in entries:
            if entry.get("queueType") == "RANKED_SOLO_5x5":
                result = RankedEntry(
                    tier=str(entry["tier"]).upper(),
                    rank=str(entry.get("rank", "I")).upper(),
                    lp=int(entry["leaguePoints"]),
                    wins=int(entry.get("wins", 0)),
                    losses=int(entry.get("losses", 0)),
                )
                break

        self._entry_cache[(puuid, platform)] = (
            asyncio.get_running_loop().time(),
            result,
        )
        return result

    async def get_soloq_entry_cached(
        self, puuid: str, platform: str, max_age: float = 900.0
    ) -> Optional[RankedEntry]:
        """Wie get_soloq_entry_by_puuid, nutzt aber einen frischen Wert
        aus dem laufenden Updatezyklus, statt Riot erneut abzufragen."""
        cached = self._entry_cache.get((puuid, normalize_platform(platform)))
        if cached and asyncio.get_running_loop().time() - cached[0] <= max_age:
            return cached[1]
        return await self.get_soloq_entry_by_puuid(puuid, platform)

    async def get_match_ids_by_puuid(
        self,
        puuid: str,
        platform: str,
        count: int = 5,
    ) -> list[str]:
        regional = regional_for_platform(platform)
        url = (
            f"https://{regional}.api.riotgames.com/lol/match/v5/matches/"
            f"by-puuid/{puuid}/ids?queue=420&start=0&count={max(1, min(count, 100))}"
        )
        data = await self._get(url)
        if not isinstance(data, list):
            raise ValueError("Ungültige Riot Match-ID Antwort.")
        return [str(x) for x in data]

    async def get_match(self, match_id: str, platform: str) -> dict:
        regional = regional_for_platform(platform)
        url = (
            f"https://{regional}.api.riotgames.com/lol/match/v5/matches/"
            f"{match_id}"
        )
        data = await self._get(url)
        if not isinstance(data, dict):
            raise ValueError("Ungültige Riot Match Antwort.")
        return data



class Database:
    def __init__(self, url: str):
        self.url = url
        self.pool: Optional[asyncpg.Pool] = None

    async def start(self):
        if self.pool is None:
            self.pool = await asyncpg.create_pool(
                self.url,
                min_size=1,
                max_size=5,
                command_timeout=30,
            )
        await self._create_schema()

    async def close(self):
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Datenbank ist nicht gestartet.")
        return self.pool

    async def _create_schema(self):
        pool = self._pool()

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS guilds (
                        guild_id BIGINT PRIMARY KEY,
                        guild_name TEXT NOT NULL,
                        plan TEXT NOT NULL DEFAULT 'free',
                        ladder_name TEXT NOT NULL DEFAULT 'LadderGG',
                        leaderboard_channel_id BIGINT,
                        google_sheet_id TEXT,
                        google_worksheet_name TEXT NOT NULL DEFAULT 'Leaderboard',
                        sheets_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)

                # Migration von der kleinen Test-Version:
                # CREATE TABLE IF NOT EXISTS ergänzt keine fehlenden Spalten.
                await conn.execute("""
                    ALTER TABLE guilds
                    ADD COLUMN IF NOT EXISTS ladder_name TEXT
                    NOT NULL DEFAULT 'LadderGG'
                """)
                await conn.execute("""
                    ALTER TABLE guilds
                    ADD COLUMN IF NOT EXISTS leaderboard_channel_id BIGINT
                """)
                await conn.execute("""
                    ALTER TABLE guilds
                    ADD COLUMN IF NOT EXISTS google_sheet_id TEXT
                """)
                await conn.execute("""
                    ALTER TABLE guilds
                    ADD COLUMN IF NOT EXISTS google_worksheet_name TEXT
                    NOT NULL DEFAULT 'Leaderboard'
                """)
                await conn.execute("""
                    ALTER TABLE guilds
                    ADD COLUMN IF NOT EXISTS sheets_enabled BOOLEAN
                    NOT NULL DEFAULT FALSE
                """)
                await conn.execute("""
                    ALTER TABLE guilds
                    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
                    NOT NULL DEFAULT NOW()
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS riot_accounts (
                        puuid TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        game_name TEXT NOT NULL,
                        tag_line TEXT NOT NULL,
                        current_lp INTEGER,
                        current_label TEXT,
                        tier TEXT,
                        rank TEXT,
                        league_points INTEGER,
                        wins INTEGER,
                        losses INTEGER,
                        last_riot_refresh TIMESTAMPTZ,
                        last_identity_refresh TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (puuid, platform)
                    )
                """)

                await conn.execute("""
                    ALTER TABLE riot_accounts
                    ADD COLUMN IF NOT EXISTS last_identity_refresh TIMESTAMPTZ
                """)


                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ladder_players (
                        guild_id BIGINT NOT NULL
                            REFERENCES guilds(guild_id)
                            ON DELETE CASCADE,
                        ladder_id BIGINT
                            REFERENCES ladders(ladder_id)
                            ON DELETE CASCADE,
                        discord_user_id BIGINT NOT NULL,
                        puuid TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        display_name TEXT NOT NULL,

                        alltime_peak_lp INTEGER NOT NULL DEFAULT 0,
                        alltime_peak_label TEXT NOT NULL DEFAULT '',
                        ladder_start_lp INTEGER NOT NULL DEFAULT 0,
                        ladder_start_label TEXT NOT NULL DEFAULT '',
                        new_peak_lp INTEGER NOT NULL DEFAULT 0,
                        new_peak_label TEXT NOT NULL DEFAULT '',
                        challenger_entry_lp INTEGER NOT NULL DEFAULT 0,

                        is_registered BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                        PRIMARY KEY (guild_id, discord_user_id),
                        FOREIGN KEY (puuid, platform)
                            REFERENCES riot_accounts(puuid, platform)
                            ON DELETE RESTRICT
                    )
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ladders (
                        ladder_id BIGSERIAL PRIMARY KEY,
                        guild_id BIGINT NOT NULL
                            REFERENCES guilds(guild_id)
                            ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        ladder_type TEXT NOT NULL
                            CHECK (ladder_type IN ('normal', 'challenge')),
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('draft', 'scheduled', 'active', 'finished', 'archived')),
                        starts_at TIMESTAMPTZ,
                        ends_at TIMESTAMPTZ,
                        rules JSONB NOT NULL DEFAULT '{}'::jsonb,
                        display_config JSONB NOT NULL DEFAULT '{}'::jsonb,
                        leaderboard_channel_id BIGINT,
                        is_default BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)

                await conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_default_ladder_per_guild
                    ON ladders (guild_id)
                    WHERE is_default = TRUE
                """)

                await conn.execute("""
                    ALTER TABLE ladder_players
                    ADD COLUMN IF NOT EXISTS ladder_id BIGINT
                """)
                await conn.execute("""
                    ALTER TABLE ladder_players
                    ADD COLUMN IF NOT EXISTS score DOUBLE PRECISION
                    NOT NULL DEFAULT 0
                """)
                await conn.execute("""
                    ALTER TABLE ladder_players
                    ADD COLUMN IF NOT EXISTS joined_at TIMESTAMPTZ
                    NOT NULL DEFAULT NOW()
                """)

                await conn.execute("""
                    INSERT INTO ladders (
                        guild_id,
                        name,
                        ladder_type,
                        status,
                        rules,
                        display_config,
                        leaderboard_channel_id,
                        is_default
                    )
                    SELECT
                        g.guild_id,
                        COALESCE(g.ladder_name, 'LadderGG'),
                        'challenge',
                        'active',
                        $1::jsonb,
                        '{}'::jsonb,
                        g.leaderboard_channel_id,
                        TRUE
                    FROM guilds g
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM ladders l
                        WHERE l.guild_id = g.guild_id
                          AND l.is_default = TRUE
                    )
                """, json.dumps(DEFAULT_CHALLENGE_RULES))

                await conn.execute("""
                    UPDATE ladder_players lp
                    SET ladder_id = l.ladder_id
                    FROM ladders l
                    WHERE lp.guild_id = l.guild_id
                      AND l.is_default = TRUE
                      AND lp.ladder_id IS NULL
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS leaderboard_messages (
                        guild_id BIGINT NOT NULL
                            REFERENCES guilds(guild_id)
                            ON DELETE CASCADE,
                        page_index INTEGER NOT NULL,
                        message_id BIGINT NOT NULL,
                        PRIMARY KEY (guild_id, page_index)
                    )
                """)

                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ladder_players_active
                    ON ladder_players (guild_id, is_registered)
                """)

                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ladder_players_account
                    ON ladder_players (puuid, platform)
                    WHERE is_registered = TRUE
                """)


                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_riot_accounts_refresh
                    ON riot_accounts (last_riot_refresh)
                """)

                # Multi-Ladder / Admin / Archive Erweiterungen
                await conn.execute("""
                    DROP INDEX IF EXISTS uq_default_ladder_per_guild
                """)
                await conn.execute("""
                    ALTER TABLE ladders
                    DROP CONSTRAINT IF EXISTS ladders_status_check
                """)
                await conn.execute("""
                    ALTER TABLE ladders
                    ADD CONSTRAINT ladders_status_check
                    CHECK (status IN (
                        'draft', 'scheduled', 'active', 'paused',
                        'finished', 'archived'
                    ))
                """)
                await conn.execute("""
                    ALTER TABLE ladders
                    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ
                """)
                await conn.execute("""
                    ALTER TABLE ladders
                    ADD COLUMN IF NOT EXISTS announcement_channel_id BIGINT
                """)
                await conn.execute("""
                    ALTER TABLE ladders
                    ADD COLUMN IF NOT EXISTS announcement_message_id BIGINT
                """)
                await conn.execute("""
                    ALTER TABLE ladders
                    ADD COLUMN IF NOT EXISTS join_mode TEXT NOT NULL DEFAULT 'open'
                """)
                await conn.execute("""
                    ALTER TABLE ladders
                    ADD COLUMN IF NOT EXISTS participant_limit INTEGER
                """)
                await conn.execute("""
                    ALTER TABLE ladders
                    ADD COLUMN IF NOT EXISTS admin_approval BOOLEAN NOT NULL DEFAULT FALSE
                """)
                await conn.execute("""
                    ALTER TABLE ladders
                    ADD COLUMN IF NOT EXISTS ping_role_id BIGINT
                """)
                await conn.execute("""
                    ALTER TABLE ladders
                    ADD COLUMN IF NOT EXISTS final_announce_pending
                    BOOLEAN NOT NULL DEFAULT FALSE
                """)
                await conn.execute("""
                    ALTER TABLE ladder_players
                    ADD COLUMN IF NOT EXISTS approved BOOLEAN NOT NULL DEFAULT TRUE
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ladder_message_pages (
                        guild_id BIGINT NOT NULL,
                        ladder_id BIGINT NOT NULL
                            REFERENCES ladders(ladder_id)
                            ON DELETE CASCADE,
                        page_index INTEGER NOT NULL,
                        message_id BIGINT NOT NULL,
                        PRIMARY KEY (guild_id, ladder_id, page_index)
                    )
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ladders_guild_status
                    ON ladders (guild_id, status)
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ladder_players_ladder
                    ON ladder_players (ladder_id, is_registered, approved)
                """)

                await conn.execute("""
                    ALTER TABLE guilds ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'de'
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS tracked_matches (
                        match_id TEXT PRIMARY KEY,
                        puuid TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        game_end_at TIMESTAMPTZ,
                        champion_name TEXT,
                        win BOOLEAN,
                        kills INTEGER NOT NULL DEFAULT 0,
                        deaths INTEGER NOT NULL DEFAULT 0,
                        assists INTEGER NOT NULL DEFAULT 0,
                        cs INTEGER NOT NULL DEFAULT 0,
                        damage INTEGER NOT NULL DEFAULT 0,
                        duration_seconds INTEGER NOT NULL DEFAULT 0,
                        rank_after_lp INTEGER,
                        rank_after_label TEXT,
                        lp_delta INTEGER,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tracked_matches_player
                    ON tracked_matches (puuid, platform, game_end_at DESC)
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS guild_tracking_settings (
                        guild_id BIGINT PRIMARY KEY
                            REFERENCES guilds(guild_id)
                            ON DELETE CASCADE,
                        match_alerts_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                        match_alert_channel_id BIGINT,
                        promotion_alerts_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        streak_alerts_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        daily_summary_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                        daily_summary_channel_id BIGINT,
                        last_daily_summary_date DATE,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS player_rank_snapshots (
                        puuid TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        snapshot_date DATE NOT NULL,
                        current_lp INTEGER,
                        current_label TEXT,
                        wins INTEGER NOT NULL DEFAULT 0,
                        losses INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (puuid, platform, snapshot_date)
                    )
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ladder_templates (
                        template_id BIGSERIAL PRIMARY KEY,
                        guild_id BIGINT NOT NULL
                            REFERENCES guilds(guild_id)
                            ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        ladder_type TEXT NOT NULL,
                        rules JSONB NOT NULL DEFAULT '{}'::jsonb,
                        display_config JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_by BIGINT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (guild_id, name)
                    )
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ladder_teams (
                        team_id BIGSERIAL PRIMARY KEY,
                        guild_id BIGINT NOT NULL,
                        ladder_id BIGINT NOT NULL
                            REFERENCES ladders(ladder_id)
                            ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        captain_user_id BIGINT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (ladder_id, name)
                    )
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ladder_team_members (
                        team_id BIGINT NOT NULL
                            REFERENCES ladder_teams(team_id)
                            ON DELETE CASCADE,
                        discord_user_id BIGINT NOT NULL,
                        joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (team_id, discord_user_id)
                    )
                """)

                await conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_ladder_team_member
                    ON ladder_team_members (discord_user_id, team_id)
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ladder_audit_log (
                        audit_id BIGSERIAL PRIMARY KEY,
                        guild_id BIGINT NOT NULL,
                        ladder_id BIGINT,
                        actor_user_id BIGINT,
                        action TEXT NOT NULL,
                        details JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)

                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ladder_audit_log
                    ON ladder_audit_log (guild_id, ladder_id, created_at DESC)
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                await conn.execute("""
                    INSERT INTO schema_meta (key, value)
                    VALUES ('schema_version', '9')
                    ON CONFLICT (key)
                    DO UPDATE SET value='9', updated_at=NOW()
                """)
                await conn.execute("""
                    ALTER TABLE guilds
                    ADD COLUMN IF NOT EXISTS ladder_admin_role_id BIGINT
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ladder_invites (
                        guild_id BIGINT NOT NULL,
                        ladder_id BIGINT NOT NULL
                            REFERENCES ladders(ladder_id)
                            ON DELETE CASCADE,
                        discord_user_id BIGINT NOT NULL,
                        invited_by BIGINT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (ladder_id, discord_user_id)
                    )
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ladder_invites_guild
                    ON ladder_invites (guild_id, ladder_id)
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ladder_events (
                        event_id BIGSERIAL PRIMARY KEY,
                        guild_id BIGINT NOT NULL,
                        ladder_id BIGINT NOT NULL
                            REFERENCES ladders(ladder_id)
                            ON DELETE CASCADE,
                        discord_user_id BIGINT,
                        event_type TEXT NOT NULL,
                        event_key TEXT NOT NULL,
                        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (ladder_id, event_key)
                    )
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ladder_events_ladder
                    ON ladder_events (ladder_id, created_at DESC)
                """)

                # Multi-Ladder: Primärschlüssel/Unique-Index müssen ladder_id enthalten.
                await conn.execute("""
                    DROP INDEX IF EXISTS uq_active_riot_account_per_guild
                """)
                await conn.execute("""
                    ALTER TABLE ladder_players
                    DROP CONSTRAINT IF EXISTS ladder_players_guild_id_puuid_platform_key
                """)
                await conn.execute("""
                    ALTER TABLE ladder_players
                    DROP CONSTRAINT IF EXISTS ladder_players_pkey
                """)
                await conn.execute("""
                    ALTER TABLE ladder_players
                    ADD CONSTRAINT ladder_players_pkey
                    PRIMARY KEY (guild_id, ladder_id, discord_user_id)
                """)
                await conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_active_riot_account_per_ladder
                    ON ladder_players (guild_id, ladder_id, puuid, platform)
                    WHERE is_registered = TRUE
                """)

    async def ensure_guild(self, guild: discord.Guild):
        pool = self._pool()
        await pool.execute("""
            INSERT INTO guilds (guild_id, guild_name, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (guild_id)
            DO UPDATE SET
                guild_name = EXCLUDED.guild_name,
                updated_at = NOW()
        """, guild.id, guild.name)

    async def get_default_ladder(self, guild_id: int):
        pool = self._pool()
        return await pool.fetchrow("""
            SELECT *
            FROM ladders
            WHERE guild_id = $1
              AND is_default = TRUE
            LIMIT 1
        """, guild_id)

    async def create_or_replace_default_ladder(
        self,
        guild_id: int,
        name: str,
        ladder_type: str,
        leaderboard_channel_id: int,
        rules: Optional[dict] = None,
        starts_at: Optional[datetime] = None,
        ends_at: Optional[datetime] = None,
    ):
        if ladder_type not in LADDER_TYPES:
            raise ValueError("Ungültiger Ladder-Typ.")

        pool = self._pool()
        rules = rules or (clone_default_challenge_rules() if ladder_type == "challenge" else {})

        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow("""
                    SELECT ladder_id
                    FROM ladders
                    WHERE guild_id = $1
                      AND is_default = TRUE
                    LIMIT 1
                """, guild_id)

                if existing:
                    ladder_id = int(existing["ladder_id"])
                    await conn.execute("""
                        UPDATE ladders
                        SET name = $2,
                            ladder_type = $3,
                            status = 'active',
                            starts_at = $4,
                            ends_at = $5,
                            rules = $6::jsonb,
                            leaderboard_channel_id = $7,
                            updated_at = NOW()
                        WHERE ladder_id = $1
                    """,
                        ladder_id,
                        name,
                        ladder_type,
                        starts_at,
                        ends_at,
                        json.dumps(rules),
                        leaderboard_channel_id,
                    )
                else:
                    ladder_id = await conn.fetchval("""
                        INSERT INTO ladders (
                            guild_id,
                            name,
                            ladder_type,
                            status,
                            starts_at,
                            ends_at,
                            rules,
                            leaderboard_channel_id,
                            is_default
                        )
                        VALUES ($1, $2, $3, 'active', $4, $5, $6::jsonb, $7, TRUE)
                        RETURNING ladder_id
                    """,
                        guild_id,
                        name,
                        ladder_type,
                        starts_at,
                        ends_at,
                        json.dumps(rules),
                        leaderboard_channel_id,
                    )

                await conn.execute("""
                    UPDATE ladder_players
                    SET ladder_id = $2
                    WHERE guild_id = $1
                """, guild_id, ladder_id)

                return int(ladder_id)

    async def update_default_ladder_rules(self, guild_id: int, rules: dict):
        pool = self._pool()
        await pool.execute("""
            UPDATE ladders
            SET rules = $2::jsonb,
                updated_at = NOW()
            WHERE guild_id = $1
              AND is_default = TRUE
        """, guild_id, json.dumps(rules))

    async def list_ladders(
        self,
        guild_id: int,
        include_archived: bool = True,
    ):
        pool = self._pool()
        if include_archived:
            return await pool.fetch("""
                SELECT *
                FROM ladders
                WHERE guild_id = $1
                ORDER BY
                    CASE status
                        WHEN 'active' THEN 0
                        WHEN 'scheduled' THEN 1
                        WHEN 'paused' THEN 2
                        WHEN 'finished' THEN 3
                        WHEN 'archived' THEN 4
                        ELSE 5
                    END,
                    created_at DESC
            """, guild_id)
        return await pool.fetch("""
            SELECT *
            FROM ladders
            WHERE guild_id = $1
              AND status <> 'archived'
            ORDER BY created_at DESC
        """, guild_id)

    async def get_ladder(self, guild_id: int, ladder_id: int):
        pool = self._pool()
        return await pool.fetchrow("""
            SELECT *
            FROM ladders
            WHERE guild_id = $1
              AND ladder_id = $2
        """, guild_id, ladder_id)

    async def create_ladder(
        self,
        guild_id: int,
        name: str,
        ladder_type: str,
        leaderboard_channel_id: int,
        rules: Optional[dict] = None,
        starts_at: Optional[datetime] = None,
        ends_at: Optional[datetime] = None,
        announcement_channel_id: Optional[int] = None,
        participant_limit: Optional[int] = None,
        admin_approval: bool = False,
        ping_role_id: Optional[int] = None,
    ) -> int:
        if ladder_type not in LADDER_TYPES:
            raise ValueError("Ungültiger Ladder-Typ.")

        pool = self._pool()
        rules = rules or (
            clone_default_challenge_rules()
            if ladder_type == "challenge"
            else {}
        )

        ladder_id = await pool.fetchval("""
            INSERT INTO ladders (
                guild_id,
                name,
                ladder_type,
                status,
                starts_at,
                ends_at,
                rules,
                display_config,
                leaderboard_channel_id,
                is_default,
                announcement_channel_id,
                participant_limit,
                admin_approval,
                ping_role_id
            )
            VALUES (
                $1, $2, $3, 'active', $4, $5, $6::jsonb, '{}'::jsonb,
                $7, FALSE, $8, $9, $10, $11
            )
            RETURNING ladder_id
        """,
            guild_id,
            name,
            ladder_type,
            starts_at,
            ends_at,
            json.dumps(rules),
            leaderboard_channel_id,
            announcement_channel_id,
            participant_limit,
            admin_approval,
            ping_role_id,
        )
        return int(ladder_id)

    async def update_ladder(
        self,
        guild_id: int,
        ladder_id: int,
        *,
        name: str,
        leaderboard_channel_id: int,
        rules: dict,
        starts_at: Optional[datetime] = None,
        ends_at: Optional[datetime] = None,
        announcement_channel_id: Optional[int] = None,
        participant_limit: Optional[int] = None,
        admin_approval: bool = False,
    ):
        pool = self._pool()
        await pool.execute("""
            UPDATE ladders
            SET name = $3,
                leaderboard_channel_id = $4,
                rules = $5::jsonb,
                starts_at = $6,
                ends_at = $7,
                announcement_channel_id = $8,
                participant_limit = $9,
                admin_approval = $10,
                updated_at = NOW()
            WHERE guild_id = $1
              AND ladder_id = $2
        """,
            guild_id,
            ladder_id,
            name,
            leaderboard_channel_id,
            json.dumps(rules),
            starts_at,
            ends_at,
            announcement_channel_id,
            participant_limit,
            admin_approval,
        )

    async def update_ladder_rules(
        self,
        guild_id: int,
        ladder_id: int,
        rules: dict,
    ):
        pool = self._pool()
        await pool.execute("""
            UPDATE ladders
            SET rules = $3::jsonb,
                updated_at = NOW()
            WHERE guild_id = $1
              AND ladder_id = $2
        """, guild_id, ladder_id, json.dumps(rules))

    async def set_final_announce_pending(
        self,
        guild_id: int,
        ladder_id: int,
        pending: bool,
    ):
        pool = self._pool()
        await pool.execute("""
            UPDATE ladders
            SET final_announce_pending = $3
            WHERE guild_id = $1
              AND ladder_id = $2
        """, guild_id, ladder_id, pending)

    async def set_ladder_status(
        self,
        guild_id: int,
        ladder_id: int,
        status: str,
    ):
        allowed = {
            "draft", "scheduled", "active", "paused", "finished", "archived"
        }
        if status not in allowed:
            raise ValueError("Ungültiger Status.")
        pool = self._pool()
        await pool.execute("""
            UPDATE ladders
            SET status = $3,
                archived_at = CASE
                    WHEN $3 = 'archived' THEN NOW()
                    ELSE archived_at
                END,
                updated_at = NOW()
            WHERE guild_id = $1
              AND ladder_id = $2
        """, guild_id, ladder_id, status)

    async def duplicate_ladder(
        self,
        guild_id: int,
        ladder_id: int,
        new_name: str,
    ) -> int:
        ladder = await self.get_ladder(guild_id, ladder_id)
        if not ladder:
            raise ValueError("Ladder not found. / Ladder nicht gefunden.")

        pool = self._pool()
        new_id = await pool.fetchval("""
            INSERT INTO ladders (
                guild_id, name, ladder_type, status, rules, display_config,
                leaderboard_channel_id, is_default,
                announcement_channel_id, participant_limit,
                admin_approval, ping_role_id
            )
            VALUES (
                $1, $2, $3, 'draft', $4::jsonb, $5::jsonb,
                $6, FALSE, $7, $8, $9, $10
            )
            RETURNING ladder_id
        """,
            guild_id,
            new_name,
            ladder["ladder_type"],
            json.dumps(parse_rules_jsonb(ladder["rules"])),
            json.dumps(parse_rules_jsonb(ladder["display_config"])),
            ladder["leaderboard_channel_id"],
            ladder["announcement_channel_id"],
            ladder["participant_limit"],
            ladder["admin_approval"],
            ladder["ping_role_id"],
        )
        return int(new_id)

    async def set_announcement_message(
        self,
        guild_id: int,
        ladder_id: int,
        message_id: int,
    ):
        pool = self._pool()
        await pool.execute("""
            UPDATE ladders
            SET announcement_message_id = $3,
                updated_at = NOW()
            WHERE guild_id = $1
              AND ladder_id = $2
        """, guild_id, ladder_id, message_id)

    async def players_for_ladder(
        self,
        guild_id: int,
        ladder_id: int,
        approved_only: bool = True,
    ):
        pool = self._pool()
        approval_clause = "AND lp.approved = TRUE" if approved_only else ""
        return await pool.fetch(f"""
            SELECT
                lp.*,
                ra.game_name,
                ra.tag_line,
                ra.current_lp,
                ra.current_label,
                ra.tier,
                ra.rank,
                ra.league_points,
                ra.wins,
                ra.losses,
                ra.last_riot_refresh
            FROM ladder_players lp
            JOIN riot_accounts ra
              ON ra.puuid = lp.puuid
             AND ra.platform = lp.platform
            WHERE lp.guild_id = $1
              AND lp.ladder_id = $2
              AND lp.is_registered = TRUE
              {approval_clause}
        """, guild_id, ladder_id)

    async def participant_count(
        self,
        guild_id: int,
        ladder_id: int,
    ) -> int:
        pool = self._pool()
        return int(await pool.fetchval("""
            SELECT COUNT(*)
            FROM ladder_players
            WHERE guild_id = $1
              AND ladder_id = $2
              AND is_registered = TRUE
        """, guild_id, ladder_id))

    async def register_player_for_ladder(
        self,
        guild_id: int,
        ladder_id: int,
        discord_user_id: int,
        puuid: str,
        platform: str,
        display_name: str,
        start_lp: int,
        start_label: str,
        entry: RankedEntry,
        approved: bool = True,
    ):
        pool = self._pool()
        challenger_entry_lp = (
            start_lp
            if start_label.lower().startswith("challenger")
            else (entry.absolute_lp if entry.tier == "CHALLENGER" else 0)
        )

        async with pool.acquire() as conn:
            async with conn.transaction():
                duplicate = await conn.fetchrow("""
                    SELECT discord_user_id
                    FROM ladder_players
                    WHERE guild_id = $1
                      AND ladder_id = $2
                      AND puuid = $3
                      AND platform = $4
                      AND discord_user_id <> $5
                      AND is_registered = TRUE
                """,
                    guild_id,
                    ladder_id,
                    puuid,
                    platform,
                    discord_user_id,
                )
                if duplicate:
                    raise ValueError(
                        "Dieser Riot Account ist in dieser Ladder bereits registriert."
                    )

                existing = await conn.fetchrow("""
                    SELECT 1
                    FROM ladder_players
                    WHERE guild_id = $1
                      AND ladder_id = $2
                      AND discord_user_id = $3
                """, guild_id, ladder_id, discord_user_id)

                if existing:
                    await conn.execute("""
                        UPDATE ladder_players
                        SET puuid = $4,
                            platform = $5,
                            display_name = $6,
                            alltime_peak_lp = $7,
                            alltime_peak_label = $8,
                            ladder_start_lp = $7,
                            ladder_start_label = $8,
                            new_peak_lp = $9,
                            new_peak_label = $10,
                            challenger_entry_lp = $11,
                            is_registered = TRUE,
                            approved = $12,
                            updated_at = NOW()
                        WHERE guild_id = $1
                          AND ladder_id = $2
                          AND discord_user_id = $3
                    """,
                        guild_id, ladder_id, discord_user_id,
                        puuid, platform, display_name,
                        start_lp, start_label,
                        entry.absolute_lp, entry.label,
                        challenger_entry_lp, approved,
                    )
                else:
                    await conn.execute("""
                        INSERT INTO ladder_players (
                            guild_id, ladder_id, discord_user_id,
                            puuid, platform, display_name,
                            alltime_peak_lp, alltime_peak_label,
                            ladder_start_lp, ladder_start_label,
                            new_peak_lp, new_peak_label,
                            challenger_entry_lp,
                            is_registered, approved, updated_at
                        )
                        VALUES (
                            $1,$2,$3,$4,$5,$6,$7,$8,$7,$8,
                            $9,$10,$11,TRUE,$12,NOW()
                        )
                    """,
                        guild_id, ladder_id, discord_user_id,
                        puuid, platform, display_name,
                        start_lp, start_label,
                        entry.absolute_lp, entry.label,
                        challenger_entry_lp, approved,
                    )

    async def get_ladder_message_ids(
        self,
        guild_id: int,
        ladder_id: int,
    ):
        pool = self._pool()
        rows = await pool.fetch("""
            SELECT page_index, message_id
            FROM ladder_message_pages
            WHERE guild_id = $1
              AND ladder_id = $2
            ORDER BY page_index
        """, guild_id, ladder_id)
        return [int(r["message_id"]) for r in rows]

    async def replace_ladder_message_ids(
        self,
        guild_id: int,
        ladder_id: int,
        message_ids: list[int],
    ):
        pool = self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    DELETE FROM ladder_message_pages
                    WHERE guild_id = $1
                      AND ladder_id = $2
                """, guild_id, ladder_id)
                if message_ids:
                    await conn.executemany("""
                        INSERT INTO ladder_message_pages (
                            guild_id, ladder_id, page_index, message_id
                        )
                        VALUES ($1, $2, $3, $4)
                    """, [
                        (guild_id, ladder_id, idx, message_id)
                        for idx, message_id in enumerate(message_ids)
                    ])

    async def get_guild_language(self, guild_id: int) -> str:
        pool = self._pool()
        lang = await pool.fetchval("SELECT language FROM guilds WHERE guild_id=$1", guild_id)
        return lang if lang in {"de", "en"} else "de"

    async def set_guild_language(self, guild_id: int, language: str):
        pool = self._pool()
        language = language if language in {"de", "en"} else "de"
        await pool.execute("UPDATE guilds SET language=$2, updated_at=NOW() WHERE guild_id=$1", guild_id, language)

    async def ensure_tracking_settings(self, guild_id: int):
        pool = self._pool()
        await pool.execute("""
            INSERT INTO guilds (guild_id, guild_name)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO NOTHING
        """, guild_id, f"Guild {guild_id}")
        await pool.execute("""
            INSERT INTO guild_tracking_settings (guild_id, match_alerts_enabled)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO NOTHING
        """, guild_id, MATCH_ALERTS_ENABLED_DEFAULT)

    async def get_tracking_settings(self, guild_id: int):
        await self.ensure_tracking_settings(guild_id)
        pool = self._pool()
        return await pool.fetchrow("""
            SELECT * FROM guild_tracking_settings WHERE guild_id = $1
        """, guild_id)

    async def update_tracking_settings(
        self,
        guild_id: int,
        match_alerts_enabled: Optional[bool] = None,
        match_alert_channel_id: Optional[int] = None,
        promotion_alerts_enabled: Optional[bool] = None,
        streak_alerts_enabled: Optional[bool] = None,
        daily_summary_enabled: Optional[bool] = None,
        daily_summary_channel_id: Optional[int] = None,
    ):
        await self.ensure_tracking_settings(guild_id)
        pool = self._pool()
        await pool.execute("""
            UPDATE guild_tracking_settings
            SET match_alerts_enabled = COALESCE($2, match_alerts_enabled),
                match_alert_channel_id = COALESCE($3, match_alert_channel_id),
                promotion_alerts_enabled = COALESCE($4, promotion_alerts_enabled),
                streak_alerts_enabled = COALESCE($5, streak_alerts_enabled),
                daily_summary_enabled = COALESCE($6, daily_summary_enabled),
                daily_summary_channel_id = COALESCE($7, daily_summary_channel_id),
                updated_at = NOW()
            WHERE guild_id = $1
        """, guild_id, match_alerts_enabled, match_alert_channel_id,
             promotion_alerts_enabled, streak_alerts_enabled,
             daily_summary_enabled, daily_summary_channel_id)

    async def tracked_match_exists(self, match_id: str) -> bool:
        pool = self._pool()
        return bool(await pool.fetchval("""
            SELECT EXISTS(SELECT 1 FROM tracked_matches WHERE match_id = $1)
        """, match_id))

    async def insert_tracked_match(
        self, match_id: str, puuid: str, platform: str,
        game_end_at: Optional[datetime], champion_name: str, win: bool,
        kills: int, deaths: int, assists: int, cs: int, damage: int,
        duration_seconds: int, rank_after_lp: Optional[int],
        rank_after_label: Optional[str], lp_delta: Optional[int],
    ) -> bool:
        pool = self._pool()
        result = await pool.execute("""
            INSERT INTO tracked_matches (
                match_id, puuid, platform, game_end_at, champion_name,
                win, kills, deaths, assists, cs, damage, duration_seconds,
                rank_after_lp, rank_after_label, lp_delta
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            ON CONFLICT (match_id) DO NOTHING
        """, match_id, puuid, platform, game_end_at, champion_name,
             win, kills, deaths, assists, cs, damage, duration_seconds,
             rank_after_lp, rank_after_label, lp_delta)
        return result.endswith("1")

    async def recent_matches(self, puuid: str, platform: str, limit: int = 20):
        pool = self._pool()
        return await pool.fetch("""
            SELECT * FROM tracked_matches
            WHERE puuid = $1 AND platform = $2
            ORDER BY game_end_at DESC NULLS LAST, created_at DESC
            LIMIT $3
        """, puuid, platform, limit)

    async def save_daily_snapshot(
        self, puuid: str, platform: str,
        entry: RankedEntry, snapshot_date,
    ):
        pool = self._pool()
        await pool.execute("""
            INSERT INTO player_rank_snapshots (
                puuid, platform, snapshot_date,
                current_lp, current_label, wins, losses
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (puuid, platform, snapshot_date)
            DO UPDATE SET
                current_lp = EXCLUDED.current_lp,
                current_label = EXCLUDED.current_label,
                wins = EXCLUDED.wins,
                losses = EXCLUDED.losses
        """, puuid, platform, snapshot_date, entry.absolute_lp,
             entry.label, entry.wins, entry.losses)

    async def daily_snapshots_for_guild(self, guild_id: int, snapshot_date):
        pool = self._pool()
        return await pool.fetch("""
            SELECT DISTINCT lp.display_name, lp.puuid, lp.platform,
                t.current_lp AS today_lp, t.current_label AS today_label,
                y.current_lp AS yesterday_lp, y.current_label AS yesterday_label
            FROM ladder_players lp
            LEFT JOIN player_rank_snapshots t
              ON t.puuid=lp.puuid AND t.platform=lp.platform
             AND t.snapshot_date=$2
            LEFT JOIN player_rank_snapshots y
              ON y.puuid=lp.puuid AND y.platform=lp.platform
             AND y.snapshot_date=($2::date - INTERVAL '1 day')::date
            WHERE lp.guild_id=$1 AND lp.is_registered=TRUE
        """, guild_id, snapshot_date)

    async def mark_daily_summary_sent(self, guild_id: int, summary_date):
        pool = self._pool()
        await pool.execute("""
            UPDATE guild_tracking_settings
            SET last_daily_summary_date=$2, updated_at=NOW()
            WHERE guild_id=$1
        """, guild_id, summary_date)

    async def audit(
        self,
        guild_id: int,
        action: str,
        *,
        ladder_id: Optional[int] = None,
        actor_user_id: Optional[int] = None,
        details: Optional[dict] = None,
    ):
        pool = self._pool()
        await pool.execute("""
            INSERT INTO ladder_audit_log (
                guild_id, ladder_id, actor_user_id, action, details
            )
            VALUES ($1,$2,$3,$4,$5::jsonb)
        """,
            guild_id,
            ladder_id,
            actor_user_id,
            action,
            json.dumps(details or {}),
        )

    async def audit_rows(
        self,
        guild_id: int,
        ladder_id: Optional[int] = None,
        limit: int = 20,
    ):
        pool = self._pool()
        return await pool.fetch("""
            SELECT *
            FROM ladder_audit_log
            WHERE guild_id = $1
              AND ($2::bigint IS NULL OR ladder_id = $2)
            ORDER BY created_at DESC
            LIMIT $3
        """, guild_id, ladder_id, limit)

    async def save_template(
        self,
        guild_id: int,
        name: str,
        ladder_type: str,
        rules: dict,
        display_config: Optional[dict],
        created_by: Optional[int],
    ):
        pool = self._pool()
        await pool.execute("""
            INSERT INTO ladder_templates (
                guild_id, name, ladder_type, rules,
                display_config, created_by
            )
            VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6)
            ON CONFLICT (guild_id, name)
            DO UPDATE SET
                ladder_type = EXCLUDED.ladder_type,
                rules = EXCLUDED.rules,
                display_config = EXCLUDED.display_config,
                created_by = EXCLUDED.created_by,
                created_at = NOW()
        """,
            guild_id, name, ladder_type,
            json.dumps(rules),
            json.dumps(display_config or {}),
            created_by,
        )

    async def list_templates(self, guild_id: int):
        pool = self._pool()
        return await pool.fetch("""
            SELECT *
            FROM ladder_templates
            WHERE guild_id = $1
            ORDER BY name
        """, guild_id)

    async def get_template(self, guild_id: int, template_id: int):
        pool = self._pool()
        return await pool.fetchrow("""
            SELECT *
            FROM ladder_templates
            WHERE guild_id = $1
              AND template_id = $2
        """, guild_id, template_id)

    async def create_team(
        self,
        guild_id: int,
        ladder_id: int,
        name: str,
        captain_user_id: int,
    ) -> int:
        pool = self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                team_id = await conn.fetchval("""
                    INSERT INTO ladder_teams (
                        guild_id, ladder_id, name, captain_user_id
                    )
                    VALUES ($1,$2,$3,$4)
                    RETURNING team_id
                """, guild_id, ladder_id, name, captain_user_id)
                await conn.execute("""
                    INSERT INTO ladder_team_members (
                        team_id, discord_user_id
                    )
                    VALUES ($1,$2)
                    ON CONFLICT DO NOTHING
                """, team_id, captain_user_id)
                return int(team_id)

    async def join_team(
        self,
        guild_id: int,
        ladder_id: int,
        team_id: int,
        discord_user_id: int,
    ):
        pool = self._pool()
        exists = await pool.fetchval("""
            SELECT EXISTS(
                SELECT 1 FROM ladder_teams
                WHERE guild_id=$1 AND ladder_id=$2 AND team_id=$3
            )
        """, guild_id, ladder_id, team_id)
        if not exists:
            raise ValueError("Team not found. / Team nicht gefunden.")

        await pool.execute("""
            DELETE FROM ladder_team_members m
            USING ladder_teams t
            WHERE m.team_id = t.team_id
              AND t.guild_id = $1
              AND t.ladder_id = $2
              AND m.discord_user_id = $3
        """, guild_id, ladder_id, discord_user_id)

        await pool.execute("""
            INSERT INTO ladder_team_members (
                team_id, discord_user_id
            )
            VALUES ($1,$2)
            ON CONFLICT DO NOTHING
        """, team_id, discord_user_id)

    async def teams_for_ladder(
        self,
        guild_id: int,
        ladder_id: int,
    ):
        pool = self._pool()
        return await pool.fetch("""
            SELECT
                t.team_id,
                t.name,
                t.captain_user_id,
                COUNT(m.discord_user_id)::int AS member_count
            FROM ladder_teams t
            LEFT JOIN ladder_team_members m
              ON m.team_id = t.team_id
            WHERE t.guild_id = $1
              AND t.ladder_id = $2
            GROUP BY t.team_id
            ORDER BY t.name
        """, guild_id, ladder_id)

    async def team_member_ids(self, team_id: int):
        pool = self._pool()
        rows = await pool.fetch("""
            SELECT discord_user_id
            FROM ladder_team_members
            WHERE team_id = $1
        """, team_id)
        return [int(r["discord_user_id"]) for r in rows]

    async def dashboard_counts(self, guild_id: int):
        pool = self._pool()
        return await pool.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE status='active')::int AS active,
                COUNT(*) FILTER (WHERE status='scheduled')::int AS scheduled,
                COUNT(*) FILTER (WHERE status='paused')::int AS paused,
                COUNT(*) FILTER (WHERE status='archived')::int AS archived
            FROM ladders
            WHERE guild_id = $1
        """, guild_id)

    async def get_schema_version(self) -> str:
        pool = self._pool()
        return str(
            await pool.fetchval(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            )
            or "unknown"
        )

    async def set_ladder_admin_role(
        self,
        guild_id: int,
        role_id: Optional[int],
    ):
        pool = self._pool()
        await pool.execute("""
            UPDATE guilds
            SET ladder_admin_role_id=$2,
                updated_at=NOW()
            WHERE guild_id=$1
        """, guild_id, role_id)

    async def add_ladder_invite(
        self,
        guild_id: int,
        ladder_id: int,
        discord_user_id: int,
        invited_by: Optional[int],
    ):
        pool = self._pool()
        await pool.execute("""
            INSERT INTO ladder_invites (
                guild_id, ladder_id, discord_user_id, invited_by
            )
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (ladder_id, discord_user_id)
            DO NOTHING
        """, guild_id, ladder_id, discord_user_id, invited_by)

    async def remove_ladder_invite(
        self,
        guild_id: int,
        ladder_id: int,
        discord_user_id: int,
    ):
        pool = self._pool()
        await pool.execute("""
            DELETE FROM ladder_invites
            WHERE guild_id=$1
              AND ladder_id=$2
              AND discord_user_id=$3
        """, guild_id, ladder_id, discord_user_id)

    async def is_ladder_invited(
        self,
        guild_id: int,
        ladder_id: int,
        discord_user_id: int,
    ) -> bool:
        pool = self._pool()
        return bool(await pool.fetchval("""
            SELECT EXISTS(
                SELECT 1 FROM ladder_invites
                WHERE guild_id=$1
                  AND ladder_id=$2
                  AND discord_user_id=$3
            )
        """, guild_id, ladder_id, discord_user_id))

    async def ladder_invites(
        self,
        guild_id: int,
        ladder_id: int,
    ):
        pool = self._pool()
        return await pool.fetch("""
            SELECT *
            FROM ladder_invites
            WHERE guild_id=$1 AND ladder_id=$2
            ORDER BY created_at
        """, guild_id, ladder_id)

    async def snapshot_for_player_on_or_before(
        self,
        puuid: str,
        platform: str,
        snapshot_date,
    ):
        pool = self._pool()
        return await pool.fetchrow("""
            SELECT *
            FROM player_rank_snapshots
            WHERE puuid=$1
              AND platform=$2
              AND snapshot_date <= $3
            ORDER BY snapshot_date DESC
            LIMIT 1
        """, puuid, platform, snapshot_date)

    async def delete_ladder(
        self,
        guild_id: int,
        ladder_id: int,
    ):
        pool = self._pool()
        await pool.execute("""
            DELETE FROM ladders
            WHERE guild_id=$1 AND ladder_id=$2
        """, guild_id, ladder_id)

    async def delete_template(
        self,
        guild_id: int,
        template_id: int,
    ):
        pool = self._pool()
        await pool.execute("""
            DELETE FROM ladder_templates
            WHERE guild_id=$1 AND template_id=$2
        """, guild_id, template_id)

    async def rename_template(
        self,
        guild_id: int,
        template_id: int,
        name: str,
    ):
        pool = self._pool()
        await pool.execute("""
            UPDATE ladder_templates
            SET name=$3
            WHERE guild_id=$1 AND template_id=$2
        """, guild_id, template_id, name)

    async def leave_team(
        self,
        guild_id: int,
        ladder_id: int,
        discord_user_id: int,
    ):
        pool = self._pool()
        await pool.execute("""
            DELETE FROM ladder_team_members m
            USING ladder_teams t
            WHERE m.team_id=t.team_id
              AND t.guild_id=$1
              AND t.ladder_id=$2
              AND m.discord_user_id=$3
        """, guild_id, ladder_id, discord_user_id)

    async def kick_team_member(
        self,
        guild_id: int,
        ladder_id: int,
        team_id: int,
        discord_user_id: int,
        captain_user_id: int,
    ):
        pool = self._pool()
        captain = await pool.fetchval("""
            SELECT captain_user_id
            FROM ladder_teams
            WHERE guild_id=$1 AND ladder_id=$2 AND team_id=$3
        """, guild_id, ladder_id, team_id)
        if captain is None:
            raise ValueError("Team not found.")
        if int(captain) != captain_user_id:
            raise PermissionError("Only the team captain can remove members.")
        if discord_user_id == captain_user_id:
            raise ValueError("The captain cannot remove themselves.")
        await pool.execute("""
            DELETE FROM ladder_team_members
            WHERE team_id=$1 AND discord_user_id=$2
        """, team_id, discord_user_id)

    async def delete_team(
        self,
        guild_id: int,
        ladder_id: int,
        team_id: int,
        captain_user_id: int,
    ):
        pool = self._pool()
        captain = await pool.fetchval("""
            SELECT captain_user_id
            FROM ladder_teams
            WHERE guild_id=$1 AND ladder_id=$2 AND team_id=$3
        """, guild_id, ladder_id, team_id)
        if captain is None:
            raise ValueError("Team not found.")
        if int(captain) != captain_user_id:
            raise PermissionError("Only the team captain can delete the team.")
        await pool.execute("""
            DELETE FROM ladder_teams
            WHERE guild_id=$1 AND ladder_id=$2 AND team_id=$3
        """, guild_id, ladder_id, team_id)

    async def record_ladder_event(
        self,
        guild_id: int,
        ladder_id: int,
        event_type: str,
        event_key: str,
        payload: dict,
        discord_user_id: Optional[int] = None,
    ) -> bool:
        pool = self._pool()
        result = await pool.execute("""
            INSERT INTO ladder_events (
                guild_id, ladder_id, discord_user_id,
                event_type, event_key, payload
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (ladder_id, event_key) DO NOTHING
        """,
            guild_id,
            ladder_id,
            discord_user_id,
            event_type,
            event_key,
            json.dumps(payload),
        )
        return result.endswith("1")

    async def last_event_of_type(self, guild_id: int, ladder_id: int, event_type: str):
        pool = self._pool()
        return await pool.fetchrow("""
            SELECT * FROM ladder_events
            WHERE guild_id=$1 AND ladder_id=$2 AND event_type=$3
            ORDER BY created_at DESC LIMIT 1
        """, guild_id, ladder_id, event_type)

    async def events_for_ladder(
        self,
        guild_id: int,
        ladder_id: int,
        limit: int = 50,
    ):
        pool = self._pool()
        return await pool.fetch("""
            SELECT *
            FROM ladder_events
            WHERE guild_id = $1
              AND ladder_id = $2
            ORDER BY created_at DESC
            LIMIT $3
        """, guild_id, ladder_id, limit)

    async def hall_of_fame_rows(
        self,
        guild_id: int,
        limit: int = 20,
    ):
        pool = self._pool()
        return await pool.fetch("""
            SELECT
                lp.display_name,
                lp.discord_user_id,
                l.name AS ladder_name,
                l.ladder_type,
                l.archived_at,
                lp.ladder_start_lp,
                lp.new_peak_lp,
                lp.new_peak_label,
                lp.alltime_peak_lp,
                lp.alltime_peak_label,
                ra.current_lp,
                ra.current_label,
                ra.wins,
                ra.losses
            FROM ladder_players lp
            JOIN ladders l
              ON l.ladder_id = lp.ladder_id
             AND l.guild_id = lp.guild_id
            JOIN riot_accounts ra
              ON ra.puuid = lp.puuid
             AND ra.platform = lp.platform
            WHERE lp.guild_id = $1
              AND l.status = 'archived'
              AND lp.is_registered = TRUE
            ORDER BY (lp.new_peak_lp - lp.ladder_start_lp) DESC,
                     lp.new_peak_lp DESC
            LIMIT $2
        """, guild_id, limit)

    async def set_ladder_name(self, guild_id: int, ladder_name: str):
        pool = self._pool()
        await pool.execute("""
            UPDATE guilds
            SET ladder_name = $2,
                updated_at = NOW()
            WHERE guild_id = $1
        """, guild_id, ladder_name)

    async def latest_refresh_for_guild(self, guild_id: int):
        pool = self._pool()
        return await pool.fetchval("""
            SELECT MAX(ra.last_riot_refresh)
            FROM ladder_players lp
            JOIN riot_accounts ra
              ON ra.puuid = lp.puuid
             AND ra.platform = lp.platform
            WHERE lp.guild_id = $1
              AND lp.is_registered = TRUE
        """, guild_id)

    async def set_leaderboard_channel(self, guild_id: int, channel_id: int):
        pool = self._pool()
        await pool.execute("""
            UPDATE guilds
            SET leaderboard_channel_id = $2,
                updated_at = NOW()
            WHERE guild_id = $1
        """, guild_id, channel_id)

    async def set_sheet(
        self,
        guild_id: int,
        sheet_id: str,
        worksheet_name: str,
    ):
        pool = self._pool()
        await pool.execute("""
            UPDATE guilds
            SET google_sheet_id = $2,
                google_worksheet_name = $3,
                sheets_enabled = TRUE,
                updated_at = NOW()
            WHERE guild_id = $1
        """, guild_id, sheet_id, worksheet_name)

    async def disable_sheet(self, guild_id: int):
        pool = self._pool()
        await pool.execute("""
            UPDATE guilds
            SET sheets_enabled = FALSE,
                updated_at = NOW()
            WHERE guild_id = $1
        """, guild_id)

    async def get_guild_config(self, guild_id: int):
        pool = self._pool()
        return await pool.fetchrow(
            "SELECT * FROM guilds WHERE guild_id = $1",
            guild_id,
        )

    async def all_guild_configs(self):
        pool = self._pool()
        return await pool.fetch(
            "SELECT * FROM guilds ORDER BY guild_id"
        )

    async def upsert_riot_account(
        self,
        puuid: str,
        platform: str,
        game_name: str,
        tag_line: str,
        entry: RankedEntry,
    ):
        pool = self._pool()
        await pool.execute("""
            INSERT INTO riot_accounts (
                puuid,
                platform,
                game_name,
                tag_line,
                current_lp,
                current_label,
                tier,
                rank,
                league_points,
                wins,
                losses,
                last_riot_refresh,
                last_identity_refresh,
                updated_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW(), NOW(), NOW()
            )
            ON CONFLICT (puuid, platform)
            DO UPDATE SET
                game_name = EXCLUDED.game_name,
                tag_line = EXCLUDED.tag_line,
                current_lp = EXCLUDED.current_lp,
                current_label = EXCLUDED.current_label,
                tier = EXCLUDED.tier,
                rank = EXCLUDED.rank,
                league_points = EXCLUDED.league_points,
                wins = EXCLUDED.wins,
                losses = EXCLUDED.losses,
                last_riot_refresh = NOW(),
                last_identity_refresh = NOW(),
                updated_at = NOW()
        """,
            puuid,
            platform,
            game_name,
            tag_line,
            entry.absolute_lp,
            entry.label,
            entry.tier,
            entry.rank,
            entry.lp,
            entry.wins,
            entry.losses,
        )

    async def update_riot_account_rank(
        self,
        puuid: str,
        platform: str,
        entry: RankedEntry,
        game_name: Optional[str] = None,
        tag_line: Optional[str] = None,
    ):
        pool = self._pool()

        await pool.execute("""
            UPDATE riot_accounts
            SET current_lp = $3,
                current_label = $4,
                tier = $5,
                rank = $6,
                league_points = $7,
                wins = $8,
                losses = $9,
                game_name = COALESCE($10, game_name),
                tag_line = COALESCE($11, tag_line),
                last_riot_refresh = NOW(),
                updated_at = NOW()
            WHERE puuid = $1
              AND platform = $2
        """,
            puuid,
            platform,
            entry.absolute_lp,
            entry.label,
            entry.tier,
            entry.rank,
            entry.lp,
            entry.wins,
            entry.losses,
            game_name,
            tag_line,
        )

    async def update_riot_account_identity(
        self,
        puuid: str,
        platform: str,
        game_name: str,
        tag_line: str,
    ):
        pool = self._pool()
        await pool.execute("""
            UPDATE riot_accounts
            SET game_name = $3,
                tag_line = $4,
                last_identity_refresh = NOW(),
                updated_at = NOW()
            WHERE puuid = $1
              AND platform = $2
        """, puuid, platform, game_name, tag_line)

    async def mark_riot_account_refreshed(
        self,
        puuid: str,
        platform: str,
    ):
        pool = self._pool()
        await pool.execute("""
            UPDATE riot_accounts
            SET last_riot_refresh = NOW(),
                updated_at = NOW()
            WHERE puuid = $1
              AND platform = $2
        """, puuid, platform)

    async def register_player(
        self,
        guild_id: int,
        discord_user_id: int,
        puuid: str,
        platform: str,
        display_name: str,
        alltime_peak_lp: int,
        alltime_peak_label: str,
        current_entry: RankedEntry,
    ):
        pool = self._pool()

        challenger_entry_lp = (
            alltime_peak_lp
            if alltime_peak_label.lower().startswith("challenger")
            else (
                current_entry.absolute_lp
                if current_entry.tier == "CHALLENGER"
                else 0
            )
        )

        async with pool.acquire() as conn:
            async with conn.transaction():
                duplicate = await conn.fetchrow("""
                    SELECT discord_user_id
                    FROM ladder_players
                    WHERE guild_id = $1
                      AND puuid = $2
                      AND platform = $3
                      AND discord_user_id <> $4
                      AND is_registered = TRUE
                """, guild_id, puuid, platform, discord_user_id)

                if duplicate:
                    raise ValueError(
                        "Dieser Riot Account ist auf diesem Discord-Server "
                        "bereits registriert."
                    )

                existing = await conn.fetchrow("""
                    SELECT puuid, platform
                    FROM ladder_players
                    WHERE guild_id = $1
                      AND discord_user_id = $2
                """, guild_id, discord_user_id)

                if (
                    existing
                    and existing["puuid"] == puuid
                    and existing["platform"] == platform
                ):
                    # Gleicher Riot Account: Ladder-Fortschritt beibehalten.
                    await conn.execute("""
                        UPDATE ladder_players
                        SET display_name = $3,
                            is_registered = TRUE,
                            updated_at = NOW()
                        WHERE guild_id = $1
                          AND discord_user_id = $2
                    """, guild_id, discord_user_id, display_name)
                    return

                if existing:
                    # Anderer Riot Account: alte Ladder-Werte dürfen nicht
                    # auf den neuen Account übertragen werden.
                    await conn.execute("""
                        UPDATE ladder_players
                        SET puuid = $3,
                            platform = $4,
                            display_name = $5,
                            alltime_peak_lp = $6,
                            alltime_peak_label = $7,
                            ladder_start_lp = $6,
                            ladder_start_label = $7,
                            new_peak_lp = $8,
                            new_peak_label = $9,
                            challenger_entry_lp = $10,
                            is_registered = TRUE,
                            updated_at = NOW()
                        WHERE guild_id = $1
                          AND discord_user_id = $2
                    """,
                        guild_id,
                        discord_user_id,
                        puuid,
                        platform,
                        display_name,
                        alltime_peak_lp,
                        alltime_peak_label,
                        current_entry.absolute_lp,
                        current_entry.label,
                        challenger_entry_lp,
                    )
                    return

                await conn.execute("""
                    INSERT INTO ladder_players (
                        guild_id,
                        discord_user_id,
                        puuid,
                        platform,
                        display_name,
                        alltime_peak_lp,
                        alltime_peak_label,
                        ladder_start_lp,
                        ladder_start_label,
                        new_peak_lp,
                        new_peak_label,
                        challenger_entry_lp,
                        is_registered,
                        updated_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $6, $7, $8, $9, $10, TRUE, NOW()
                    )
                """,
                    guild_id,
                    discord_user_id,
                    puuid,
                    platform,
                    display_name,
                    alltime_peak_lp,
                    alltime_peak_label,
                    current_entry.absolute_lp,
                    current_entry.label,
                    challenger_entry_lp,
                )

    async def unregister_player(self, guild_id: int, discord_user_id: int):
        pool = self._pool()
        await pool.execute("""
            UPDATE ladder_players
            SET is_registered = FALSE,
                updated_at = NOW()
            WHERE guild_id = $1
              AND discord_user_id = $2
        """, guild_id, discord_user_id)

    async def active_unique_accounts(
        self,
        guild_id: Optional[int] = None,
    ):
        pool = self._pool()

        if guild_id is None:
            return await pool.fetch("""
                SELECT DISTINCT
                    ra.puuid,
                    ra.platform,
                    ra.game_name,
                    ra.tag_line,
                    ra.current_lp,
                    ra.current_label,
                    ra.last_identity_refresh
                FROM riot_accounts ra
                JOIN ladder_players lp
                  ON lp.puuid = ra.puuid
                 AND lp.platform = ra.platform
                WHERE lp.is_registered = TRUE
                ORDER BY ra.platform, ra.puuid
            """)

        return await pool.fetch("""
            SELECT DISTINCT
                ra.puuid,
                ra.platform,
                ra.game_name,
                ra.tag_line,
                ra.current_lp,
                ra.current_label,
                ra.last_identity_refresh
            FROM riot_accounts ra
            JOIN ladder_players lp
              ON lp.puuid = ra.puuid
             AND lp.platform = ra.platform
            WHERE lp.is_registered = TRUE
              AND lp.guild_id = $1
            ORDER BY ra.platform, ra.puuid
        """, guild_id)

    async def apply_account_rank_to_ladders(
        self,
        puuid: str,
        platform: str,
        entry: RankedEntry,
    ):
        pool = self._pool()

        rows = await pool.fetch("""
            SELECT
                guild_id,
                ladder_id,
                discord_user_id,
                new_peak_lp,
                new_peak_label,
                challenger_entry_lp
            FROM ladder_players
            WHERE puuid = $1
              AND platform = $2
              AND is_registered = TRUE
        """, puuid, platform)

        if not rows:
            return

        async with pool.acquire() as conn:
            async with conn.transaction():
                for row in rows:
                    new_peak_lp = int(row["new_peak_lp"] or 0)
                    new_peak_label = row["new_peak_label"] or entry.label
                    challenger_entry_lp = int(
                        row["challenger_entry_lp"] or 0
                    )

                    if entry.absolute_lp > new_peak_lp:
                        new_peak_lp = entry.absolute_lp
                        new_peak_label = entry.label

                    if (
                        challenger_entry_lp == 0
                        and entry.tier == "CHALLENGER"
                    ):
                        challenger_entry_lp = entry.absolute_lp

                    await conn.execute("""
                        UPDATE ladder_players
                        SET new_peak_lp = $4,
                            new_peak_label = $5,
                            challenger_entry_lp = $6,
                            updated_at = NOW()
                        WHERE guild_id = $1
                          AND ladder_id = $2
                          AND discord_user_id = $3
                    """,
                        row["guild_id"],
                        row["ladder_id"],
                        row["discord_user_id"],
                        new_peak_lp,
                        new_peak_label,
                        challenger_entry_lp,
                    )

    async def players_for_guild(self, guild_id: int):
        pool = self._pool()

        rows = await pool.fetch("""
            SELECT
                lp.*,
                ra.game_name,
                ra.tag_line,
                ra.current_lp,
                ra.current_label,
                ra.tier,
                ra.rank,
                ra.league_points,
                ra.wins,
                ra.losses,
                ra.last_riot_refresh
            FROM ladder_players lp
            JOIN riot_accounts ra
              ON ra.puuid = lp.puuid
             AND ra.platform = lp.platform
            WHERE lp.guild_id = $1
              AND lp.is_registered = TRUE
        """, guild_id)

        return sorted(
            rows,
            key=lambda row: (
                weighted_delta(
                    int(row["ladder_start_lp"]),
                    int(row["new_peak_lp"]),
                    row["ladder_start_label"],
                    row["new_peak_label"],
                    int(row["challenger_entry_lp"] or 0),
                ),
                int(row["new_peak_lp"]),
                row["display_name"].lower(),
            ),
            reverse=True,
        )

    async def guild_ids_with_players(self):
        pool = self._pool()
        rows = await pool.fetch("""
            SELECT DISTINCT guild_id
            FROM ladder_players
            WHERE is_registered = TRUE
            ORDER BY guild_id
        """)
        return [int(row["guild_id"]) for row in rows]

    async def reset_ladder(self, guild_id: int):
        pool = self._pool()
        await pool.execute("""
            UPDATE ladder_players
            SET
                alltime_peak_lp = GREATEST(alltime_peak_lp, new_peak_lp),
                alltime_peak_label = CASE
                    WHEN new_peak_lp > alltime_peak_lp
                    THEN new_peak_label
                    ELSE alltime_peak_label
                END,
                ladder_start_lp = GREATEST(alltime_peak_lp, new_peak_lp),
                ladder_start_label = CASE
                    WHEN new_peak_lp > alltime_peak_lp
                    THEN new_peak_label
                    ELSE alltime_peak_label
                END,
                new_peak_lp = CASE
                    WHEN (
                        SELECT current_lp
                        FROM riot_accounts ra
                        WHERE ra.puuid = ladder_players.puuid
                          AND ra.platform = ladder_players.platform
                    ) < GREATEST(alltime_peak_lp, new_peak_lp)
                    THEN (
                        SELECT current_lp
                        FROM riot_accounts ra
                        WHERE ra.puuid = ladder_players.puuid
                          AND ra.platform = ladder_players.platform
                    )
                    ELSE GREATEST(alltime_peak_lp, new_peak_lp)
                END,
                new_peak_label = CASE
                    WHEN (
                        SELECT current_lp
                        FROM riot_accounts ra
                        WHERE ra.puuid = ladder_players.puuid
                          AND ra.platform = ladder_players.platform
                    ) < GREATEST(alltime_peak_lp, new_peak_lp)
                    THEN (
                        SELECT current_label
                        FROM riot_accounts ra
                        WHERE ra.puuid = ladder_players.puuid
                          AND ra.platform = ladder_players.platform
                    )
                    ELSE CASE
                        WHEN new_peak_lp > alltime_peak_lp
                        THEN new_peak_label
                        ELSE alltime_peak_label
                    END
                END,
                challenger_entry_lp = CASE
                    WHEN (
                        CASE
                            WHEN new_peak_lp > alltime_peak_lp
                            THEN new_peak_label
                            ELSE alltime_peak_label
                        END
                    ) ILIKE 'Challenger %'
                    THEN GREATEST(alltime_peak_lp, new_peak_lp)

                    WHEN (
                        SELECT current_label
                        FROM riot_accounts ra
                        WHERE ra.puuid = ladder_players.puuid
                          AND ra.platform = ladder_players.platform
                    ) ILIKE 'Challenger %'
                    AND (
                        SELECT current_lp
                        FROM riot_accounts ra
                        WHERE ra.puuid = ladder_players.puuid
                          AND ra.platform = ladder_players.platform
                    ) < GREATEST(alltime_peak_lp, new_peak_lp)
                    THEN (
                        SELECT current_lp
                        FROM riot_accounts ra
                        WHERE ra.puuid = ladder_players.puuid
                          AND ra.platform = ladder_players.platform
                    )

                    ELSE 0
                END,
                updated_at = NOW()
            WHERE guild_id = $1
              AND is_registered = TRUE
        """, guild_id)

    async def restore_alltime_peak_from_ladder_start(self, guild_id: int):
        pool = self._pool()
        await pool.execute("""
            UPDATE ladder_players
            SET alltime_peak_lp = ladder_start_lp,
                alltime_peak_label = ladder_start_label,
                updated_at = NOW()
            WHERE guild_id = $1
        """, guild_id)

    async def get_leaderboard_message_ids(self, guild_id: int):
        pool = self._pool()
        rows = await pool.fetch("""
            SELECT page_index, message_id
            FROM leaderboard_messages
            WHERE guild_id = $1
            ORDER BY page_index
        """, guild_id)
        return [int(row["message_id"]) for row in rows]

    async def replace_leaderboard_message_ids(
        self,
        guild_id: int,
        message_ids: list[int],
    ):
        pool = self._pool()

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM leaderboard_messages WHERE guild_id = $1",
                    guild_id,
                )

                if message_ids:
                    await conn.executemany("""
                        INSERT INTO leaderboard_messages (
                            guild_id,
                            page_index,
                            message_id
                        )
                        VALUES ($1, $2, $3)
                    """, [
                        (guild_id, index, message_id)
                        for index, message_id in enumerate(message_ids)
                    ])


def _progress_text(row, ladder_type: str, rules: dict) -> str:
    if ladder_type == "normal":
        return row["current_label"] or "Unranked"

    mode = (rules.get("scoring") or {}).get("mode", "peak_gain")
    score = calculate_challenge_score(row, rules)

    if mode in {"peak_gain", "current_gain", "absolute_rank"}:
        return f"{score:+g} LP"
    if mode == "wins":
        return f"{int(row['wins'] or 0)} W"
    if mode == "winrate":
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        games = wins + losses
        return f"{(wins / games * 100 if games else 0):.1f}%"
    if mode == "games":
        return f"{int(row['wins'] or 0) + int(row['losses'] or 0)} Games"

    return score_label(score)


def _leaderboard_region(value) -> str:
    region = str(value or "").upper()
    return {
        "EUW1": "EUW",
        "EUN1": "EUNE",
        "NA1": "NA",
        "BR1": "BR",
        "JP1": "JP",
        "OC1": "OCE",
        "TR1": "TR",
        "LA1": "LAN",
        "LA2": "LAS",
    }.get(region, region)


def _challenge_rule_summary(rules: dict, lang: str) -> str:
    scoring = rules.get("scoring") or {}
    features = rules.get("features") or {}
    extras = features.get("scoring_extras") or {}
    competition = features.get("competition") or {}
    join = rules.get("join") or {}

    parts = [pretty_scoring_mode(scoring.get("mode", "peak_gain"))]

    minimum_games = int(extras.get("minimum_games_for_score", 0) or 0)
    if minimum_games:
        parts.append(
            f"Min. {minimum_games} Games"
            if lang == "de"
            else f"Min. {minimum_games} games"
        )

    if join.get("allow_after_start", True):
        parts.append(
            "Join nach Start"
            if lang == "de"
            else "Join after start"
        )

    if not competition.get("registration_open", True):
        parts.append(
            "Anmeldung geschlossen"
            if lang == "de"
            else "Registration closed"
        )

    floor = scoring.get("floor")
    cap = scoring.get("cap")
    if floor is not None:
        parts.append(f"Floor {floor:g}" if isinstance(floor, (int, float)) else f"Floor {floor}")
    if cap is not None:
        parts.append(f"Cap {cap:g}" if isinstance(cap, (int, float)) else f"Cap {cap}")

    return " · ".join(parts)


def _row_peak_gain(position: int, row, rules: dict, lang: str) -> str:
    rank = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"`{position}.`")
    return (
        f"{rank} **{row['display_name']}**"
        f" | `{_progress_text(row, 'challenge', rules)}`"
        f" | Region: `{_leaderboard_region(row['platform'])}`"
        f" | Peak: `{row['new_peak_label'] or '—'}`"
        f" | All Time Peak: `{row['alltime_peak_label'] or '—'}`"
        f" | Current: `{row['current_label'] or 'Unranked'}`"
    )


def _row_current_gain(position: int, row, rules: dict, lang: str) -> str:
    rank = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"`{position}.`")
    return (
        f"{rank} **{row['display_name']}**"
        f" | `{_progress_text(row, 'challenge', rules)}`"
        f" | Region: `{_leaderboard_region(row['platform'])}`"
        f" | Peak: `{row['new_peak_label'] or '—'}`"
        f" | All Time Peak: `{row['alltime_peak_label'] or '—'}`"
        f" | Current: `{row['current_label'] or 'Unranked'}`"
    )


def _row_wins(position: int, row, rules: dict, lang: str) -> str:
    rank = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"`{position}.`")
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    games = wins + losses
    return (
        f"{rank} **{row['display_name']}**"
        f" | Wins: `{wins}`"
        f" | W-L: `{wins}-{losses}`"
        f" | Games: `{games}`"
        f" | Region: `{_leaderboard_region(row['platform'])}`"
        f" | {'All Time Peak' if lang == 'en' else 'All Time Peak'}: `{row['alltime_peak_label'] or '—'}`"
        f" | Current: `{row['current_label'] or 'Unranked'}`"
    )


def _row_winrate(position: int, row, rules: dict, lang: str) -> str:
    rank = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"`{position}.`")
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    games = wins + losses
    wr = wins / games * 100 if games else 0.0
    return (
        f"{rank} **{row['display_name']}**"
        f" | Winrate: `{wr:.1f}%`"
        f" | W-L: `{wins}-{losses}`"
        f" | Games: `{games}`"
        f" | Region: `{_leaderboard_region(row['platform'])}`"
        f" | {'All Time Peak' if lang == 'en' else 'All Time Peak'}: `{row['alltime_peak_label'] or '—'}`"
        f" | Current: `{row['current_label'] or 'Unranked'}`"
    )


def _row_normal(position: int, row, lang: str) -> str:
    rank = {1: "🥇", 2: "🥈", 3: "🥉"}.get(position, f"`{position}.`")
    return (
        f"{rank} **{row['display_name']}**"
        f" | Region: `{_leaderboard_region(row['platform'])}`"
        f" | Current: `{row['current_label'] or 'Unranked'}`"
        f" | {'All Time Peak' if lang == 'en' else 'All Time Peak'}: `{row['alltime_peak_label'] or row['new_peak_label'] or '—'}`"
    )


def build_leaderboard_embeds(
    rows,
    ladder_name: str = "LadderGG",
    last_update: Optional[datetime] = None,
    ladder_type: str = "challenge",
    rules: Optional[dict] = None,
    lang: str = "de",
) -> list[discord.Embed]:
    rules = rules or {}

    if last_update is None:
        update_text = t(lang, "not_updated")
    else:
        if last_update.tzinfo is None:
            last_update = last_update.replace(tzinfo=timezone.utc)
        update_text = f"<t:{int(last_update.timestamp())}:R>"

    if ladder_type == "normal":
        ordered = sorted(
            rows,
            key=lambda r: (
                int(r["current_lp"] or 0),
                int(r["wins"] or 0),
            ),
            reverse=True,
        )
        icon = "🏆"
        subtitle = "Solo/Duo"
        mode = "normal"
    else:
        ordered = sorted(
            rows,
            key=lambda r: (
                calculate_challenge_score(r, rules),
                int(r["new_peak_lp"] or 0),
                int(r["current_lp"] or 0),
            ),
            reverse=True,
        )
        icon = "🔥"
        subtitle = _challenge_rule_summary(rules, lang)
        mode = str((rules.get("scoring") or {}).get("mode", "peak_gain"))

    player_word = (
        ("Player" if len(ordered) == 1 else "Players")
        if lang == "en"
        else ("Spieler" if len(ordered) != 1 else "Spieler")
    )

    if not ordered:
        return [
            discord.Embed(
                title=f"{icon} {ladder_name}",
                description=(
                    f"**{subtitle}** · **0 {player_word}**\n\n"
                    f"{t(lang, 'use_join')}"
                ),
            )
        ]

    # Same idea as the old Peakbot: one player = one Discord line.
    page_size = 10
    pages = []

    for page_index, start_index in enumerate(
        range(0, len(ordered), page_size),
        start=1,
    ):
        chunk = ordered[start_index:start_index + page_size]
        lines = []

        for offset, row in enumerate(chunk):
            position = start_index + offset + 1

            if ladder_type == "normal":
                line = _row_normal(position, row, lang)
            elif mode in {"peak_gain", "absolute_rank"}:
                line = _row_peak_gain(position, row, rules, lang)
            elif mode == "current_gain":
                line = _row_current_gain(position, row, rules, lang)
            elif mode == "wins":
                line = _row_wins(position, row, rules, lang)
            elif mode == "winrate":
                line = _row_winrate(position, row, rules, lang)
            else:
                line = _row_current_gain(position, row, rules, lang)

            lines.append(line)

        page_count = (len(ordered) + page_size - 1) // page_size
        page_suffix = (
            f" · {page_index}/{page_count}"
            if page_count > 1
            else ""
        )

        description = (
            f"**{subtitle}** · **{len(ordered)} {player_word}**\n\n"
            + "\n".join(lines)
            + f"\n\n*{t(lang, 'updated')} {update_text}*"
        )

        pages.append(
            discord.Embed(
                title=f"{icon} {ladder_name}{page_suffix}",
                description=description,
            )
        )

    return pages


def build_sheet_values(rows) -> list[list[object]]:
    values: list[list[object]] = [[
        "Rank",
        "Name",
        "Region",
        "Weighted Delta",
        "New Peak",
        "Ladder Start",
        "All Time Peak",
        "Current",
        "Riot Name",
        "Tag",
        "Updated At",
    ]]

    for idx, row in enumerate(rows, start=1):
        delta = weighted_delta(
            int(row["ladder_start_lp"]),
            int(row["new_peak_lp"]),
            row["ladder_start_label"],
            row["new_peak_label"],
            int(row["challenger_entry_lp"] or 0),
        )

        updated_at = row["last_riot_refresh"]
        if isinstance(updated_at, datetime):
            updated_at = updated_at.astimezone(timezone.utc).isoformat()

        values.append([
            idx,
            row["display_name"],
            row["platform"].upper(),
            delta,
            row["new_peak_label"],
            row["ladder_start_label"],
            row["alltime_peak_label"],
            row["current_label"] or "",
            row["game_name"],
            row["tag_line"],
            updated_at or "",
        ])

    return values


def _sheet_datetime(value) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    return str(value)


def _sheet_rule_summary(ladder, lang: str = "de") -> list[list[object]]:
    rules = parse_rules_jsonb(ladder["rules"])
    scoring = rules.get("scoring") or {}
    start = rules.get("start") or {}
    join = rules.get("join") or {}
    features = rules.get("features") or {}
    goal = features.get("goal") or {}

    multipliers = []
    for rule in rules.get("multipliers") or []:
        multiplier = float(rule.get("multiplier", 1.0))
        if multiplier == 1.0 and not rule.get("only_after_entry"):
            continue
        suffix = (
            " (after entry)" if lang == "en" else " (ab Eintritt)"
        ) if rule.get("only_after_entry") else ""
        multipliers.append(
            f"{str(rule.get('tier', '?')).title()} {multiplier:g}x{suffix}"
        )

    rows = [
        [t(lang, "scoring"), pretty_scoring_mode(scoring.get("mode", "peak_gain"))],
        [t(lang, "sheet_start_value"), str(start.get("mode", "all_time_peak")).replace("_", " ").title()],
        [t(lang, "sheet_negative"), t(lang, "yes") if scoring.get("allow_negative", True) else t(lang, "no")],
        [t(lang, "sheet_late_join"), t(lang, "yes") if join.get("allow_after_start", True) else t(lang, "no")],
        [t(lang, "sheet_min"), scoring.get("floor") if scoring.get("floor") is not None else t(lang, "none")],
        [t(lang, "sheet_max"), scoring.get("cap") if scoring.get("cap") is not None else t(lang, "none")],
        [t(lang, "sheet_multipliers"), " · ".join(multipliers) if multipliers else t(lang, "none")],
    ]
    if goal.get("type") and goal.get("type") != "none":
        rows.append([
            t(lang, "goal"),
            f"{goal.get('type')} · {goal.get('label') or goal.get('target')}",
        ])
    return rows


def _sheet_short_datetime(value) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%d.%m.%Y")
    return str(value)


def _sheet_region(value) -> str:
    region = str(value or "").upper()
    aliases = {
        "EUW1": "EUW",
        "EUN1": "EUNE",
        "NA1": "NA",
        "KR": "KR",
        "BR1": "BR",
        "JP1": "JP",
        "OC1": "OCE",
        "TR1": "TR",
        "RU": "RU",
        "LA1": "LAN",
        "LA2": "LAS",
    }
    return aliases.get(region, region)


def _sheet_yes_no(value: bool, lang: str) -> str:
    if lang == "en":
        return "Yes" if value else "No"
    return "Ja" if value else "Nein"


def _sheet_participation_label(rules: dict, lang: str) -> str:
    features = rules.get("features") or {}
    competition = features.get("competition") or {}
    mode = str(competition.get("registration_mode", "public")).lower()
    labels = {
        "de": {"public": "Öffentlich", "role": "Discord-Rolle", "invite": "Nur Einladung"},
        "en": {"public": "Public", "role": "Discord role", "invite": "Invite only"},
    }
    return labels.get(lang, labels["de"]).get(mode, mode.title())


def build_ladder_sheet_header(rows, ladder, lang: str = "de") -> list[list[object]]:
    rules = parse_rules_jsonb(ladder["rules"])
    is_challenge = ladder["ladder_type"] == "challenge"
    scoring = rules.get("scoring") or {}
    features = rules.get("features") or {}
    extras = features.get("scoring_extras") or {}
    competition = features.get("competition") or {}
    join = rules.get("join") or {}

    participant_count = len(rows)
    total_games = sum(
        int(r["wins"] or 0) + int(r["losses"] or 0)
        for r in rows
    )

    if rows:
        if is_challenge:
            leader = max(rows, key=lambda r: calculate_challenge_score(r, rules))
            leader_value = (
                f"{leader['display_name']} · "
                f"{score_label(calculate_challenge_score(leader, rules))}"
            )
        else:
            leader = max(rows, key=lambda r: int(r["current_lp"] or 0))
            leader_value = (
                f"{leader['display_name']} · "
                f"{leader['current_label'] or 'Unranked'}"
            )
    else:
        leader_value = "—"

    scoring_label = (
        pretty_scoring_mode(scoring.get("mode", "peak_gain"))
        if is_challenge else "Solo/Duo"
    )

    status_raw = str(ladder["status"] or "active").lower()
    status_map = {
        "de": {
            "active": "Aktiv", "scheduled": "Geplant",
            "finished": "Beendet", "archived": "Archiviert",
        },
        "en": {
            "active": "Active", "scheduled": "Scheduled",
            "finished": "Finished", "archived": "Archived",
        },
    }
    status = status_map.get(lang, status_map["de"]).get(
        status_raw, status_raw.title()
    )

    start_text = _sheet_short_datetime(ladder["starts_at"])
    end_text = _sheet_short_datetime(ladder["ends_at"])
    period = f"{start_text} → {end_text}"

    minimum_games = int(extras.get("minimum_games_for_score", 0) or 0)
    registration_open = bool(competition.get("registration_open", True))
    late_join = bool(join.get("allow_after_start", True))

    # Four compact header rows. Each item uses a two-column block:
    # label | value. This prevents the ugly wrapped labels from the old sheet.
    if lang == "en":
        return [
            [f"{'🔥' if is_challenge else '🏆'} {ladder['name']}", "", "", "", "", "", "", "", "", ""],
            ["Type", "Challenge" if is_challenge else "Normal", "Status", status, "Players", participant_count, "Games", total_games, "Ladder", f"#{int(ladder['ladder_id'])}"],
            ["Period", period, "Scoring", scoring_label, "Leader", leader_value, "Participation", _sheet_participation_label(rules, lang), "Registration", "Open" if registration_open else "Closed"],
            ["Min. games", minimum_games, "Join after start", _sheet_yes_no(late_join, lang), "Negative score", _sheet_yes_no(scoring.get("allow_negative", True), lang), "Floor", scoring.get("floor") if scoring.get("floor") is not None else "—", "Cap", scoring.get("cap") if scoring.get("cap") is not None else "—"],
            ["", "", "", "", "", "", "", "", "", ""],
        ]

    return [
        [f"{'🔥' if is_challenge else '🏆'} {ladder['name']}", "", "", "", "", "", "", "", "", ""],
        ["Typ", "Challenge" if is_challenge else "Normal", "Status", status, "Spieler", participant_count, "Games", total_games, "Ladder", f"#{int(ladder['ladder_id'])}"],
        ["Zeitraum", period, "Wertung", scoring_label, "Führung", leader_value, "Teilnahme", _sheet_participation_label(rules, lang), "Anmeldung", "Offen" if registration_open else "Geschlossen"],
        ["Mindestspiele", minimum_games, "Beitritt nach Start", _sheet_yes_no(late_join, lang), "Negative Scores", _sheet_yes_no(scoring.get("allow_negative", True), lang), "Floor", scoring.get("floor") if scoring.get("floor") is not None else "—", "Cap", scoring.get("cap") if scoring.get("cap") is not None else "—"],
        ["", "", "", "", "", "", "", "", "", ""],
    ]


def build_ladder_sheet_table(rows, ladder, lang: str = "de") -> list[list[object]]:
    rules = parse_rules_jsonb(ladder["rules"])
    is_challenge = ladder["ladder_type"] == "challenge"

    if is_challenge:
        ordered = sorted(
            rows,
            key=lambda r: (
                calculate_challenge_score(r, rules),
                int(r["new_peak_lp"] or 0),
                int(r["current_lp"] or 0),
            ),
            reverse=True,
        )
    else:
        ordered = sorted(
            rows,
            key=lambda r: (
                int(r["current_lp"] or 0),
                int(r["wins"] or 0),
            ),
            reverse=True,
        )

    # Peak-bot style: stable, compact columns. No redundant All-Time-Peak
    # and no technical timestamp in the main ranking.
    if is_challenge:
        headers = [
            "Rank",
            "Name",
            "Region",
            "Score",
            "Challenge Peak" if lang == "en" else "Challenge Peak",
            "Ladder Start",
            "Current",
            "Riot ID",
            "Games",
            "W-L",
            "Winrate",
        ]
    else:
        headers = [
            "Rank",
            "Name",
            "Region",
            "Current",
            "Peak",
            "Riot ID",
            "Games",
            "W-L",
            "Winrate",
        ]

    values = [headers]

    for index, row in enumerate(ordered, start=1):
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        games = wins + losses
        winrate = wins / games if games else 0.0
        riot_id = f"{row['game_name']}#{row['tag_line']}"

        if is_challenge:
            score = calculate_challenge_score(row, rules)
            values.append([
                index,
                row["display_name"],
                _sheet_region(row["platform"]),
                score,
                row["new_peak_label"] or "—",
                row["ladder_start_label"] or "—",
                row["current_label"] or "Unranked",
                riot_id,
                games,
                f"{wins}-{losses}",
                winrate,
            ])
        else:
            values.append([
                index,
                row["display_name"],
                _sheet_region(row["platform"]),
                row["current_label"] or "Unranked",
                row["new_peak_label"] or "—",
                riot_id,
                games,
                f"{wins}-{losses}",
                winrate,
            ])

    return values


def _column_letter(index: int) -> str:
    result = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def format_ladder_worksheet(
    spreadsheet,
    worksheet,
    table_start_row: int,
    row_count: int,
    column_count: int = 11,
):
    """Peak-bot style sheet: compact info header + plain ranking table."""
    sheet_id = worksheet.id
    end_row = table_start_row + max(row_count, 1)

    requests = [
        {
            "unmergeCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 5,
                    "startColumnIndex": 0,
                    "endColumnIndex": 50,
                }
            }
        },
        {
            "clearBasicFilter": {
                "sheetId": sheet_id,
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {
                        "frozenRowCount": table_start_row,
                    },
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "mergeCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "mergeType": "MERGE_ALL",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.10, "green": 0.12, "blue": 0.18},
                        "textFormat": {
                            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                            "fontSize": 16,
                            "bold": True,
                        },
                        "horizontalAlignment": "LEFT",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat",
            }
        },
        # Info area: labels bold, values normal. No giant dashboard blocks.
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": 4,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.96, "green": 0.97, "blue": 0.99},
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "CLIP",
                    }
                },
                "fields": "userEnteredFormat",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": 4,
                    "startColumnIndex": 0,
                    "endColumnIndex": min(column_count, 10),
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"foregroundColor": {"red": 0.12, "green": 0.14, "blue": 0.20}}
                    }
                },
                "fields": "userEnteredFormat.textFormat.foregroundColor",
            }
        },
        # Ranking header exactly like a classic leaderboard.
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": table_start_row - 1,
                    "endRowIndex": table_start_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.10, "green": 0.12, "blue": 0.18},
                        "textFormat": {
                            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                            "bold": True,
                        },
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": "userEnteredFormat",
            }
        },
        {
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": table_start_row - 1,
                        "endRowIndex": end_row,
                        "startColumnIndex": 0,
                        "endColumnIndex": column_count,
                    }
                }
            }
        },
    ]

    # Bold the compact header labels in A/C/E/G/I.
    for col in range(0, min(column_count, 10), 2):
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": 4,
                    "startColumnIndex": col,
                    "endColumnIndex": col + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True}
                    }
                },
                "fields": "userEnteredFormat.textFormat.bold",
            }
        })

    # Winrate is the final column in both layouts.
    if column_count >= 1:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": table_start_row,
                    "endRowIndex": end_row,
                    "startColumnIndex": column_count - 1,
                    "endColumnIndex": column_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "PERCENT", "pattern": "0.0%"}
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    # Peak-bot-like widths.
    challenge_widths = [60, 150, 75, 90, 145, 145, 145, 185, 70, 70, 85]
    normal_widths = [60, 150, 75, 145, 145, 185, 70, 70, 85]
    widths = (challenge_widths if column_count >= 11 else normal_widths)[:column_count]

    for col, width in enumerate(widths):
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": col,
                    "endIndex": col + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })

    for row_index, height in [
        (0, 36), (1, 25), (2, 25), (3, 25), (4, 10),
        (table_start_row - 1, 28),
    ]:
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": row_index,
                    "endIndex": row_index + 1,
                },
                "properties": {"pixelSize": height},
                "fields": "pixelSize",
            }
        })

    spreadsheet.batch_update({"requests": requests})


def write_polished_ladder_sheet(
    spreadsheet,
    worksheet,
    rows,
    ladder,
    archived: bool = False,
    lang: str = "de",
):
    header = build_ladder_sheet_header(rows, ladder, lang)
    table = build_ladder_sheet_table(rows, ladder, lang)

    values = list(header)
    table_start_row = len(values) + 1
    values.extend(table)

    worksheet.clear()
    worksheet.update(
        values=values,
        range_name="A1",
        value_input_option="USER_ENTERED",
    )

    format_ladder_worksheet(
        spreadsheet,
        worksheet,
        table_start_row,
        max(0, len(table) - 1),
        len(table[0]) if table else 1,
    )




class LadderGGBot(commands.Bot):
    async def setup_hook(self):
        await db.start()
        await riot.start()

        if TEST_GUILD_ID:
            guild = discord.Object(id=TEST_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info(
                "Slash Commands mit Test-Guild %s synchronisiert.",
                TEST_GUILD_ID,
            )
        else:
            await self.tree.sync()
            logger.info("Globale Slash Commands synchronisiert.")

        if not periodic_update.is_running():
            periodic_update.start()

    async def close(self):
        if periodic_update.is_running():
            periodic_update.cancel()

        await send_alert("⚠️ LadderGG wird heruntergefahren...")

        await riot.close()
        await db.close()
        await super().close()


async def send_alert(message: str):
    """
    Schickt eine kurze Statusnachricht an einen Discord-Webhook
    (ALERT_WEBHOOK_URL), z.B. bei Start/Stop des Bots. Fehler dabei
    dürfen den eigentlichen Start/Shutdown nie blockieren.
    """
    if not ALERT_WEBHOOK_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                ALERT_WEBHOOK_URL,
                json={"content": message},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception:
        logger.exception("Alert-Webhook konnte nicht erreicht werden.")


intents = discord.Intents.default()
bot = LadderGGBot(command_prefix="!", intents=intents)
riot = RiotClient(RIOT_API_KEY)
db = Database(DATABASE_URL)

_google_client: Optional[gspread.Client] = None
_google_client_lock = asyncio.Lock()
_google_sync_lock = asyncio.Lock()


async def get_google_client() -> gspread.Client:
    global _google_client

    async with _google_client_lock:
        if _google_client is not None:
            return _google_client

        if not os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
            raise FileNotFoundError(
                f"Google Service Account Datei nicht gefunden: "
                f"{GOOGLE_SERVICE_ACCOUNT_FILE}"
            )

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        def create_client():
            credentials = Credentials.from_service_account_file(
                GOOGLE_SERVICE_ACCOUNT_FILE,
                scopes=scopes,
            )
            return gspread.authorize(credentials)

        _google_client = await asyncio.to_thread(create_client)
        return _google_client


def normalize_google_sheet_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("Google Sheet ID/URL darf nicht leer sein.")

    # Full Google Sheets URL:
    # https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit...
    match = re.search(
        r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
        value,
    )
    if match:
        return match.group(1)

    # Raw Sheet ID.
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", value):
        return value

    raise ValueError(
        "Invalid Google Sheets ID or URL. Please use the full sheet URL "
        "or the ID between `/d/` and `/edit`. / "
        "Ungültige Google-Sheets-ID oder URL. "
        "Bitte die vollständige Sheet-URL oder die ID zwischen `/d/` und `/edit` verwenden."
    )


def google_service_account_email() -> Optional[str]:
    try:
        with open(
            GOOGLE_SERVICE_ACCOUNT_FILE,
            "r",
            encoding="utf-8",
        ) as fp:
            payload = json.load(fp)
        return payload.get("client_email")
    except Exception:
        return None


async def sync_google_sheet_for_guild(guild_id: int):
    async with _google_sync_lock:
        await _sync_google_sheet_for_guild_unlocked(guild_id)


async def _sync_google_sheet_for_guild_unlocked(guild_id: int):
    config = await db.get_guild_config(guild_id)
    lang = await db.get_guild_language(guild_id)

    if not config or not config["sheets_enabled"]:
        return

    raw_sheet_id = (config["google_sheet_id"] or "").strip()
    if not raw_sheet_id:
        return

    sheet_id = normalize_google_sheet_id(raw_sheet_id)

    ladders = await db.list_ladders(
        guild_id,
        include_archived=False,
    )
    client = await get_google_client()

    async def ladder_payload(ladder):
        rows = await db.players_for_ladder(
            guild_id,
            int(ladder["ladder_id"]),
            approved_only=False,
        )
        return ladder, rows

    payloads = [
        await ladder_payload(ladder)
        for ladder in ladders
        if ladder["status"] != "archived"
    ]

    def write_sheet():
        spreadsheet = client.open_by_key(sheet_id)

        # ---------- Overview ----------
        overview_name = "LadderGG"
        try:
            overview = spreadsheet.worksheet(overview_name)
        except gspread.WorksheetNotFound:
            overview = spreadsheet.add_worksheet(
                title=overview_name,
                rows=200,
                cols=10,
            )

        overview_values = [
            ["🏆 LadderGG", "", "", "", ""],
            [t(lang, "sheet_overview"), "", "", "", ""],
            [],
            [t(lang, "sheet_name"), t(lang, "sheet_type"), t(lang, "sheet_status"), t(lang, "sheet_participants"), t(lang, "sheet_channel")],
        ]

        for ladder, rows in payloads:
            overview_values.append([
                ladder["name"],
                ("Challenge" if ladder["ladder_type"] == "challenge" else "Normal"),
                str(ladder["status"]).title(),
                len(rows),
                f"#{ladder['leaderboard_channel_id']}" if ladder["leaderboard_channel_id"] else "—",
            ])

        overview.clear()
        overview.update(
            values=overview_values,
            range_name="A1",
            value_input_option="USER_ENTERED",
        )

        # Simple polished overview formatting
        spreadsheet.batch_update({
            "requests": [
                {
                    "mergeCells": {
                        "range": {
                            "sheetId": overview.id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 5,
                        },
                        "mergeType": "MERGE_ALL",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": overview.id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 5,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.12, "green": 0.14, "blue": 0.20},
                                "textFormat": {
                                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                    "fontSize": 18,
                                    "bold": True,
                                },
                            }
                        },
                        "fields": "userEnteredFormat",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": overview.id,
                            "startRowIndex": 3,
                            "endRowIndex": 4,
                            "startColumnIndex": 0,
                            "endColumnIndex": 5,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.12, "green": 0.14, "blue": 0.20},
                                "textFormat": {
                                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                    "bold": True,
                                },
                            }
                        },
                        "fields": "userEnteredFormat",
                    }
                },
                {
                    "setBasicFilter": {
                        "filter": {
                            "range": {
                                "sheetId": overview.id,
                                "startRowIndex": 3,
                                "endRowIndex": max(4, len(overview_values)),
                                "startColumnIndex": 0,
                                "endColumnIndex": 5,
                            }
                        }
                    }
                },
            ]
        })

        # ---------- One live tab per ladder ----------
        for ladder, rows in payloads:
            clean_name = re.sub(
                r"[^A-Za-z0-9 _-]",
                "",
                ladder["name"],
            ).strip()[:70]
            prefix = "🔥 " if ladder["ladder_type"] == "challenge" else "🏆 "
            tab_name = (prefix + clean_name)[:100]

            try:
                worksheet = spreadsheet.worksheet(tab_name)
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(
                    title=tab_name,
                    rows=max(150, len(rows) + 40),
                    cols=14,
                )

            write_polished_ladder_sheet(
                spreadsheet,
                worksheet,
                rows,
                ladder,
                archived=False,
                lang=lang,
            )

    await asyncio.to_thread(write_sheet)


async def sync_all_google_sheets():
    configs = await db.all_guild_configs()

    for config in configs:
        if not config["sheets_enabled"]:
            continue

        guild_id = int(config["guild_id"])

        try:
            await sync_google_sheet_for_guild(guild_id)
        except Exception:
            logger.exception(
                "Google Sheets Sync fehlgeschlagen für Guild %s",
                guild_id,
            )

        if SHEET_SYNC_DELAY_SECONDS:
            await asyncio.sleep(SHEET_SYNC_DELAY_SECONDS)


async def maybe_announce_rank_change(
    account,
    entry: RankedEntry,
):
    old_lp = account.get("current_lp")
    old_label = account.get("current_label")

    if old_lp is None or old_label is None:
        return
    if int(old_lp) == int(entry.absolute_lp):
        return

    old_tier = str(old_label).split(" ", 1)[0].upper()
    new_tier = str(entry.label).split(" ", 1)[0].upper()
    if old_tier == new_tier:
        return

    promoted = int(entry.absolute_lp) > int(old_lp)
    guild_rows = await _guilds_for_player(
        account["puuid"],
        account["platform"],
    )

    for guild_row in guild_rows:
        guild_id = int(guild_row["guild_id"])
        settings = await db.get_tracking_settings(guild_id)
        if not settings["promotion_alerts_enabled"]:
            continue
        channel_id = settings["match_alert_channel_id"]
        channel = bot.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            continue
        lang = await db.get_guild_language(guild_id)
        await channel.send(
            embed=discord.Embed(
                title=(
                    f"🚀 {t(lang, 'promotion')}"
                    if promoted
                    else f"📉 {t(lang, 'demotion')}"
                ),
                description=(
                    f"**{guild_row['display_name']}** · "
                    f"{old_label} → **{entry.label}**"
                ),
            )
        )


async def refresh_unique_riot_accounts(
    guild_id: Optional[int] = None,
) -> tuple[int, int]:
    """
    Jeder eindeutige Riot Account wird pro Zyklus genau einmal für Ranked
    abgefragt, unabhängig davon, auf wie vielen Discord-Servern er registriert
    ist. Riot-ID/Name wird höchstens einmal pro 24 Stunden zusätzlich geprüft.

    Rückgabe: (erfolgreich aktualisiert, Fehler)
    """
    accounts = await db.active_unique_accounts(guild_id)
    updated = 0
    failed = 0
    now = datetime.now(timezone.utc)

    for account in accounts:
        puuid = account["puuid"]
        platform = account["platform"]

        try:
            entry = await riot.get_soloq_entry_by_puuid(puuid, platform)

            if entry is None:
                await db.mark_riot_account_refreshed(puuid, platform)
                continue

            await maybe_announce_rank_change(account, entry)

            await db.update_riot_account_rank(
                puuid,
                platform,
                entry,
            )

            await db.apply_account_rank_to_ladders(
                puuid,
                platform,
                entry,
            )

            last_identity_refresh = account["last_identity_refresh"]
            identity_due = (
                last_identity_refresh is None
                or (now - last_identity_refresh).total_seconds() >= 86400
            )

            if identity_due:
                try:
                    account_data = await riot.get_account_by_puuid(
                        puuid,
                        platform,
                    )
                    game_name = account_data.get("gameName")
                    tag_line = account_data.get("tagLine")

                    if game_name and tag_line:
                        await db.update_riot_account_identity(
                            puuid,
                            platform,
                            game_name,
                            tag_line,
                        )
                except Exception:
                    logger.warning(
                        "Täglicher Riot-ID Refresh fehlgeschlagen für %s/%s",
                        puuid,
                        platform,
                    )

            updated += 1

        except Exception:
            failed += 1
            logger.exception(
                "Riot Refresh fehlgeschlagen für %s/%s",
                puuid,
                platform,
            )

    return updated, failed


def _leaderboard_embed_payload(embed: discord.Embed) -> dict:
    """Return only the meaningful leaderboard content for change detection.

    Timestamp/footer intentionally do not count. Riot refresh timestamps can
    change every cycle even when nobody's rank/score changed, which previously
    caused an unnecessary Discord PATCH every few minutes.
    """
    payload = embed.to_dict()
    payload.pop("timestamp", None)
    payload.pop("footer", None)
    return payload


def _message_has_same_leaderboard(
    message: discord.Message,
    embed: discord.Embed,
) -> bool:
    if not message.embeds:
        return False
    try:
        return (
            _leaderboard_embed_payload(message.embeds[0])
            == _leaderboard_embed_payload(embed)
        )
    except Exception:
        return False


async def post_or_edit_ladder_leaderboard(
    guild_id: int,
    ladder_id: int,
):
    ladder = await db.get_ladder(guild_id, ladder_id)
    lang = await db.get_guild_language(guild_id)
    if not ladder:
        return

    channel_id = ladder["leaderboard_channel_id"]
    if not channel_id:
        return

    channel = bot.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        return

    rows = await db.players_for_ladder(guild_id, ladder_id)
    last_update = await db.latest_refresh_for_guild(guild_id)
    embeds = build_leaderboard_embeds(
        rows,
        ladder["name"],
        last_update,
        ladder["ladder_type"],
        parse_rules_jsonb(ladder["rules"]),
        lang=lang,
    )

    old_ids = await db.get_ladder_message_ids(guild_id, ladder_id)
    new_ids = []

    for index, embed in enumerate(embeds):
        msg = None
        if index < len(old_ids):
            try:
                msg = await channel.fetch_message(old_ids[index])

                if not _message_has_same_leaderboard(msg, embed):
                    await msg.edit(content=None, embed=embed)
                    logger.debug(
                        "Leaderboard geändert: Guild %s Ladder %s Seite %s.",
                        guild_id,
                        ladder_id,
                        index + 1,
                    )
                else:
                    logger.debug(
                        "Leaderboard unverändert: Guild %s Ladder %s Seite %s – PATCH übersprungen.",
                        guild_id,
                        ladder_id,
                        index + 1,
                    )

            except discord.NotFound:
                msg = None
            except (
                discord.Forbidden,
                discord.HTTPException,
                ValueError,
            ):
                logger.exception(
                    "Leaderboard-Seite konnte nicht geprüft/editiert werden "
                    "(Guild %s Ladder %s Seite %s).",
                    guild_id,
                    ladder_id,
                    index + 1,
                )
                # Keep the existing message id instead of blindly creating a
                # duplicate after a temporary Discord error.
                if index < len(old_ids):
                    new_ids.append(old_ids[index])
                    continue
                msg = None

        if msg is None:
            msg = await channel.send(embed=embed)

        new_ids.append(msg.id)

    for old_id in old_ids[len(embeds):]:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.delete()
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            ValueError,
        ):
            pass

    await db.replace_ladder_message_ids(
        guild_id,
        ladder_id,
        new_ids,
    )


def _participant(match: dict, puuid: str) -> Optional[dict]:
    for p in ((match.get("info") or {}).get("participants") or []):
        if p.get("puuid")==puuid:
            return p
    return None

def _match_end(match: dict) -> Optional[datetime]:
    info=match.get("info") or {}
    raw=info.get("gameEndTimestamp") or info.get("gameCreation")
    return datetime.fromtimestamp(int(raw)/1000,tz=timezone.utc) if raw else None

def compact_match_history_embed(display_name, rows, lang="de"):
    rows = list(rows)
    n = len(rows)
    wins = sum(bool(r["win"]) for r in rows)
    losses = n - wins
    tk = sum(int(r["kills"] or 0) for r in rows)
    td = sum(int(r["deaths"] or 0) for r in rows)
    ta = sum(int(r["assists"] or 0) for r in rows)
    wr = wins / n * 100 if n else 0
    ratio = (tk + ta) / max(1, td)
    avg_cs = sum(int(r["cs"] or 0) for r in rows) / n if n else 0
    avg_dmg = sum(int(r["damage"] or 0) for r in rows) / n if n else 0
    avg_dur = sum(int(r["duration_seconds"] or 0) for r in rows) / n if n else 0

    first = bool(rows[0]["win"]) if rows else True
    streak = 0
    for r in rows:
        if bool(r["win"]) != first:
            break
        streak += 1

    lp = [int(r["lp_delta"]) for r in rows if r["lp_delta"] is not None]
    form = f"{streak} {t(lang, 'wins_in_row' if first else 'losses_in_row')}"

    embed = discord.Embed(
        title=f"🎮 {display_name} · {t(lang, 'recent_soloq')}"
    )
    summary = (
        f"**{wins}W · {losses}L** · `{wr:.1f}% WR`\n"
        f"**Ø KDA:** `{tk/n:.1f}/{td/n:.1f}/{ta/n:.1f}` · `{ratio:.2f}`\n"
        f"**Ø CS:** `{avg_cs:.0f}` · **Ø Damage:** `{avg_dmg:,.0f}`\n"
        f"**Ø Game:** `{int(avg_dur)//60}m {int(avg_dur)%60:02d}s`\n"
        f"**Form:** `{form}`"
    )
    if lp:
        summary += f"\n**LP:** `{sum(lp):+d}` ({len(lp)}/{n} games)"

    embed.add_field(
        name=f"📊 {t(lang, 'overview')}",
        value=summary,
        inline=False,
    )

    lines = []
    for i, r in enumerate(rows, 1):
        dur = int(r["duration_seconds"] or 0)
        lptext = (
            f" · `{int(r['lp_delta']):+d} LP`"
            if r["lp_delta"] is not None
            else ""
        )
        lines.append(
            f"{'🟢' if r['win'] else '🔴'} "
            f"**{i}. {r['champion_name'] or 'Unknown'}** · "
            f"**{int(r['kills'] or 0)}/{int(r['deaths'] or 0)}/{int(r['assists'] or 0)}** · "
            f"{int(r['cs'] or 0)} CS · {int(r['damage'] or 0):,} dmg · "
            f"`{dur//60}:{dur%60:02d}`{lptext}"
        )

    chunks, current = [], ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if len(candidate) > 1000:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    for idx, chunk in enumerate(chunks):
        embed.add_field(
            name="🧾 Matches" + (f" · {idx+1}" if idx else ""),
            value=chunk,
            inline=False,
        )

    embed.set_footer(
        text=f"LadderGG · {n} {t(lang, 'tracked_games')}"
    )
    return embed


def match_card_embed(display_name: str, row, lang: str = "de") -> discord.Embed:
    won = bool(row["win"])
    kills = int(row["kills"] or 0)
    deaths = int(row["deaths"] or 0)
    assists = int(row["assists"] or 0)
    kda = (kills + assists) / max(1, deaths)
    duration = int(row["duration_seconds"] or 0)
    minutes, seconds = divmod(duration, 60)
    champion = row["champion_name"] or "Unknown"

    embed = discord.Embed(
        title=(
            f"{'✅ VICTORY' if won else '❌ DEFEAT'} "
            f"as {champion}"
        ),
        description=(
            f"**{display_name}** · Ranked Solo/Duo · "
            f"`{minutes}m {seconds:02d}s`"
        ),
    )

    embed.add_field(
        name="KDA",
        value=f"**{kills}/{deaths}/{assists}**\n`{kda:.2f} KDA`",
        inline=True,
    )
    embed.add_field(
        name="CS",
        value=f"**{int(row['cs'] or 0)}**",
        inline=True,
    )
    embed.add_field(
        name="Damage",
        value=f"**{int(row['damage'] or 0):,}**",
        inline=True,
    )

    if row["rank_after_label"]:
        rank_value = (
            f"{rank_emoji(row['rank_after_label'])} "
            f"**{row['rank_after_label']}**"
        )
        if row["lp_delta"] is not None:
            rank_value += f"\n`{int(row['lp_delta']):+d} LP`"
        embed.add_field(
            name=t(lang, "ranked_result"),
            value=rank_value,
            inline=False,
        )

    if row["game_end_at"]:
        dt = row["game_end_at"]
        embed.timestamp = (
            dt
            if dt.tzinfo
            else dt.replace(tzinfo=timezone.utc)
        )

    embed.set_footer(
        text="LadderGG · Match tracking"
    )
    return embed


async def _guilds_for_player(puuid: str, platform: str):
    pool=db._pool()
    return await pool.fetch("""
        SELECT DISTINCT lp.guild_id, lp.display_name
        FROM ladder_players lp
        JOIN ladders l ON l.guild_id=lp.guild_id AND l.ladder_id=lp.ladder_id
        WHERE lp.puuid=$1 AND lp.platform=$2
          AND lp.is_registered=TRUE AND l.status<>'archived'
    """,puuid,platform)

async def process_new_matches_for_account(account):
    puuid=account["puuid"]; platform=account["platform"]
    ids=await riot.get_match_ids_by_puuid(puuid,platform,MATCH_LOOKBACK_COUNT)
    for match_id in reversed(ids):
        if await db.tracked_match_exists(match_id):
            continue
        match=await riot.get_match(match_id,platform)
        p=_participant(match,puuid)
        if not p:
            continue
        info=match.get("info") or {}
        current=await riot.get_soloq_entry_by_puuid(puuid,platform)
        after_lp=current.absolute_lp if current else None
        after_label=current.label if current else None
        recent=await db.recent_matches(puuid,platform,1)
        before_lp=recent[0]["rank_after_lp"] if recent else None
        lp_delta=(after_lp-int(before_lp)) if after_lp is not None and before_lp is not None else None

        inserted=await db.insert_tracked_match(
            match_id,puuid,platform,_match_end(match),
            str(p.get("championName") or "Unknown"),bool(p.get("win")),
            int(p.get("kills") or 0),int(p.get("deaths") or 0),int(p.get("assists") or 0),
            int((p.get("totalMinionsKilled") or 0)+(p.get("neutralMinionsKilled") or 0)),
            int(p.get("totalDamageDealtToChampions") or 0),
            int(info.get("gameDuration") or 0),after_lp,after_label,lp_delta,
        )
        if not inserted:
            continue

        stored=(await db.recent_matches(puuid,platform,1))[0]
        for g in await _guilds_for_player(puuid,platform):
            gid=int(g["guild_id"]); settings=await db.get_tracking_settings(gid)
            lang=await db.get_guild_language(gid)
            channel_id=settings["match_alert_channel_id"]
            channel=bot.get_channel(int(channel_id)) if channel_id else None

            if settings["match_alerts_enabled"] and isinstance(channel,discord.TextChannel):
                try:
                    await channel.send(embed=match_card_embed(g["display_name"],stored,lang))
                except discord.HTTPException:
                    logger.exception("Match-Alert konnte nicht gesendet werden (Guild %s).", gid)

            if settings["streak_alerts_enabled"] and isinstance(channel,discord.TextChannel):
                recent_games=await db.recent_matches(puuid,platform,10)
                streak=0
                for x in recent_games:
                    if x["win"]: streak+=1
                    else: break
                if streak>=3:
                    try:
                        await channel.send(embed=discord.Embed(
                            title=f"🔥 {t(lang, 'win_streak')}",
                            description=(f"**{g['display_name']}** has **{streak} wins in a row**." if lang == "en" else f"**{g['display_name']}** ist bei **{streak} Siegen in Folge**.")
                        ))
                    except discord.HTTPException:
                        logger.exception("Streak-Alert konnte nicht gesendet werden (Guild %s).", gid)

async def refresh_match_tracking():
    if not MATCH_TRACKING_ENABLED:
        return
    for account in await db.active_unique_accounts():
        try:
            await process_new_matches_for_account(account)
        except Exception:
            logger.exception("Match-Tracking fehlgeschlagen für %s/%s",account["puuid"],account["platform"])

async def save_daily_rank_snapshots():
    today=datetime.now(timezone.utc).date()
    for account in await db.active_unique_accounts():
        try:
            entry=await riot.get_soloq_entry_cached(account["puuid"],account["platform"])
            if entry:
                await db.save_daily_snapshot(account["puuid"],account["platform"],entry,today)
        except Exception:
            logger.exception("Daily Snapshot fehlgeschlagen für %s",account["puuid"])

async def maybe_post_daily_summaries():
    now=datetime.now(timezone.utc)
    if now.hour<DAILY_SUMMARY_HOUR_UTC:
        return
    today=now.date()
    for guild in bot.guilds:
        lang = await db.get_guild_language(guild.id)
        s=await db.get_tracking_settings(guild.id)
        if not s["daily_summary_enabled"] or s["last_daily_summary_date"]==today:
            continue
        rows=await db.daily_snapshots_for_guild(guild.id,today)
        entries=[]
        for r in rows:
            if r["today_lp"] is None or r["yesterday_lp"] is None:
                continue
            entries.append((int(r["today_lp"])-int(r["yesterday_lp"]),r["display_name"],r["today_label"]))
        entries.sort(reverse=True)
        if not entries:
            continue
        channel_id=s["daily_summary_channel_id"]
        channel=bot.get_channel(int(channel_id)) if channel_id else None
        if isinstance(channel,discord.TextChannel):
            e=discord.Embed(
                title=f"📈 {t(lang, 'daily_summary')}",
                description="\n".join(
                    f"**{name}**  `{delta:+d} LP` · {label}"
                    for delta,name,label in entries[:20]
                ),
            )
            e.set_footer(text=f"{len(entries)} Spieler · LadderGG")
            await channel.send(embed=e)
            await db.mark_daily_summary_sent(guild.id,today)

async def maybe_announce_leader_change(guild_id: int, ladder_id: int):
    ladder = await db.get_ladder(guild_id, ladder_id)
    if not ladder or ladder["status"] != "active":
        return
    rules = parse_rules_jsonb(ladder["rules"])
    competition = feature_rules(rules).get("competition") or {}
    if not competition.get("announce_leader_change", False):
        return
    rows = await db.players_for_ladder(guild_id, ladder_id)
    if not rows:
        return
    leader = (
        max(rows, key=lambda r: calculate_challenge_score(r, rules))
        if ladder["ladder_type"] == "challenge"
        else max(rows, key=lambda r: int(r["current_lp"] or 0))
    )
    uid = int(leader["discord_user_id"])
    previous = await db.last_event_of_type(guild_id, ladder_id, "leader_change")
    old_uid = None
    if previous:
        payload = previous["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        old_uid = payload.get("discord_user_id")
    if old_uid == uid:
        return
    await db.record_ladder_event(
        guild_id, ladder_id, "leader_change",
        f"leader:{uid}:{int(datetime.now(timezone.utc).timestamp())}",
        {"discord_user_id": uid, "player": leader["display_name"]},
        discord_user_id=uid,
    )
    channel_id = ladder["announcement_channel_id"] or ladder["leaderboard_channel_id"]
    channel = bot.get_channel(int(channel_id)) if channel_id else None
    if isinstance(channel, discord.TextChannel):
        lang = await db.get_guild_language(guild_id)
        await channel.send(embed=discord.Embed(
            title=f"👑 {t(lang, 'leader_change')}",
            description=t(lang, "leader_change_text").replace("{player}", str(leader["display_name"])),
        ))


async def maybe_post_milestones(
    guild_id: int,
    ladder_id: int,
):
    ladder = await db.get_ladder(guild_id, ladder_id)
    lang = await db.get_guild_language(guild_id)
    if not ladder or ladder["status"] != "active":
        return

    rules = parse_rules_jsonb(ladder["rules"])
    features = feature_rules(rules)
    if not features.get("milestones_enabled"):
        return

    channel_id = (
        ladder["announcement_channel_id"]
        or ladder["leaderboard_channel_id"]
    )
    channel = bot.get_channel(int(channel_id)) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return

    rows = await db.players_for_ladder(guild_id, ladder_id)
    for row in rows:
        peak_lp = int(row["new_peak_lp"] or 0)
        peak_label = row["new_peak_label"] or "Unbekannt"
        event_key = f"peak:{row['discord_user_id']}:{peak_lp}"
        created = await db.record_ladder_event(
            guild_id,
            ladder_id,
            "new_peak",
            event_key,
            {
                "player": row["display_name"],
                "peak_lp": peak_lp,
                "peak_label": peak_label,
            },
            discord_user_id=int(row["discord_user_id"]),
        )
        if created and peak_lp > int(row["ladder_start_lp"] or 0):
            embed = discord.Embed(
                title=f"🔥 {t(lang, 'new_peak')}",
                description=(
                    f"**{row['display_name']}** erreicht "
                    f"**{peak_label}**."
                ),
            )
            if ladder["ladder_type"] == "challenge":
                score = calculate_challenge_score(row, rules)
                embed.add_field(
                    name="Challenge-Score",
                    value=score_label(score),
                    inline=True,
                )
            embed.set_footer(text=ladder["name"])
            await channel.send(embed=embed)


async def evaluate_ladder_goal(
    guild_id: int,
    ladder_id: int,
):
    ladder = await db.get_ladder(guild_id, ladder_id)
    lang = await db.get_guild_language(guild_id)
    if not ladder or ladder["status"] != "active":
        return

    rules = parse_rules_jsonb(ladder["rules"])
    features = feature_rules(rules)
    goal = features.get("goal") or {}
    goal_type = goal.get("type", "none")

    if goal_type == "none" or not goal.get("auto_finish"):
        return

    rows = await db.players_for_ladder(guild_id, ladder_id)
    winner = None

    for row in rows:
        if goal_type == "rank":
            reached = int(row["current_lp"] or 0) >= int(goal["target"])
        elif goal_type == "score":
            reached = calculate_challenge_score(row, rules) >= float(goal["target"])
        elif goal_type == "wins":
            reached = int(row["wins"] or 0) >= int(goal["target"])
        else:
            reached = False

        if reached:
            winner = row
            break

    if not winner:
        return

    event_key = f"goal:{goal_type}:{goal.get('target')}"
    created = await db.record_ladder_event(
        guild_id,
        ladder_id,
        "goal_reached",
        event_key,
        {
            "player": winner["display_name"],
            "goal": goal,
        },
        discord_user_id=int(winner["discord_user_id"]),
    )
    if not created:
        return

    await db.set_ladder_status(guild_id, ladder_id, "finished")

    channel_id = (
        ladder["announcement_channel_id"]
        or ladder["leaderboard_channel_id"]
    )
    channel = bot.get_channel(int(channel_id)) if channel_id else None
    if isinstance(channel, discord.TextChannel):
        embed = discord.Embed(
            title=f"🏁 {t(lang, 'goal_reached')}",
            description=(
                f"**{winner['display_name']}** hat das Ziel der "
                f"Challenge **{ladder['name']}** erreicht."
            ),
        )
        embed.set_footer(text="Die Ladder wurde automatisch beendet.")
        await channel.send(embed=embed)


async def sync_reward_roles(
    guild_id: int,
    ladder_id: int,
):
    return




async def _post_final_results(guild_id: int, ladder, rules: dict) -> bool:
    """Postet den Endstand einer automatisch beendeten Ladder.

    True  = erledigt (gesendet, oder es gibt keinen/keinen nutzbaren Kanal)
    False = Senden fehlgeschlagen -> beim nächsten Zyklus erneut versuchen
    """
    ladder_id = int(ladder["ladder_id"])
    competition = feature_rules(rules).get("competition") or {}
    channel_id = ladder["announcement_channel_id"] or ladder["leaderboard_channel_id"]
    if not channel_id:
        return True
    channel = bot.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        logger.warning(
            "Endstand: Kanal %s für Guild %s Ladder %s nicht gefunden.",
            channel_id, guild_id, ladder_id,
        )
        return True

    lang = await db.get_guild_language(guild_id)
    rows = await db.players_for_ladder(guild_id, ladder_id)
    ordered = (
        sorted(rows, key=lambda r: calculate_challenge_score(r, rules), reverse=True)
        if ladder["ladder_type"] == "challenge"
        else sorted(rows, key=lambda r: int(r["current_lp"] or 0), reverse=True)
    )
    lines = []
    for idx, row in enumerate(ordered[:3], 1):
        medal = {1:"🥇",2:"🥈",3:"🥉"}[idx]
        result = (
            score_label(calculate_challenge_score(row, rules))
            if ladder["ladder_type"] == "challenge"
            else (row["current_label"] or "Unranked")
        )
        lines.append(f"{medal} **{row['display_name']}** · {result}")
    # Podium rewards
    for idx, row in enumerate(ordered[:3], start=1):
        reward = competition.get(f"reward_{idx}", "")
        if reward and idx <= len(lines):
            lines[idx - 1] += f" · 🎁 {reward}"

    # Special awards
    award_lines = []
    if rows:
        biggest = max(
            rows,
            key=lambda r: int(r["new_peak_lp"] or 0) - int(r["ladder_start_lp"] or 0),
        )
        award_lines.append(
            f"📈 **{t(lang, 'biggest_climber')}:** "
            f"{biggest['display_name']} "
            f"({int(biggest['new_peak_lp'] or 0) - int(biggest['ladder_start_lp'] or 0):+d} LP)"
        )

        most_games = max(
            rows,
            key=lambda r: int(r["wins"] or 0) + int(r["losses"] or 0),
        )
        mg = int(most_games["wins"] or 0) + int(most_games["losses"] or 0)
        award_lines.append(
            f"🎮 **{t(lang, 'most_games')}:** "
            f"{most_games['display_name']} ({mg})"
        )

        highest_peak = max(
            rows,
            key=lambda r: int(r["new_peak_lp"] or 0),
        )
        award_lines.append(
            f"👑 **{t(lang, 'highest_peak')}:** "
            f"{highest_peak['display_name']} · "
            f"{highest_peak['new_peak_label'] or '—'}"
        )

        wr_candidates = [
            r for r in rows
            if int(r["wins"] or 0) + int(r["losses"] or 0) >= 20
        ]
        if wr_candidates:
            best_wr = max(
                wr_candidates,
                key=lambda r: (
                    int(r["wins"] or 0)
                    / max(1, int(r["wins"] or 0) + int(r["losses"] or 0))
                ),
            )
            games = int(best_wr["wins"] or 0) + int(best_wr["losses"] or 0)
            wr = int(best_wr["wins"] or 0) / max(1, games) * 100
            award_lines.append(
                f"🔥 **{t(lang, 'best_winrate')}:** "
                f"{best_wr['display_name']} ({wr:.1f}%)"
            )

    desc = "\n".join(lines) if lines else t(lang, "no_players")
    if award_lines:
        desc += "\n\n" + "\n".join(award_lines)
    try:
        await channel.send(embed=discord.Embed(
            title=f"🏁 {ladder['name']} · {t(lang, 'final_results')}",
            description=desc,
        ))
    except discord.Forbidden:
        logger.warning(
            "Endstand: keine Schreibrechte in Kanal %s (Guild %s Ladder %s).",
            channel_id, guild_id, ladder_id,
        )
    return True



async def run_ladder_lifecycle():
    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        ladders = await db.list_ladders(guild.id, include_archived=False)
        for ladder in ladders:
            ladder_id = int(ladder["ladder_id"])
            rules = parse_rules_jsonb(ladder["rules"])
            competition = feature_rules(rules).get("competition") or {}
            finished_now = False
            try:
                if (
                    ladder["status"] == "scheduled"
                    and ladder["starts_at"]
                    and now >= ladder["starts_at"]
                    and competition.get("auto_start", True)
                ):
                    await db.set_ladder_status(guild.id, ladder_id, "active")
                    await db.audit(guild.id, "auto_start", ladder_id=ladder_id)

                if (
                    ladder["status"] == "active"
                    and ladder["ends_at"]
                    and now >= ladder["ends_at"]
                    and competition.get("auto_finish_at_end", True)
                ):
                    await db.set_final_announce_pending(guild.id, ladder_id, True)
                    await db.set_ladder_status(guild.id, ladder_id, "finished")
                    await db.audit(guild.id, "auto_finish", ladder_id=ladder_id)
                    finished_now = True

                # Endstand posten. Der Status steht schon auf "finished";
                # bis die Nachricht wirklich raus ist, bleibt final_announce_pending
                # gesetzt und es wird im nächsten Zyklus erneut versucht.
                if finished_now or (
                    ladder["status"] == "finished"
                    and ladder["final_announce_pending"]
                ):
                    await post_or_edit_ladder_leaderboard(guild.id, ladder_id)
                    if await _post_final_results(guild.id, ladder, rules):
                        await db.set_final_announce_pending(
                            guild.id, ladder_id, False
                        )
            except Exception:
                logger.exception("Lifecycle automation failed for guild %s ladder %s", guild.id, ladder_id)


async def run_ladder_automations():
    for guild in bot.guilds:
        ladders = await db.list_ladders(
            guild.id,
            include_archived=False,
        )
        for ladder in ladders:
            if ladder["status"] != "active":
                continue
            ladder_id = int(ladder["ladder_id"])
            try:
                await maybe_announce_leader_change(guild.id, ladder_id)
                await maybe_post_milestones(guild.id, ladder_id)
                await evaluate_ladder_goal(guild.id, ladder_id)
                await sync_reward_roles(guild.id, ladder_id)
            except Exception:
                logger.exception(
                    "Ladder-Automation fehlgeschlagen: Guild %s Ladder %s",
                    guild.id,
                    ladder_id,
                )


async def post_all_multi_ladder_leaderboards():
    for guild in bot.guilds:
        ladders = await db.list_ladders(
            guild.id,
            include_archived=False,
        )
        for ladder in ladders:
            if ladder["status"] in {"active", "scheduled", "paused"}:
                try:
                    await post_or_edit_ladder_leaderboard(
                        guild.id,
                        int(ladder["ladder_id"]),
                    )
                except Exception:
                    logger.exception(
                        "Multi-Ladder Update fehlgeschlagen: Guild %s Ladder %s",
                        guild.id,
                        ladder["ladder_id"],
                    )


async def post_or_edit_leaderboard(guild_id: int):
    config = await db.get_guild_config(guild_id)

    if not config:
        return

    channel_id = config["leaderboard_channel_id"]

    if not channel_id:
        return

    channel = bot.get_channel(int(channel_id))

    if not isinstance(channel, discord.TextChannel):
        logger.warning(
            "Leaderboard Channel %s für Guild %s nicht gefunden.",
            channel_id,
            guild_id,
        )
        return

    rows = await db.players_for_guild(guild_id)
    last_update = await db.latest_refresh_for_guild(guild_id)
    ladder = await db.get_default_ladder(guild_id)
    ladder_type = ladder["ladder_type"] if ladder else "challenge"
    rules = parse_rules_jsonb(ladder["rules"]) if ladder else {}
    ladder_name = ladder["name"] if ladder else (config["ladder_name"] or "LadderGG")
    pages = build_leaderboard_embeds(
        rows,
        ladder_name,
        last_update,
        ladder_type,
        rules,
    )
    old_ids = await db.get_leaderboard_message_ids(guild_id)
    new_ids: list[int] = []

    for index, embed in enumerate(pages):
        msg = None

        if index < len(old_ids):
            try:
                msg = await channel.fetch_message(old_ids[index])

                if not _message_has_same_leaderboard(msg, embed):
                    await msg.edit(content=None, embed=embed)
                else:
                    logger.debug(
                        "Legacy-Leaderboard unverändert: Guild %s Seite %s – PATCH übersprungen.",
                        guild_id,
                        index + 1,
                    )

            except discord.NotFound:
                msg = None
            except (
                discord.Forbidden,
                discord.HTTPException,
                ValueError,
            ):
                logger.exception(
                    "Leaderboard-Seite %s konnte nicht geprüft/editiert werden "
                    "(Guild %s).",
                    index + 1,
                    guild_id,
                )
                if index < len(old_ids):
                    new_ids.append(old_ids[index])
                    continue
                msg = None

        if msg is None:
            msg = await channel.send(embed=embed)

        new_ids.append(msg.id)

    for extra_id in old_ids[len(pages):]:
        try:
            old_msg = await channel.fetch_message(extra_id)
            await old_msg.delete()
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            ValueError,
        ):
            logger.exception(
                "Alte Leaderboard-Seite konnte nicht gelöscht werden "
                "(Guild %s).",
                guild_id,
            )

    await db.replace_leaderboard_message_ids(
        guild_id,
        new_ids,
    )


async def post_all_leaderboards():
    guild_ids = await db.guild_ids_with_players()

    for guild_id in guild_ids:
        try:
            await post_or_edit_leaderboard(guild_id)
        except Exception:
            logger.exception(
                "Leaderboard Update fehlgeschlagen für Guild %s",
                guild_id,
            )


_last_failure_alert: Optional[datetime] = None


async def _maybe_alert_total_failure(updated: int, failed: int):
    """Schlägt jeder Riot-Abruf eines Zyklus fehl (typisch: abgelaufener
    oder ungültiger API-Key), kommt höchstens alle 3 Stunden ein Alert."""
    global _last_failure_alert
    if updated > 0 or failed == 0:
        return
    now = datetime.now(timezone.utc)
    if _last_failure_alert and now - _last_failure_alert < timedelta(hours=3):
        return
    _last_failure_alert = now
    if riot.key_invalid:
        reason = (
            "Der RIOT_API_KEY ist abgelaufen oder ungültig (HTTP 403). "
            "Neuen Key im Riot Developer Portal erzeugen, in die .env "
            "eintragen und `sudo systemctl restart laddergg` ausführen."
        )
    else:
        reason = "Bitte Riot-Status und Netzwerkverbindung prüfen."
    await send_alert(
        f"🚨 Alle {failed} Riot-Abrufe im Updatezyklus sind fehlgeschlagen. "
        + reason
    )


async def _safe_periodic_step(name: str, awaitable):
    try:
        return await awaitable
    except Exception:
        logger.exception("Periodic step failed: %s", name)
        return None


@tasks.loop(minutes=UPDATE_MINUTES)
async def periodic_update():
    started = datetime.now(timezone.utc)

    result = await _safe_periodic_step(
        "refresh_unique_riot_accounts",
        refresh_unique_riot_accounts(),
    )
    updated, failed = result if result is not None else (0, 0)
    await _maybe_alert_total_failure(updated, failed)
    await _safe_periodic_step("refresh_match_tracking", refresh_match_tracking())
    await _safe_periodic_step("save_daily_rank_snapshots", save_daily_rank_snapshots())
    await _safe_periodic_step("maybe_post_daily_summaries", maybe_post_daily_summaries())
    # V10 uses per-ladder leaderboards. Running the old guild-wide updater
    # as well caused duplicate PATCH requests and Discord 429 rate limits.
    await _safe_periodic_step(
        "post_all_multi_ladder_leaderboards",
        post_all_multi_ladder_leaderboards(),
    )
    await _safe_periodic_step("run_ladder_lifecycle", run_ladder_lifecycle())
    await _safe_periodic_step("run_ladder_automations", run_ladder_automations())
    await _safe_periodic_step("sync_all_google_sheets", sync_all_google_sheets())

    elapsed = (
        datetime.now(timezone.utc) - started
    ).total_seconds()

    logger.info(
        "Updatezyklus fertig: %s Riot Accounts aktualisiert, "
        "%s Fehler, %.1f Sekunden.",
        updated,
        failed,
        elapsed,
    )


@periodic_update.before_loop
async def before_periodic_update():
    await bot.wait_until_ready()


@periodic_update.error
async def periodic_update_error(error: Exception):
    # discord.py's tasks.loop stops permanently on an unhandled exception
    # unless it is restarted here. Ohne das würde ein einziger Hiccup
    # (DB-Timeout, Netzwerkfehler, ...) alle Updates für immer stoppen,
    # bis der Bot manuell neu gestartet wird.
    logger.exception(
        "periodic_update Loop abgestürzt, wird neu gestartet.",
        exc_info=error,
    )
    await send_alert(
        f"⚠️ Update-Loop abgestürzt und wird neu gestartet: {error}"
    )
    if not periodic_update.is_running():
        periodic_update.restart()


class OnboardingView(discord.ui.View):
    def __init__(self, lang: str = "de"):
        super().__init__(timeout=None)
        self.open_setup.label = (
            "Setup-Panel öffnen" if lang == "de" else "Open setup panel"
        )

    @discord.ui.button(
        label="Open setup panel",
        emoji="🏆",
        style=discord.ButtonStyle.primary,
        custom_id="onboarding_open_setup",
    )
    async def open_setup(self, interaction: discord.Interaction, button):
        if not await member_is_ladder_admin(interaction):
            lang = await db.get_guild_language(interaction.guild.id)
            await interaction.response.send_message(
                (
                    "Only server admins or the configured Ladder Admin "
                    "role can open the setup panel."
                    if lang == "en"
                    else "Nur Server-Admins oder die konfigurierte "
                    "Ladder-Admin-Rolle können das Setup-Panel öffnen."
                ),
                ephemeral=True,
            )
            return
        await setup.callback(interaction)


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        await db.ensure_guild(guild)
        lang = await db.get_guild_language(guild.id)

        channel = guild.system_channel
        if not channel or not channel.permissions_for(guild.me).send_messages:
            # Fallback: ersten Textkanal nehmen, in dem der Bot schreiben darf.
            for candidate in guild.text_channels:
                if candidate.permissions_for(guild.me).send_messages:
                    channel = candidate
                    break

        if not channel:
            return

        if lang == "en":
            embed = discord.Embed(
                title="👋 Welcome to LadderGG!",
                description=(
                    "LadderGG tracks League of Legends ranked leaderboards "
                    "and peak-race competitions right in this server.\n\n"
                    "**Get started in 3 steps:**\n"
                    "**1.** `/config language` — set English or German for "
                    "all bot messages on this server.\n"
                    "**2.** `/config setup` — open the admin panel to create "
                    "your first ladder (leaderboard channel, ranked type, rules).\n"
                    "**3.** Tell your members to run `/register` or "
                    "`/ladder join` to add themselves.\n\n"
                    "Run **`/commands`** any time for the full command list."
                ),
                color=0xC8A24D,
            )
            embed.set_footer(
                text="Only server admins can run /config commands."
            )
        else:
            embed = discord.Embed(
                title="👋 Willkommen bei LadderGG!",
                description=(
                    "LadderGG trackt League-of-Legends-Ranked-Leaderboards und "
                    "Peak-Race-Wettbewerbe direkt in diesem Server.\n\n"
                    "**In 3 Schritten startklar:**\n"
                    "**1.** `/config language` — Deutsch oder Englisch für "
                    "alle Bot-Nachrichten auf diesem Server festlegen.\n"
                    "**2.** `/config setup` — öffnet das Admin-Panel, um die "
                    "erste Ladder anzulegen (Leaderboard-Channel, Ranked-Typ, Regeln).\n"
                    "**3.** Sag deinen Mitgliedern, sie sollen `/register` "
                    "oder `/ladder join` ausführen, um sich einzutragen.\n\n"
                    "Mit **`/commands`** jederzeit die vollständige Befehlsübersicht anzeigen."
                ),
                color=0xC8A24D,
            )
            embed.set_footer(
                text="Nur Server-Admins können /config-Befehle ausführen."
            )

        await channel.send(embed=embed, view=OnboardingView(lang))
    except Exception:
        logger.exception("Onboarding failed for guild %s", guild.id)


_startup_alert_sent = False


@bot.event
async def on_ready():
    global _startup_alert_sent
    logger.info("Discord verbunden als %s", bot.user)

    if not _startup_alert_sent:
        _startup_alert_sent = True
        await send_alert(
            f"✅ LadderGG ist online (Guilds: {len(bot.guilds)})."
        )

    # OnboardingView ist generisch (custom_id-basiert) und muss nur einmal
    # global registriert werden, nicht pro Nachricht - der Button
    # funktioniert danach auch nach einem Bot-Neustart weiter.
    bot.add_view(OnboardingView())

    # Öffentliche Join-/Leaderboard-/Regeln-Buttons nach Neustarts reaktivieren.
    for guild in bot.guilds:
        try:
            ladders = await db.list_ladders(
                guild.id,
                include_archived=False,
            )
            for ladder in ladders:
                if (
                    ladder["announcement_message_id"]
                    and ladder["status"] != "archived"
                ):
                    bot.add_view(
                        LadderAnnouncementView(
                            int(ladder["ladder_id"]),
                            await db.get_guild_language(guild.id),
                        ),
                        message_id=int(
                            ladder["announcement_message_id"]
                        ),
                    )
        except Exception:
            logger.exception(
                "Persistent Views konnten für Guild %s nicht geladen werden.",
                guild.id,
            )




@bot.tree.command(
    description="Registriert deine Riot ID für das LadderGG-Leaderboard."
)
@app_commands.describe(
    game_name="Riot ID Name, z. B. Faker",
    tag_line="Riot ID Tag, z. B. EUW",
    region="Region, z. B. EUW, EUW1, EUN1, NA1 oder KR",
    alltime_peak="All Time Peak, z. B. Master 120 LP oder Emerald I 75 LP",
    display_name="Name im Leaderboard, optional",
)
async def register(
    interaction: discord.Interaction,
    game_name: str,
    tag_line: str,
    region: str,
    alltime_peak: str,
    display_name: Optional[str] = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server.",
            ephemeral=True,
        )
        return

    lang = await db.get_guild_language(interaction.guild.id)

    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )

    try:
        await db.ensure_guild(interaction.guild)
        ladder = await db.get_default_ladder(interaction.guild.id)

        alltime_peak_lp, alltime_peak_label = parse_alltime_peak(
            alltime_peak, lang
        )
        platform = normalize_platform(region)

        account = await riot.get_account_by_riot_id(
            game_name.strip(),
            tag_line.strip().lstrip("#"),
            platform,
        )

        puuid = account["puuid"]
        canonical_game_name = account.get("gameName") or game_name.strip()
        canonical_tag_line = (
            account.get("tagLine")
            or tag_line.strip().lstrip("#")
        )

        entry = await riot.get_soloq_entry_by_puuid(
            puuid,
            platform,
        )

        if entry is None:
            await interaction.followup.send(
                (
                    "I can't find any SoloQ ranked data for this account "
                    "right now."
                    if lang == "en"
                    else "Ich finde für diesen Account aktuell keine "
                    "SoloQ-Rank-Daten."
                ),
                ephemeral=True,
            )
            return

        await db.upsert_riot_account(
            puuid,
            platform,
            canonical_game_name,
            canonical_tag_line,
            entry,
        )

        name = (
            display_name.strip()
            if display_name and display_name.strip()
            else interaction.user.display_name
        )

        register_peak_lp = alltime_peak_lp
        register_peak_label = alltime_peak_label

        if ladder and ladder["ladder_type"] == "challenge":
            rules = parse_rules_jsonb(ladder["rules"])
            start_mode = (rules.get("start") or {}).get("mode", "all_time_peak")
            if start_mode == "current":
                register_peak_lp = entry.absolute_lp
                register_peak_label = entry.label

        await db.register_player(
            interaction.guild.id,
            interaction.user.id,
            puuid,
            platform,
            name,
            register_peak_lp,
            register_peak_label,
            entry,
        )

        await post_or_edit_leaderboard(interaction.guild.id)

        delta = weighted_delta(
            alltime_peak_lp,
            entry.absolute_lp,
            alltime_peak_label,
            entry.label,
            alltime_peak_lp
            if alltime_peak_label.lower().startswith("challenger")
            else (
                entry.absolute_lp
                if entry.tier == "CHALLENGER"
                else 0
            ),
        )

        registered_word = "Registered" if lang == "en" else "Registriert"
        await interaction.followup.send(
            f"{registered_word}: **{name}** | "
            f"Riot ID: **{canonical_game_name}#{canonical_tag_line}** | "
            f"Region: **{platform.upper()}** | "
            f"Current: **{entry.label}** | "
            f"New Peak: **{entry.label}** | "
            f"All Time Peak: **{alltime_peak_label}** | "
            f"Weighted Delta: **{format_delta(delta)}**.",
            ephemeral=True,
        )

    except Exception as exc:
        logger.exception("/register fehlgeschlagen")
        await interaction.followup.send(
            f"{'Error' if lang == 'en' else 'Fehler'}: {exc}",
            ephemeral=True,
        )


@bot.tree.command(
    description="Entfernt dich aus dem LadderGG-Leaderboard."
)
async def unregister(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server.",
            ephemeral=True,
        )
        return

    lang = await db.get_guild_language(interaction.guild.id)

    await db.ensure_guild(interaction.guild)
    await db.unregister_player(
        interaction.guild.id,
        interaction.user.id,
    )
    await post_or_edit_leaderboard(interaction.guild.id)

    await interaction.response.send_message(
        (
            "You have been removed from the leaderboard."
            if lang == "en"
            else "Du wurdest aus dem Leaderboard entfernt."
        ),
        ephemeral=True,
    )


@bot.tree.command(
    description="Zeigt das Leaderboard dieses Discord-Servers."
)
async def leaderboard(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server.",
            ephemeral=True,
        )
        return

    lang = await db.get_guild_language(interaction.guild.id)

    await db.ensure_guild(interaction.guild)
    config = await db.get_guild_config(interaction.guild.id)
    rows = await db.players_for_guild(interaction.guild.id)
    last_update = await db.latest_refresh_for_guild(interaction.guild.id)
    ladder = await db.get_default_ladder(interaction.guild.id)
    ladder_type = ladder["ladder_type"] if ladder else "challenge"
    rules = parse_rules_jsonb(ladder["rules"]) if ladder else {}
    ladder_name = ladder["name"] if ladder else (
        config["ladder_name"] if config else "LadderGG"
    )
    pages = build_leaderboard_embeds(
        rows,
        ladder_name,
        last_update,
        ladder_type,
        rules,
        lang=lang,
    )

    await interaction.response.send_message(embed=pages[0])

    for embed in pages[1:]:
        await interaction.followup.send(embed=embed)






TRANSLATIONS = {
    "de": {
        "not_updated": "Noch kein Update",
        "no_players": "Noch keine Teilnehmer.",
        "use_join": "Nutze **Beitreten**, um teilzunehmen.",
        "players": "Teilnehmer",
        "updated": "Update",
        "scoring": "Wertung",
        "goal": "Ziel",
        "current": "Aktuell",
        "challenge_peak": "Challenge Peak",
        "all_time_peak": "All-Time Peak",
        "active_modifiers": "Aktive Multiplikatoren",
        "join": "Beitreten",
        "leaderboard": "Leaderboard",
        "rules": "Regeln",
        "new_ladder": "Neue Ladder",
        "manage_ladder": "Ladder verwalten",
        "archive": "Archiv",
        "tracking": "Tracking & Alerts",
        "normal_ladder": "Normal Ladder",
        "challenge_ladder": "Challenge Ladder",
        "choose_ladder_type": "Welche Ladder möchtest du erstellen?",
        "choose_ladder": "Ladder auswählen:",
        "no_active_ladder": "Keine aktive Ladder vorhanden.",
        "archive_empty": "Archiv ist leer.",
        "edit": "Bearbeiten",
        "pause_resume": "Pausieren/Fortsetzen",
        "template": "Als Vorlage",
        "archive_action": "Archivieren",
        "challenge_settings": "Challenge Einstellungen",
        "multiplier": "Multiplikator",
        "extras": "Extras",
        "save": "Speichern",
        "leaderboard_channel": "Leaderboard-Channel",
        "announcement_channel": "Ankündigungs-Channel",
        "start_scoring": "Startwert & Wertung",
        "rank_multiplier": "Rang für Multiplikator",
        "select_channel_first": "Bitte zuerst den Leaderboard-Channel wählen.",
        "challenge_only": "Diese Einstellungen gelten nur für Challenge Ladders.",
        "multiplier_only": "Multiplikatoren gelten nur für Challenge Ladders.",
        "created": "erstellt",
        "updated_word": "aktualisiert",
        "join_success": "Du bist der Ladder beigetreten.",
        "no_riot": "Kein Riot-Account gefunden.",
        "no_matches": "Noch keine Matches getrackt.",
        "recent_soloq": "Letzte SoloQ Games",
        "overview": "Übersicht",
        "tracked_games": "getrackte SoloQ Games",
        "wins_in_row": "Siege in Folge",
        "losses_in_row": "Niederlagen in Folge",
        "ranked_result": "Ranked Ergebnis",
        "daily_summary": "Daily LP Summary",
        "new_peak": "Neuer Challenge-Peak!",
        "goal_reached": "Challenge-Ziel erreicht!",
        "win_streak": "Win Streak!",
        "challenge_started": "Eine neue LadderGG-Challenge wurde gestartet.",
        "ladder_started": "Eine neue LadderGG-Ladder wurde gestartet.",
        "click_join": "Klicke auf „Beitreten“, um dich anzumelden.",
        "server_lang_de": "Serversprache auf **Deutsch** gesetzt.",
        "server_lang_en": "Server language set to **English**.",
        "sheet_overview": "Server Ladders",
        "sheet_name": "Name",
        "sheet_type": "Typ",
        "sheet_status": "Status",
        "sheet_participants": "Teilnehmer",
        "sheet_channel": "Leaderboard Channel",
        "sheet_period": "Zeitraum",
        "sheet_total_games": "Games gesamt",
        "sheet_leader": "Aktueller Leader",
        "sheet_rules": "Regeln",
        "sheet_start_value": "Startwert",
        "sheet_negative": "Negative Punkte",
        "sheet_late_join": "Beitritt nach Start",
        "sheet_min": "Mindestpunktzahl",
        "sheet_max": "Maximalpunktzahl",
        "sheet_multipliers": "Multiplikatoren",
        "sheet_player": "Spieler",
        "sheet_region": "Region",
        "sheet_winrate": "Winrate",
        "sheet_last_update": "Letztes Update",
        "yes": "Ja",
        "no": "Nein",
        "none": "Keine",
        "new_ladder_title": "Neue Ladder",
        "challenge_settings_title": "Challenge Einstellungen",
        "extras_title": "Extras & Automatisierung",
        "multiplier_title": "Multiplikator",
        "join_title": "Ladder beitreten",
        "tracking_title": "Tracking & Alerts",
        "goal_label": "Automatisches Ziel",
        "goal_placeholder": "none | rank:Master 0 LP | score:500 | wins:100",
        "milestones_label": "Milestone-Meldungen? (ja/nein)",
        "milestones_placeholder": "Neue Peaks und erreichte Ziele posten",
        "required_role_label": "Benötigte Discord-Rollen-ID (optional)",
        "required_role_placeholder": "leer = jede Rolle erlaubt",
        "multiplier_rank_label": "Rang",
        "multiplier_value_label": "Multiplikator",
        "multiplier_after_label": "Erst ab Rang-Eintritt? (ja/nein)",
        "competition_title": "Competition & Scoring",
        "scoring_mode_label": "Wertungsmodus",
        "scoring_mode_placeholder": "peak_gain | current_gain | wins | winrate | games",
        "win_loss_points_label": "Punkte pro Sieg / Niederlage",
        "win_loss_points_placeholder": "z. B. 10 / -2",
        "minimum_games_label": "Mindestanzahl Games für Score",
        "minimum_games_placeholder": "0 = deaktiviert",
        "format_label": "Format",
        "format_placeholder": "solo | teams",
        "advanced": "Erweitert",
        "advanced_title": "Erweiterte Einstellungen",
        "timing_limits": "Zeitraum & Limits",
        "scoring_extras_title": "Zusätzliche Wertung",
        "participation_title": "Teilnahme",
        "automation_rewards": "Automatisierung & Preise",
        "back": "Zurück",
        "registration_open": "Anmeldung offen",
        "promotion": "Aufstieg!",
        "demotion": "Abstieg",
        "today": "Heute",
        "this_week": "Diese Woche",
        "period_gain": "LP-Veränderung",
        "invite_only": "Nur per Einladung",
        "public_join": "Öffentlich",
        "role_only": "Nur Discord-Rolle",
        "health_ok": "OK",
        "health_error": "Fehler",
        "biggest_climber": "Größter Climb",
        "best_winrate": "Beste Winrate",
        "most_games": "Meiste Games",
        "highest_peak": "Höchster Peak",
        "final_results": "Endstand",
        "registration_closed": "Anmeldung geschlossen",
        "reward_text": "Preis / Belohnung",
        "leader_change": "Führungswechsel",
        "leader_change_text": "{player} übernimmt Platz 1.",
        "ladder_not_found": "Ladder nicht gefunden.",
        "template_created": "✅ Vorlage erstellt. Neue Ladder-ID: `{id}`",
        "status_now": "✅ Status: **{status}**",
        "archive_confirm_body": (
            "Die Ladder wird eingefroren. "
            "Falls Google Sheets verbunden ist, wird ein eigener Archiv-Tab "
            "mit Endstand und Regeln erstellt.\n\n"
            "**Wirklich archivieren?**"
        ),
        "archive_confirm_button": "Ja, archivieren",
        "archived_success": "✅ Ladder archiviert.",
        "archive_failed": "❌ Beim Archivieren ist etwas schiefgelaufen.",
        "save_template_button": "Als Vorlage speichern",
        "audit_log_button": "Audit Log",
        "no_audit_entries": "Keine Audit-Einträge.",
        "audit_log_title": "📜 Ladder Audit Log",
    },
    "en": {
        "not_updated": "Not updated yet",
        "no_players": "No players yet.",
        "use_join": "Use **Join** to enter this ladder.",
        "players": "players",
        "updated": "Updated",
        "scoring": "Scoring",
        "goal": "Goal",
        "current": "Current",
        "challenge_peak": "Challenge Peak",
        "all_time_peak": "All-Time Peak",
        "active_modifiers": "Active modifiers",
        "join": "Join",
        "leaderboard": "Leaderboard",
        "rules": "Rules",
        "new_ladder": "New ladder",
        "manage_ladder": "Manage ladder",
        "archive": "Archive",
        "tracking": "Tracking & Alerts",
        "normal_ladder": "Normal Ladder",
        "challenge_ladder": "Challenge Ladder",
        "choose_ladder_type": "Which ladder would you like to create?",
        "choose_ladder": "Choose a ladder:",
        "no_active_ladder": "No active ladder available.",
        "archive_empty": "The archive is empty.",
        "edit": "Edit",
        "pause_resume": "Pause/Resume",
        "template": "Use as template",
        "archive_action": "Archive",
        "challenge_settings": "Challenge settings",
        "multiplier": "Multiplier",
        "extras": "Extras",
        "save": "Save",
        "leaderboard_channel": "Leaderboard channel",
        "announcement_channel": "Announcement channel",
        "start_scoring": "Start value & scoring",
        "rank_multiplier": "Rank for multiplier",
        "select_channel_first": "Please select a leaderboard channel first.",
        "challenge_only": "These settings only apply to challenge ladders.",
        "multiplier_only": "Multipliers only apply to challenge ladders.",
        "created": "created",
        "updated_word": "updated",
        "join_success": "You joined the ladder.",
        "no_riot": "No Riot account found.",
        "no_matches": "No matches tracked yet.",
        "recent_soloq": "Recent SoloQ",
        "overview": "Overview",
        "tracked_games": "tracked SoloQ games",
        "wins_in_row": "wins in a row",
        "losses_in_row": "losses in a row",
        "ranked_result": "Ranked result",
        "daily_summary": "Daily LP Summary",
        "new_peak": "New Challenge Peak!",
        "goal_reached": "Challenge goal reached!",
        "win_streak": "Win Streak!",
        "challenge_started": "A new LadderGG challenge has started.",
        "ladder_started": "A new LadderGG ladder has started.",
        "click_join": "Click “Join” to enter.",
        "server_lang_de": "Serversprache auf **Deutsch** gesetzt.",
        "server_lang_en": "Server language set to **English**.",
        "sheet_overview": "Server Ladders",
        "sheet_name": "Name",
        "sheet_type": "Type",
        "sheet_status": "Status",
        "sheet_participants": "Players",
        "sheet_channel": "Leaderboard Channel",
        "sheet_period": "Period",
        "sheet_total_games": "Total games",
        "sheet_leader": "Current leader",
        "sheet_rules": "Rules",
        "sheet_start_value": "Start value",
        "sheet_negative": "Negative points",
        "sheet_late_join": "Join after start",
        "sheet_min": "Minimum score",
        "sheet_max": "Maximum score",
        "sheet_multipliers": "Multipliers",
        "sheet_player": "Player",
        "sheet_region": "Region",
        "sheet_winrate": "Win rate",
        "sheet_last_update": "Last update",
        "yes": "Yes",
        "no": "No",
        "none": "None",
        "new_ladder_title": "New ladder",
        "challenge_settings_title": "Challenge settings",
        "extras_title": "Extras & automation",
        "multiplier_title": "Multiplier",
        "join_title": "Join ladder",
        "tracking_title": "Tracking & Alerts",
        "goal_label": "Automatic goal",
        "goal_placeholder": "none | rank:Master 0 LP | score:500 | wins:100",
        "milestones_label": "Milestone notifications? (yes/no)",
        "milestones_placeholder": "Post new peaks and reached goals",
        "required_role_label": "Required Discord role ID (optional)",
        "required_role_placeholder": "empty = any role allowed",
        "multiplier_rank_label": "Rank",
        "multiplier_value_label": "Multiplier",
        "multiplier_after_label": "Only after entering rank? (yes/no)",
        "competition_title": "Competition & Scoring",
        "scoring_mode_label": "Scoring mode",
        "scoring_mode_placeholder": "peak_gain | current_gain | wins | winrate | games",
        "win_loss_points_label": "Points per Win / Loss",
        "win_loss_points_placeholder": "e.g. 10 / -2",
        "minimum_games_label": "Minimum games for score",
        "minimum_games_placeholder": "0 = disabled",
        "format_label": "Format",
        "format_placeholder": "solo | teams",
        "advanced": "Advanced",
        "advanced_title": "Advanced settings",
        "timing_limits": "Timing & limits",
        "scoring_extras_title": "Extra scoring",
        "participation_title": "Participation",
        "automation_rewards": "Automation & prizes",
        "back": "Back",
        "registration_open": "Registration open",
        "promotion": "Promotion!",
        "demotion": "Demotion",
        "today": "Today",
        "this_week": "This week",
        "period_gain": "LP change",
        "invite_only": "Invite only",
        "public_join": "Public",
        "role_only": "Discord role only",
        "health_ok": "OK",
        "health_error": "Error",
        "biggest_climber": "Biggest climb",
        "best_winrate": "Best win rate",
        "most_games": "Most games",
        "highest_peak": "Highest peak",
        "final_results": "Final standings",
        "registration_closed": "Registration closed",
        "reward_text": "Prize / reward",
        "leader_change": "Leader change",
        "leader_change_text": "{player} takes #1.",
        "ladder_not_found": "Ladder not found.",
        "template_created": "✅ Template created. New ladder ID: `{id}`",
        "status_now": "✅ Status: **{status}**",
        "archive_confirm_body": (
            "This ladder will be frozen. "
            "If Google Sheets is connected, a separate archive tab "
            "with final standings and rules will be created.\n\n"
            "**Really archive this ladder?**"
        ),
        "archive_confirm_button": "Yes, archive it",
        "archived_success": "✅ Ladder archived.",
        "archive_failed": "❌ Something went wrong while archiving.",
        "save_template_button": "Save as Template",
        "audit_log_button": "Audit Log",
        "no_audit_entries": "No audit entries.",
        "audit_log_title": "📜 Ladder Audit Log",
    },
}


def t(lang: str, key: str) -> str:
    lang = lang if lang in TRANSLATIONS else "de"
    return TRANSLATIONS[lang].get(key, TRANSLATIONS["de"].get(key, key))


def parse_optional_datetime(value: str) -> Optional[datetime]:
    value = value.strip()
    if not value:
        return None
    for fmt in (
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(
        "Datum als `TT.MM.JJJJ` oder `TT.MM.JJJJ HH:MM` eingeben."
    )


def ladder_summary(ladder) -> str:
    icon = "🏆" if ladder["ladder_type"] == "normal" else "🔥"
    return (
        f"{icon} **{ladder['name']}** · `{ladder['status']}` "
        f"· ID `{ladder['ladder_id']}`"
    )


def state_from_ladder(ladder) -> dict:
    return {
        "ladder_id": int(ladder["ladder_id"]),
        "ladder_type": ladder["ladder_type"],
        "name": ladder["name"],
        "rules": parse_rules_jsonb(ladder["rules"]),
        "starts_at": ladder["starts_at"],
        "ends_at": ladder["ends_at"],
        "leaderboard_channel_id": ladder["leaderboard_channel_id"],
        "announcement_channel_id": ladder["announcement_channel_id"],
        "participant_limit": ladder["participant_limit"],
        "admin_approval": bool(ladder["admin_approval"]),
        "selected_tier": "CHALLENGER",
    }


def editor_text(state: dict) -> str:
    lang = state.get("lang", "de")
    ladder_type = state.get("ladder_type", "normal")
    rules = state.get("rules") or {}
    scoring = (rules.get("scoring") or {}).get("mode", "peak_gain")

    type_text = (
        t(lang, "challenge_ladder")
        if ladder_type == "challenge"
        else t(lang, "normal_ladder")
    )
    leaderboard = (
        f"<#{state['leaderboard_channel_id']}>"
        if state.get("leaderboard_channel_id")
        else ("Not selected" if lang == "en" else "Nicht gewählt")
    )
    announcement = (
        f"<#{state['announcement_channel_id']}>"
        if state.get("announcement_channel_id")
        else ("Optional" if lang == "en" else "Optional")
    )

    lines = [
        f"## {'🔥' if ladder_type == 'challenge' else '🏆'} {state.get('name', 'LadderGG')}",
        f"**{'Type' if lang == 'en' else 'Typ'}:** {type_text}",
        f"**Leaderboard:** {leaderboard}",
        f"**{'Announcement' if lang == 'en' else 'Ankündigung'}:** {announcement}",
    ]

    if ladder_type == "challenge":
        lines.append(
            f"**{t(lang, 'scoring')}:** {pretty_scoring_mode(scoring)}"
        )

    if state.get("starts_at") or state.get("ends_at"):
        lines.append(
            f"**{'Period' if lang == 'en' else 'Zeitraum'}:** "
            f"{_sheet_datetime(state.get('starts_at'))} → "
            f"{_sheet_datetime(state.get('ends_at'))}"
        )

    lines.append("")
    lines.append(
        (
            "Choose channels and scoring above. Use **Advanced** only if you need more."
            if lang == "en"
            else
            "Wähle oben Channel und Wertung. **Erweitert** brauchst du nur für Sonderregeln."
        )
    )
    return "\n".join(lines)



class NewLadderModal(discord.ui.Modal, title="Neue Ladder"):
    name = discord.ui.TextInput(
        label="Name",
        placeholder="z. B. Road to Challenger",
        max_length=80,
    )

    def __init__(self, ladder_type: str, lang: str = "de"):
        super().__init__(title=t(lang, "new_ladder_title"))
        self.lang = lang
        self.ladder_type = ladder_type
        self.name.label = "Name"

    async def on_submit(self, interaction: discord.Interaction):
        state = {
            "ladder_type": self.ladder_type,
            "name": str(self.name.value).strip() or "LadderGG",
            "rules": (
                clone_default_challenge_rules()
                if self.ladder_type == "challenge"
                else {}
            ),
            "starts_at": None,
            "ends_at": None,
            "leaderboard_channel_id": None,
            "announcement_channel_id": None,
            "participant_limit": None,
            "admin_approval": False,
            "selected_tier": "CHALLENGER",
            "lang": self.lang,
        }
        await interaction.response.send_message(
            editor_text(state),
            view=LadderEditorView(state),
            ephemeral=True,
        )


class ChallengeSettingsModal(discord.ui.Modal):
    def __init__(self, state: dict, lang: str = "de"):
        super().__init__(
            title=t(lang, "timing_limits"),
            timeout=None,
        )
        self.state = state
        self.lang = lang

        self.start = discord.ui.TextInput(
            label="Start",
            required=False,
            placeholder=(
                "16.08.2026 18:00 · empty = now"
                if lang == "en"
                else "16.08.2026 18:00 · leer = sofort"
            ),
        )
        self.end = discord.ui.TextInput(
            label="End" if lang == "en" else "Ende",
            required=False,
            placeholder=(
                "30.09.2026 20:00 · empty = open"
                if lang == "en"
                else "30.09.2026 20:00 · leer = offen"
            ),
        )
        self.limit = discord.ui.TextInput(
            label="Player limit" if lang == "en" else "Teilnehmerlimit",
            required=False,
            placeholder="50",
        )

        if state.get("starts_at"):
            self.start.default = state["starts_at"].strftime("%d.%m.%Y %H:%M")
        if state.get("ends_at"):
            self.end.default = state["ends_at"].strftime("%d.%m.%Y %H:%M")
        if state.get("participant_limit"):
            self.limit.default = str(state["participant_limit"])

        self.add_item(self.start)
        self.add_item(self.end)
        self.add_item(self.limit)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            starts = parse_optional_datetime(str(self.start.value))
            ends = parse_optional_datetime(str(self.end.value))
            if starts and ends and ends <= starts:
                raise ValueError(
                    "End must be after start."
                    if self.lang == "en"
                    else "Ende muss nach dem Start liegen."
                )

            limit_raw = str(self.limit.value).strip()
            participant_limit = int(limit_raw) if limit_raw else None
            if participant_limit is not None and participant_limit < 1:
                raise ValueError(
                    "Player limit must be at least 1."
                    if self.lang == "en"
                    else "Teilnehmerlimit muss mindestens 1 sein."
                )
        except ValueError as exc:
            await interaction.response.send_message(
                f"❌ {exc}",
                ephemeral=True,
            )
            return

        self.state["starts_at"] = starts
        self.state["ends_at"] = ends
        self.state["participant_limit"] = participant_limit

        await interaction.response.edit_message(
            content=advanced_settings_text(self.state),
            view=AdvancedSettingsView(self.state),
        )



class CompetitionSettingsModal(discord.ui.Modal):
    def __init__(self, state: dict, lang: str = "de"):
        super().__init__(
            title=t(lang, "participation_title"),
            timeout=None,
        )
        self.state = state
        self.lang = lang

        features = feature_rules(state["rules"])
        competition = features.setdefault("competition", {})

        mode = competition.get("registration_mode", "public")
        leader = competition.get("announce_leader_change", False)
        registration = competition.get("registration_open", True)

        self.mode = discord.ui.TextInput(
            label="Mode" if lang == "en" else "Modus",
            placeholder="public | role | invite",
            default=str(mode),
            max_length=20,
        )
        self.leader_alerts = discord.ui.TextInput(
            label=(
                "Leader change alerts? (yes/no)"
                if lang == "en"
                else "Führungswechsel melden? (ja/nein)"
            ),
            default=(
                "yes" if leader else "no"
            ) if lang == "en" else (
                "ja" if leader else "nein"
            ),
            max_length=10,
        )
        self.registration = discord.ui.TextInput(
            label=(
                "Registration open? (yes/no)"
                if lang == "en"
                else "Anmeldung offen? (ja/nein)"
            ),
            default=(
                "yes" if registration else "no"
            ) if lang == "en" else (
                "ja" if registration else "nein"
            ),
            max_length=10,
        )

        self.add_item(self.mode)
        self.add_item(self.leader_alerts)
        self.add_item(self.registration)

    async def on_submit(self, interaction: discord.Interaction):
        mode = str(self.mode.value).strip().lower()
        if mode not in {"public", "role", "invite"}:
            await interaction.response.send_message(
                (
                    "❌ Mode must be `public`, `role` or `invite`."
                    if self.lang == "en"
                    else "❌ Modus muss `public`, `role` oder `invite` sein."
                ),
                ephemeral=True,
            )
            return

        yes = {"yes", "y", "ja", "j", "true", "1"}
        no = {"no", "n", "nein", "false", "0"}

        leader_raw = str(self.leader_alerts.value).strip().lower()
        reg_raw = str(self.registration.value).strip().lower()

        if leader_raw not in yes | no or reg_raw not in yes | no:
            await interaction.response.send_message(
                (
                    "❌ Please use yes/no."
                    if self.lang == "en"
                    else "❌ Bitte ja/nein verwenden."
                ),
                ephemeral=True,
            )
            return

        competition = feature_rules(
            self.state["rules"]
        ).setdefault("competition", {})
        competition["registration_mode"] = mode
        competition["announce_leader_change"] = leader_raw in yes
        competition["registration_open"] = reg_raw in yes

        await interaction.response.edit_message(
            content=advanced_settings_text(self.state),
            view=AdvancedSettingsView(self.state),
        )



class ExtraScoringModal(discord.ui.Modal):
    def __init__(self, state: dict, lang: str = "de"):
        super().__init__(
            title=t(lang, "scoring_extras_title"),
            timeout=None,
        )
        self.state = state
        self.lang = lang

        extras = feature_rules(
            state["rules"]
        ).setdefault("scoring_extras", {})
        scoring = state["rules"].setdefault("scoring", {})

        self.win_loss_points = discord.ui.TextInput(
            label=(
                "Points per Win / Loss"
                if lang == "en"
                else "Punkte pro Sieg / Niederlage"
            ),
            default=(
                f"{extras.get('points_per_win', 0)} / "
                f"{extras.get('points_per_loss', 0)}"
            ),
            placeholder="0 / 0",
            max_length=25,
        )
        self.minimum_games = discord.ui.TextInput(
            label="Minimum games" if lang == "en" else "Mindestspiele",
            default=str(extras.get("minimum_games_for_score", 0)),
            max_length=10,
        )
        self.allow_negative = discord.ui.TextInput(
            label=(
                "Allow negative progress? (yes/no)"
                if lang == "en"
                else "Negativen Fortschritt erlauben? (ja/nein)"
            ),
            default=(
                "yes" if scoring.get("allow_negative", True) else "no"
            ) if lang == "en" else (
                "ja" if scoring.get("allow_negative", True) else "nein"
            ),
            placeholder="yes / no" if lang == "en" else "ja / nein",
            max_length=5,
        )
        self.floor = discord.ui.TextInput(
            label="Minimum score" if lang == "en" else "Mindestscore",
            required=False,
            default="" if scoring.get("floor") is None else str(scoring["floor"]),
            max_length=12,
        )
        self.cap = discord.ui.TextInput(
            label="Maximum score" if lang == "en" else "Maximalscore",
            required=False,
            default="" if scoring.get("cap") is None else str(scoring["cap"]),
            max_length=12,
        )

        self.add_item(self.win_loss_points)
        self.add_item(self.minimum_games)
        self.add_item(self.allow_negative)
        self.add_item(self.floor)
        self.add_item(self.cap)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            raw_points = str(self.win_loss_points.value).strip()
            if "/" not in raw_points:
                raise ValueError(
                    "Use `win / loss`, e.g. `0 / 0`."
                    if self.lang == "en"
                    else "Bitte `Sieg / Niederlage` eingeben, z. B. `0 / 0`."
                )
            win_raw, loss_raw = raw_points.split("/", 1)
            win_points = float(win_raw.strip().replace(",", "."))
            loss_points = float(loss_raw.strip().replace(",", "."))
            minimum_games = max(0, int(str(self.minimum_games.value).strip()))

            negative_raw = str(self.allow_negative.value).strip().lower()
            yes_values = {"yes", "y", "true", "1", "ja", "j"}
            no_values = {"no", "n", "false", "0", "nein"}
            if negative_raw in yes_values:
                allow_negative = True
            elif negative_raw in no_values:
                allow_negative = False
            else:
                raise ValueError(
                    "Negative progress must be `yes` or `no`."
                    if self.lang == "en"
                    else "Negativer Fortschritt muss `ja` oder `nein` sein."
                )

            floor_raw = str(self.floor.value).strip()
            cap_raw = str(self.cap.value).strip()
            floor = float(floor_raw.replace(",", ".")) if floor_raw else None
            cap = float(cap_raw.replace(",", ".")) if cap_raw else None
            if floor is not None and cap is not None and cap < floor:
                raise ValueError(
                    "Maximum score must be >= minimum score."
                    if self.lang == "en"
                    else "Maximalscore muss >= Mindestscore sein."
                )
        except ValueError as exc:
            await interaction.response.send_message(
                f"❌ {exc}",
                ephemeral=True,
            )
            return

        features = feature_rules(self.state["rules"])
        extras = features.setdefault("scoring_extras", {})
        extras["points_per_win"] = win_points
        extras["points_per_loss"] = loss_points
        extras["minimum_games_for_score"] = minimum_games

        scoring = self.state["rules"].setdefault("scoring", {})
        scoring["allow_negative"] = allow_negative
        scoring["floor"] = floor
        scoring["cap"] = cap

        await interaction.response.edit_message(
            content=advanced_settings_text(self.state),
            view=AdvancedSettingsView(self.state),
        )


class FeatureSettingsModal(discord.ui.Modal, title="Extras & Automatisierung"):
    goal = discord.ui.TextInput(
        label="Automatisches Ziel",
        required=False,
        placeholder="none | rank:Master 0 LP | score:500 | wins:100",
    )
    milestones = discord.ui.TextInput(
        label="Milestone-Meldungen? (ja/nein)",
        default="nein",
        placeholder="Neue Peaks und erreichte Ziele posten",
    )
    required_role = discord.ui.TextInput(
        label="Benötigte Discord-Rollen-ID (optional)",
        required=False,
        placeholder="leer = jede Rolle erlaubt",
    )

    reward_text = discord.ui.TextInput(
        label="Preis / Belohnung (optional)",
        required=False,
        placeholder="1st | 2nd | 3rd (e.g. €50 | €25 | €10)",
        max_length=120,
    )

    def __init__(self, state: dict, lang: str = "de"):
        super().__init__(title=t(lang, "automation_rewards"))
        self.state = state
        self.lang = lang
        self.goal.label = t(lang, "goal_label")
        self.goal.placeholder = t(lang, "goal_placeholder")
        self.milestones.label = t(lang, "milestones_label")
        self.milestones.placeholder = t(lang, "milestones_placeholder")
        self.required_role.label = t(lang, "required_role_label")
        self.required_role.placeholder = t(lang, "required_role_placeholder")
        self.reward_text.label = t(lang, "reward_text")
        self.reward_text.placeholder = "€50 | €25 | €10" if lang == "en" else "50€ | 25€ | 10€"
        features = feature_rules(state["rules"])
        goal = features.get("goal") or {}
        goal_type = goal.get("type", "none")
        target = goal.get("target")

        if goal_type == "rank" and goal.get("label"):
            self.goal.default = f"rank:{goal['label']}"
        elif goal_type in {"score", "wins"} and target is not None:
            self.goal.default = f"{goal_type}:{target}"
        else:
            self.goal.default = "none"

        self.milestones.default = (
            "ja" if features.get("milestones_enabled") else "nein"
        )

        eligibility = features.get("eligibility") or {}
        competition = features.setdefault("competition", {})
        if eligibility.get("required_role_id"):
            self.required_role.default = str(eligibility["required_role_id"])
        rewards = [
            str(competition.get("reward_1") or ""),
            str(competition.get("reward_2") or ""),
            str(competition.get("reward_3") or ""),
        ]
        if any(rewards):
            self.reward_text.default = " | ".join(rewards)

    async def on_submit(self, interaction: discord.Interaction):
        yes = {"ja", "j", "yes", "y", "true", "1"}
        no = {"nein", "n", "no", "false", "0"}

        try:
            goal = parse_goal_text(str(self.goal.value), self.lang)
            milestone_raw = str(self.milestones.value).strip().lower()
            if milestone_raw not in yes | no:
                raise ValueError(
                    "Use `yes` or `no` for milestone notifications."
                    if self.lang == "en"
                    else "Milestone-Meldungen bitte mit `ja` oder `nein` angeben."
                )

            def role_id(raw: str) -> Optional[int]:
                raw = raw.strip()
                if not raw:
                    return None
                value = int(raw)
                if value <= 0:
                    raise ValueError(
                        "Role IDs must be positive numbers."
                        if self.lang == "en"
                        else "Rollen-IDs müssen positive Zahlen sein."
                    )
                return value

            required_role_id = role_id(str(self.required_role.value))

        except ValueError as exc:
            await interaction.response.send_message(
                f"❌ {exc}",
                ephemeral=True,
            )
            return

        features = feature_rules(self.state["rules"])
        features["goal"] = goal
        features["milestones_enabled"] = milestone_raw in yes
        features.setdefault("eligibility", {})[
            "required_role_id"
        ] = required_role_id
        reward_parts = [
            p.strip()
            for p in str(self.reward_text.value).split("|")
        ]
        reward_parts += [""] * (3 - len(reward_parts))
        competition = features.setdefault("competition", {})
        competition["reward_1"] = reward_parts[0] if reward_parts else ""
        competition["reward_2"] = reward_parts[1] if len(reward_parts) > 1 else ""
        competition["reward_3"] = reward_parts[2] if len(reward_parts) > 2 else ""

        await interaction.response.edit_message(
            content=advanced_settings_text(self.state),
            view=AdvancedSettingsView(self.state),
        )


def advanced_settings_text(state: dict) -> str:
    lang = state.get("lang", "de")
    rules = _normalize_multiplier_rules(state.get("rules") or {})
    multipliers = rules.get("multipliers") or []

    mult_text = " · ".join(
        f"{str(m['tier']).title()} ×{float(m.get('multiplier', 1.0)):g}"
        for m in multipliers
    )

    title = "## 🛠️ Advanced settings" if lang == "en" else "## 🛠️ Erweiterte Einstellungen"
    hint = (
        "Changes are stored directly in this ladder draft. Use **Back** to return to the same setup and press **Save**."
        if lang == "en"
        else
        "Änderungen werden direkt in diesem Ladder-Entwurf gespeichert. Mit **Zurück** kommst du zum selben Setup und kannst dort **Speichern**."
    )
    mult_label = "Multipliers" if lang == "en" else "Multiplikatoren"

    return f"{title}\n\n**{mult_label}:**\n{mult_text}\n\n{hint}"


class MultiplierModal(discord.ui.Modal):
    def __init__(self, state: dict, lang: str = "de"):
        super().__init__(
            title=(
                "Tier multipliers"
                if lang == "en"
                else "Elo-Multiplikatoren"
            ),
            timeout=None,
        )
        self.state = state
        self.lang = lang

        rules = _normalize_multiplier_rules(self.state["rules"])
        by_tier = {
            str(item["tier"]).upper(): float(item.get("multiplier", 1.0))
            for item in rules.get("multipliers") or []
        }

        default_lines = "\n".join(
            f"{tier}={by_tier.get(tier, 1.0):g}"
            for tier in [
                "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM",
                "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER",
            ]
        )

        self.values = discord.ui.TextInput(
            label=(
                "Multiplier per rank"
                if lang == "en"
                else "Multiplikator pro Elo"
            ),
            style=discord.TextStyle.paragraph,
            default=default_lines,
            placeholder=(
                "IRON=1.0\nBRONZE=1.0\n...\nCHALLENGER=1.3"
            ),
            required=True,
            max_length=600,
        )
        self.add_item(self.values)

    async def on_submit(self, interaction: discord.Interaction):
        valid_tiers = [
            "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM",
            "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER",
        ]

        parsed = {}
        try:
            for raw_line in str(self.values.value).splitlines():
                line = raw_line.strip()
                if not line:
                    continue

                if "=" not in line:
                    raise ValueError(
                        (
                            f"Invalid line: `{line}`. Use e.g. `DIAMOND=1.2`."
                            if self.lang == "en"
                            else f"Ungültige Zeile: `{line}`. Nutze z. B. `DIAMOND=1.2`."
                        )
                    )

                tier_raw, value_raw = line.split("=", 1)
                tier = tier_raw.strip().upper()
                if tier not in valid_tiers:
                    raise ValueError(
                        (
                            f"Unknown rank `{tier}`."
                            if self.lang == "en"
                            else f"Unbekannte Elo `{tier}`."
                        )
                    )

                value = float(value_raw.strip().replace(",", "."))
                if value < 0 or value > 10:
                    raise ValueError(
                        (
                            f"{tier}: multiplier must be between 0 and 10."
                            if self.lang == "en"
                            else f"{tier}: Multiplikator muss zwischen 0 und 10 liegen."
                        )
                    )
                parsed[tier] = value

            missing = [tier for tier in valid_tiers if tier not in parsed]
            if missing:
                raise ValueError(
                    (
                        "Missing ranks: " + ", ".join(missing)
                        if self.lang == "en"
                        else "Fehlende Elos: " + ", ".join(missing)
                    )
                )

        except ValueError as exc:
            await interaction.response.send_message(
                f"❌ {exc}",
                ephemeral=True,
            )
            return

        old_by_tier = {
            str(item.get("tier", "")).upper(): item
            for item in (self.state["rules"].get("multipliers") or [])
            if isinstance(item, dict)
        }

        self.state["rules"]["multipliers"] = [
            {
                "tier": tier,
                "multiplier": parsed[tier],
                "only_after_entry": bool(
                    (old_by_tier.get(tier) or {}).get(
                        "only_after_entry",
                        False,
                    )
                ),
            }
            for tier in valid_tiers
        ]

        # IMPORTANT: stay inside the Advanced panel. Previously this replaced
        # the ephemeral Advanced message with a brand-new LadderEditorView,
        # which looked like a second setup and made the flow confusing.
        await interaction.response.edit_message(
            content=advanced_settings_text(self.state),
            view=AdvancedSettingsView(self.state),
        )




class AdvancedSettingsView(discord.ui.View):
    def __init__(self, state: dict):
        super().__init__(timeout=900)
        self.state = state
        self.lang = state.get("lang", "de")

        labels = [
            t(self.lang, "timing_limits"),
            t(self.lang, "scoring_extras_title"),
            t(self.lang, "participation_title"),
            t(self.lang, "automation_rewards"),
            t(self.lang, "multiplier"),
        ]
        for item, label in zip(self.children, labels):
            if isinstance(item, discord.ui.Button):
                item.label = label

        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label == "Back":
                item.label = "Back" if self.lang == "en" else "Zurück"

    @discord.ui.button(
        label="Timing",
        style=discord.ButtonStyle.secondary,
        emoji="🕒",
        row=0,
    )
    async def timing(self, interaction, button):
        await interaction.response.send_modal(
            ChallengeSettingsModal(self.state, self.lang)
        )

    @discord.ui.button(
        label="Extra Scoring",
        style=discord.ButtonStyle.secondary,
        emoji="➕",
        row=0,
    )
    async def extra_scoring(self, interaction, button):
        if self.state["ladder_type"] != "challenge":
            await interaction.response.send_message(
                (
                    "Extra scoring is only available for challenge ladders."
                    if self.lang == "en"
                    else "Zusätzliche Wertung gibt es nur bei Challenge Ladders."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            ExtraScoringModal(self.state, self.lang)
        )

    @discord.ui.button(
        label="Participation",
        style=discord.ButtonStyle.secondary,
        emoji="👥",
        row=0,
    )
    async def participation(self, interaction, button):
        if self.state["ladder_type"] != "challenge":
            await interaction.response.send_message(
                (
                    "Participation settings are only available for challenge ladders."
                    if self.lang == "en"
                    else "Teilnahme-Einstellungen gibt es nur bei Challenge Ladders."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            CompetitionSettingsModal(self.state, self.lang)
        )

    @discord.ui.button(
        label="Automation",
        style=discord.ButtonStyle.secondary,
        emoji="✨",
        row=0,
    )
    async def automation(self, interaction, button):
        if self.state["ladder_type"] != "challenge":
            await interaction.response.send_message(
                (
                    "Automation is only available for challenge ladders."
                    if self.lang == "en"
                    else "Automatisierung gibt es nur bei Challenge Ladders."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            FeatureSettingsModal(self.state, self.lang)
        )

    @discord.ui.button(
        label="Multiplier",
        style=discord.ButtonStyle.secondary,
        emoji="✖️",
        row=0,
    )
    async def multiplier(self, interaction, button):
        if self.state["ladder_type"] != "challenge":
            await interaction.response.send_message(
                t(self.lang, "multiplier_only"),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            MultiplierModal(
                self.state,
                self.lang,
            )
        )
    @discord.ui.button(
        label="Back",
        style=discord.ButtonStyle.primary,
        emoji="⬅️",
        row=1,
    )
    async def back_to_setup(self, interaction, button):
        await interaction.response.edit_message(
            content=editor_text(self.state),
            view=LadderEditorView(self.state),
        )



class LadderEditorView(discord.ui.View):
    def __init__(self, state: dict):
        super().__init__(timeout=1800)
        self.state = state
        self.lang = state.get("lang", "de")

        # Decorator buttons are copied to the instance; localize them here.
        label_map = {
            "Erweitert": t(self.lang, "advanced"),
            "Vorschau": "Preview" if self.lang == "en" else "Vorschau",
            "Speichern": t(self.lang, "save"),
        }
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label in label_map:
                item.label = label_map[item.label]

        leaderboard = discord.ui.ChannelSelect(
            placeholder=t(self.lang, "leaderboard_channel"),
            channel_types=[discord.ChannelType.text],
            row=0,
        )
        leaderboard.callback = self.on_leaderboard
        self.leaderboard_select = leaderboard
        self.add_item(leaderboard)

        announcement = discord.ui.ChannelSelect(
            placeholder=t(self.lang, "announcement_channel"),
            channel_types=[discord.ChannelType.text],
            row=1,
        )
        announcement.callback = self.on_announcement
        self.announcement_select = announcement
        self.add_item(announcement)

        if state["ladder_type"] == "challenge":
            scoring = discord.ui.Select(
                placeholder=t(self.lang, "start_scoring"),
                options=[
                    discord.SelectOption(
                        label="Peak Gain",
                        value="all_time_peak|peak_gain",
                        description=(
                            "Best peak improvement"
                            if self.lang == "en"
                            else "Verbesserung des besten Peaks"
                        ),
                    ),
                    discord.SelectOption(
                        label="Current Gain",
                        value="current|current_gain",
                        description=(
                            "Current LP compared to start"
                            if self.lang == "en"
                            else "Aktuelle LP gegenüber Start"
                        ),
                    ),
                    discord.SelectOption(
                        label="Wins",
                        value="current|wins",
                        description=(
                            "Most wins"
                            if self.lang == "en"
                            else "Meiste Siege"
                        ),
                    ),
                    discord.SelectOption(
                        label="Winrate",
                        value="current|winrate",
                        description=(
                            "Highest win rate"
                            if self.lang == "en"
                            else "Höchste Winrate"
                        ),
                    ),
                ],
                row=2,
            )
            scoring.callback = self.on_scoring
            self.scoring_select = scoring
            self.add_item(scoring)


    async def redraw(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=editor_text(self.state),
            view=LadderEditorView(self.state),
        )

    async def on_leaderboard(self, interaction: discord.Interaction):
        self.state["leaderboard_channel_id"] = int(
            self.leaderboard_select.values[0].id
        )
        await self.redraw(interaction)

    async def on_announcement(self, interaction: discord.Interaction):
        self.state["announcement_channel_id"] = int(
            self.announcement_select.values[0].id
        )
        await self.redraw(interaction)

    async def on_scoring(self, interaction: discord.Interaction):
        start_mode, scoring_mode = self.scoring_select.values[0].split("|", 1)
        self.state["rules"].setdefault("start", {})["mode"] = start_mode
        self.state["rules"].setdefault("scoring", {})["mode"] = scoring_mode
        await self.redraw(interaction)






    @discord.ui.button(
        label="Erweitert",
        style=discord.ButtonStyle.secondary,
        emoji="🛠️",
        row=4,
    )
    async def advanced(self, interaction, button):
        await interaction.response.edit_message(
            content=advanced_settings_text(self.state),
            view=AdvancedSettingsView(self.state),
        )

    @discord.ui.button(
        label="Vorschau",
        style=discord.ButtonStyle.secondary,
        emoji="👁️",
        row=4,
    )
    async def preview(self, interaction, button):
        rules = self.state.get("rules") or {}
        lang = self.lang

        embed = discord.Embed(
            title=(
                f"🔥 {self.state['name']}"
                if self.state["ladder_type"] == "challenge"
                else f"🏆 {self.state['name']}"
            ),
            description=(
                "Preview · Challenge Ladder"
                if self.state["ladder_type"] == "challenge"
                else "Preview · Normal Ladder"
            ),
        )

        if self.state["ladder_type"] == "challenge":
            scoring = (rules.get("scoring") or {}).get(
                "mode",
                "peak_gain",
            )
            embed.add_field(
                name=t(lang, "scoring"),
                value=pretty_scoring_mode(scoring),
                inline=True,
            )

        embed.add_field(
            name="Start",
            value=_sheet_datetime(self.state.get("starts_at")),
            inline=True,
        )
        embed.add_field(
            name="End",
            value=_sheet_datetime(self.state.get("ends_at")),
            inline=True,
        )
        embed.set_footer(
            text=(
                "This is only a preview. Nothing has been published yet."
                if lang == "en"
                else "Nur Vorschau – noch nichts veröffentlicht."
            )
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Speichern",
        style=discord.ButtonStyle.success,
        emoji="✅",
        row=3,
    )
    async def save(self, interaction, button):
        if not self.state.get("leaderboard_channel_id"):
            await interaction.response.send_message(
                ("Please select a leaderboard channel first." if self.lang == "en" else "Bitte zuerst den Leaderboard-Channel wählen."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if self.state.get("ladder_id"):
                ladder_id = int(self.state["ladder_id"])
                await db.update_ladder(
                    interaction.guild.id,
                    ladder_id,
                    name=self.state["name"],
                    leaderboard_channel_id=self.state["leaderboard_channel_id"],
                    rules=self.state["rules"],
                    starts_at=self.state.get("starts_at"),
                    ends_at=self.state.get("ends_at"),
                    announcement_channel_id=self.state.get(
                        "announcement_channel_id"
                    ),
                    participant_limit=self.state.get("participant_limit"),
                    admin_approval=False,
                )
                action_text = t(self.lang, "updated_word")
            else:
                ladder_id = await db.create_ladder(
                    interaction.guild.id,
                    self.state["name"],
                    self.state["ladder_type"],
                    self.state["leaderboard_channel_id"],
                    rules=self.state["rules"],
                    starts_at=self.state.get("starts_at"),
                    ends_at=self.state.get("ends_at"),
                    announcement_channel_id=self.state.get(
                        "announcement_channel_id"
                    ),
                    participant_limit=self.state.get("participant_limit"),
                    admin_approval=False,
                )
                self.state["ladder_id"] = ladder_id
                action_text = t(self.lang, "created")

            await post_or_edit_ladder_leaderboard(
                interaction.guild.id,
                ladder_id,
            )

            if self.state.get("announcement_channel_id"):
                await post_ladder_announcement(
                    interaction.guild.id,
                    ladder_id,
                )

            await db.audit(
                interaction.guild.id,
                "ladder_update" if action_text == t(self.lang, "updated_word") else "ladder_create",
                ladder_id=ladder_id,
                actor_user_id=interaction.user.id,
                details={
                    "name": self.state["name"],
                    "type": self.state["ladder_type"],
                },
            )

            await interaction.followup.send(
                f"✅ **{self.state['name']}** {action_text}.",
                ephemeral=True,
            )
        except Exception:
            logger.exception("Ladder Speichern fehlgeschlagen")
            await interaction.followup.send(
                (
                    "❌ Something went wrong while saving. Please try again."
                    if self.lang == "en"
                    else "❌ Beim Speichern ist etwas schiefgelaufen. Bitte erneut versuchen."
                ),
                ephemeral=True,
            )


class SetupTypeView(discord.ui.View):
    def __init__(self, lang: str = "de"):
        super().__init__(timeout=600)
        self.lang = lang
        self.children[0].label = t(lang, "normal_ladder")
        self.children[1].label = t(lang, "challenge_ladder")

    @discord.ui.button(
        label="Normal Ladder",
        style=discord.ButtonStyle.primary,
        emoji="🏆",
    )
    async def normal(self, interaction, button):
        await interaction.response.send_modal(NewLadderModal("normal", self.lang))

    @discord.ui.button(
        label="Challenge Ladder",
        style=discord.ButtonStyle.danger,
        emoji="🔥",
    )
    async def challenge(self, interaction, button):
        await interaction.response.send_modal(NewLadderModal("challenge", self.lang))


class LadderSelect(discord.ui.Select):
    def __init__(self, ladders, lang: str = "de"):
        self.lang = lang
        options = [
            discord.SelectOption(
                label=(
                    f"{'🔥' if l['ladder_type'] == 'challenge' else '🏆'} "
                    f"{l['name']}"
                )[:100],
                value=str(l["ladder_id"]),
                description=(
                    f"{l['ladder_type']} · {l['status']} · ID {l['ladder_id']}"
                )[:100],
            )
            for l in ladders[:25]
        ]
        super().__init__(
            placeholder=t(self.lang, "choose_ladder"),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        ladder_id = int(self.values[0])
        ladder = await db.get_ladder(
            interaction.guild.id,
            ladder_id,
        )
        if not ladder:
            await interaction.response.send_message(
                t(self.lang, "ladder_not_found"),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            ladder_summary(ladder),
            view=LadderManageView(ladder_id, self.lang),
            ephemeral=True,
        )


class LadderSelectView(discord.ui.View):
    def __init__(self, ladders, lang: str = "de"):
        super().__init__(timeout=900)
        self.add_item(LadderSelect(ladders, lang))


class TrackingSettingsModal(discord.ui.Modal, title="Tracking & Alerts"):
    match_channel = discord.ui.TextInput(
        label="Match-Alert Channel-ID (optional)", required=False,
        placeholder="leer = Match Alerts aus",
    )
    daily_channel = discord.ui.TextInput(
        label="Daily-Summary Channel-ID (optional)", required=False,
        placeholder="leer = kein Daily Summary Channel",
    )
    promotions = discord.ui.TextInput(label="Promotion Alerts? (ja/nein)",default="ja")
    streaks = discord.ui.TextInput(label="Win-Streak Alerts? (ja/nein)",default="ja")
    daily = discord.ui.TextInput(label="Daily LP Summary? (ja/nein)",default="nein")

    def __init__(self, guild_id: int, lang: str = "de"):
        super().__init__(title=t(lang, "tracking_title"))
        self.guild_id = guild_id
        self.lang = lang
        self.match_channel.placeholder = (
            "leave empty = alerts off" if lang == "en" else "leer = Match Alerts aus"
        )
        self.daily_channel.placeholder = (
            "leave empty = no daily summary" if lang == "en" else "leer = kein Daily Summary Channel"
        )

    async def on_submit(self,interaction:discord.Interaction):
        yes={"ja","j","yes","y","true","1"}; no={"nein","n","no","false","0"}
        try:
            def oid(raw):
                raw=raw.strip()
                return int(raw) if raw else None
            match_id=oid(str(self.match_channel.value))
            daily_id=oid(str(self.daily_channel.value))
            pr=str(self.promotions.value).strip().lower()
            sr=str(self.streaks.value).strip().lower()
            dr=str(self.daily.value).strip().lower()
            if any(x not in yes|no for x in (pr,sr,dr)):
                raise ValueError(("Use `yes` or `no`." if self.lang == "en" else "Ja/Nein bitte als `ja` oder `nein` eingeben."))
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}",ephemeral=True); return

        await db.update_tracking_settings(
            self.guild_id,
            match_alerts_enabled=match_id is not None,
            match_alert_channel_id=match_id,
            promotion_alerts_enabled=pr in yes,
            streak_alerts_enabled=sr in yes,
            daily_summary_enabled=dr in yes,
            daily_summary_channel_id=daily_id,
        )
        await interaction.response.send_message(("✅ Tracking & Alerts saved." if self.lang == "en" else "✅ Tracking & Alerts gespeichert."),ephemeral=True)

class TemplateNameModal(
    discord.ui.Modal,
    title="Save Ladder Template",
):
    name = discord.ui.TextInput(
        label="Template name",
        placeholder="Road to Challenger",
        max_length=80,
    )

    def __init__(self, ladder_id: int, lang: str = "de"):
        super().__init__()
        self.ladder_id = ladder_id
        self.lang = lang

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            ladder = await db.get_ladder(
                interaction.guild.id,
                self.ladder_id,
            )
            if not ladder:
                await interaction.followup.send(
                    "Ladder not found.",
                    ephemeral=True,
                )
                return

            await db.save_template(
                interaction.guild.id,
                str(self.name.value).strip(),
                ladder["ladder_type"],
                parse_rules_jsonb(ladder["rules"]),
                parse_rules_jsonb(ladder["display_config"]),
                interaction.user.id,
            )
            await db.audit(
                interaction.guild.id,
                "template_save",
                ladder_id=self.ladder_id,
                actor_user_id=interaction.user.id,
                details={"template": str(self.name.value).strip()},
            )
            await interaction.followup.send(
                "✅ Template saved.",
                ephemeral=True,
            )
        except Exception:
            logger.exception("Template speichern fehlgeschlagen")
            await interaction.followup.send(
                "❌ Something went wrong while saving the template.",
                ephemeral=True,
            )


class TemplateSelect(discord.ui.Select):
    def __init__(self, templates, lang: str = "de"):
        self.lang = lang
        options = [
            discord.SelectOption(
                label=str(tpl["name"])[:100],
                value=str(tpl["template_id"]),
                description=str(tpl["ladder_type"]).title(),
            )
            for tpl in templates[:25]
        ]
        super().__init__(
            placeholder=(
                "Choose template"
                if lang == "en"
                else "Template auswählen"
            ),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        template = await db.get_template(
            interaction.guild.id,
            int(self.values[0]),
        )
        if not template:
            await interaction.response.send_message(
                "Template not found.",
                ephemeral=True,
            )
            return

        state = {
            "ladder_type": template["ladder_type"],
            "name": f"{template['name']} Copy",
            "rules": parse_rules_jsonb(template["rules"]),
            "starts_at": None,
            "ends_at": None,
            "leaderboard_channel_id": None,
            "announcement_channel_id": None,
            "participant_limit": None,
            "admin_approval": False,
            "selected_tier": "CHALLENGER",
            "lang": self.lang,
        }

        await interaction.response.send_message(
            editor_text(state),
            view=LadderEditorView(state),
            ephemeral=True,
        )


class TemplateSelectView(discord.ui.View):
    def __init__(self, templates, lang: str = "de"):
        super().__init__(timeout=600)
        self.add_item(TemplateSelect(templates, lang))


class AdminPanelView(discord.ui.View):
    def __init__(self, lang: str = "de"):
        super().__init__(timeout=900)
        self.lang = lang
        labels = [
            ("New" if lang == "en" else "Neu"),
            ("Manage" if lang == "en" else "Verwalten"),
            ("Tracking" if lang == "en" else "Tracking"),
            ("Archive" if lang == "en" else "Archiv"),
        ]
        for item, label in zip(self.children, labels):
            if isinstance(item, discord.ui.Button):
                item.label = label

    @discord.ui.button(
        label="Neue Ladder",
        style=discord.ButtonStyle.success,
        emoji="➕",
    )
    async def new_ladder(self, interaction, button):
        await interaction.response.send_message(
            "Welche Ladder möchtest du erstellen?",
            view=SetupTypeView(self.lang),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Ladder verwalten",
        style=discord.ButtonStyle.primary,
        emoji="⚙️",
    )
    async def manage_ladder(self, interaction, button):
        ladders = await db.list_ladders(
            interaction.guild.id,
            include_archived=False,
        )
        if not ladders:
            await interaction.response.send_message(
                "Keine aktive Ladder vorhanden.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Ladder auswählen:",
            view=LadderSelectView(ladders, self.lang),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Tracking & Alerts",
        style=discord.ButtonStyle.secondary,
        emoji="📡",
    )
    async def tracking_settings(self,interaction,button):
        await interaction.response.send_modal(
            TrackingSettingsModal(interaction.guild.id, self.lang)
        )

    @discord.ui.button(
        label="Archiv",
        style=discord.ButtonStyle.secondary,
        emoji="📦",
    )
    async def archive_list(self, interaction, button):
        ladders = await db.list_ladders(
            interaction.guild.id,
            include_archived=True,
        )
        archived = [l for l in ladders if l["status"] == "archived"]
        if not archived:
            await interaction.response.send_message(
                "Archiv ist leer.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "\n".join(ladder_summary(l) for l in archived[:25]),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Templates",
        style=discord.ButtonStyle.secondary,
        emoji="🧩",
    )
    async def templates(self, interaction, button):
        templates = await db.list_templates(
            interaction.guild.id,
        )
        if not templates:
            await interaction.response.send_message(
                (
                    "No templates yet. Open a ladder and use `Save template`."
                    if self.lang == "en"
                    else "Noch keine Templates. Öffne eine Ladder und nutze `Template speichern`."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            (
                "Choose a template:"
                if self.lang == "en"
                else "Template auswählen:"
            ),
            view=TemplateSelectView(templates, self.lang),
            ephemeral=True,
        )



class LadderManageView(discord.ui.View):
    def __init__(self, ladder_id: int, lang: str = "de"):
        super().__init__(timeout=900)
        self.ladder_id = ladder_id
        self.lang = lang
        labels = [
            t(lang, "edit"),
            t(lang, "pause_resume"),
            t(lang, "template"),
            t(lang, "archive_action"),
            t(lang, "save_template_button"),
            t(lang, "audit_log_button"),
        ]
        for item, label in zip(self.children, labels):
            if isinstance(item, discord.ui.Button):
                item.label = label

    @discord.ui.button(
        label="Bearbeiten",
        style=discord.ButtonStyle.primary,
        emoji="✏️",
    )
    async def edit(self, interaction, button):
        ladder = await db.get_ladder(
            interaction.guild.id,
            self.ladder_id,
        )
        if not ladder:
            await interaction.response.send_message(
                t(self.lang, "ladder_not_found"),
                ephemeral=True,
            )
            return

        state = state_from_ladder(ladder)
        state["lang"] = self.lang
        await interaction.response.send_message(
            editor_text(state),
            view=LadderEditorView(state),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Pausieren/Fortsetzen",
        style=discord.ButtonStyle.secondary,
        emoji="⏯️",
    )
    async def pause_resume(self, interaction, button):
        ladder = await db.get_ladder(
            interaction.guild.id,
            self.ladder_id,
        )
        if not ladder:
            await interaction.response.send_message(
                t(self.lang, "ladder_not_found"),
                ephemeral=True,
            )
            return

        new_status = (
            "active"
            if ladder["status"] == "paused"
            else "paused"
        )
        await db.set_ladder_status(
            interaction.guild.id,
            self.ladder_id,
            new_status,
        )
        await interaction.response.send_message(
            t(self.lang, "status_now").format(status=new_status),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Als Vorlage",
        style=discord.ButtonStyle.secondary,
        emoji="♻️",
    )
    async def duplicate(self, interaction, button):
        ladder = await db.get_ladder(
            interaction.guild.id,
            self.ladder_id,
        )
        new_id = await db.duplicate_ladder(
            interaction.guild.id,
            self.ladder_id,
            f"{ladder['name']} Kopie",
        )
        await interaction.response.send_message(
            t(self.lang, "template_created").format(id=new_id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Archivieren",
        style=discord.ButtonStyle.danger,
        emoji="📦",
    )
    async def archive(self, interaction, button):
        await interaction.response.send_message(
            t(self.lang, "archive_confirm_body"),
            view=ArchiveConfirmView(self.ladder_id, self.lang),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Save Template",
        style=discord.ButtonStyle.secondary,
        emoji="🧩",
        row=1,
    )
    async def save_template_button(self, interaction, button):
        await interaction.response.send_modal(
            TemplateNameModal(self.ladder_id, self.lang)
        )

    @discord.ui.button(
        label="Audit Log",
        style=discord.ButtonStyle.secondary,
        emoji="📜",
        row=1,
    )
    async def audit_log_button(self, interaction, button):
        rows = await db.audit_rows(
            interaction.guild.id,
            self.ladder_id,
            limit=15,
        )
        if not rows:
            await interaction.response.send_message(
                t(self.lang, "no_audit_entries"),
                ephemeral=True,
            )
            return

        lines = []
        for row in rows:
            when = row["created_at"]
            ts = int(when.timestamp()) if when else 0
            actor = (
                f"<@{row['actor_user_id']}>"
                if row["actor_user_id"]
                else "System"
            )
            lines.append(
                f"<t:{ts}:R> · **{row['action']}** · {actor}"
            )

        await interaction.response.send_message(
            embed=discord.Embed(
                title=t(self.lang, "audit_log_title"),
                description="\n".join(lines),
            ),
            ephemeral=True,
        )



class ArchiveConfirmView(discord.ui.View):
    def __init__(self, ladder_id: int, lang: str = "de"):
        super().__init__(timeout=300)
        self.ladder_id = ladder_id
        self.lang = lang
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.label = t(lang, "archive_confirm_button")

    @discord.ui.button(
        label="Ja, archivieren",
        style=discord.ButtonStyle.danger,
        emoji="📦",
    )
    async def confirm(self, interaction, button):
        await interaction.response.defer(
            ephemeral=True,
            thinking=True,
        )
        try:
            await archive_ladder(
                interaction.guild.id,
                self.ladder_id,
            )
            await interaction.followup.send(
                t(self.lang, "archived_success"),
                ephemeral=True,
            )
        except Exception:
            logger.exception("Ladder Archivieren fehlgeschlagen")
            await interaction.followup.send(
                t(self.lang, "archive_failed"),
                ephemeral=True,
            )


class JoinRiotModal(discord.ui.Modal, title="Challenge beitreten"):
    game_name = discord.ui.TextInput(
        label="Riot Name",
        placeholder="Arexor",
    )
    tag_line = discord.ui.TextInput(
        label="Riot Tag",
        placeholder="azb",
    )
    region = discord.ui.TextInput(
        label="Region",
        placeholder="EUW",
        default="EUW",
    )
    alltime_peak = discord.ui.TextInput(
        label="All Time Peak (optional)",
        placeholder="Master 200 LP",
        required=False,
    )

    def __init__(self, ladder_id: int, lang: str = "de"):
        super().__init__(title=t(lang, "join_title"))
        self.ladder_id = ladder_id
        self.lang = lang

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._handle_submit(interaction)
        except ValueError as exc:
            await interaction.followup.send(
                f"❌ {exc}",
                ephemeral=True,
            )
        except Exception:
            logger.exception("Ladder Beitritt fehlgeschlagen")
            await interaction.followup.send(
                (
                    "❌ Something went wrong while joining. Please try again."
                    if self.lang == "en"
                    else "❌ Beim Beitreten ist etwas schiefgelaufen. Bitte erneut versuchen."
                ),
                ephemeral=True,
            )

    async def _handle_submit(self, interaction: discord.Interaction):
        ladder = await db.get_ladder(
            interaction.guild.id,
            self.ladder_id,
        )
        if not ladder:
            await interaction.followup.send(
                (
                    "Ladder not found."
                    if self.lang == "en"
                    else "Ladder nicht gefunden."
                ),
                ephemeral=True,
            )
            return

        count = await db.participant_count(
            interaction.guild.id,
            self.ladder_id,
        )
        if (
            ladder["participant_limit"]
            and count >= int(ladder["participant_limit"])
        ):
            await interaction.followup.send(
                "Teilnehmerlimit erreicht.",
                ephemeral=True,
            )
            return

        rules = parse_rules_jsonb(ladder["rules"])
        features = feature_rules(rules)
        eligibility = features.get("eligibility") or {}
        competition = features.get("competition") or {}
        registration_mode = competition.get("registration_mode", "public")

        if not competition.get("registration_open", True):
            await interaction.followup.send(
                t(self.lang, "registration_closed"),
                ephemeral=True,
            )
            return

        if registration_mode == "invite":
            invited = await db.is_ladder_invited(
                interaction.guild.id,
                self.ladder_id,
                interaction.user.id,
            )
            if not invited:
                await interaction.followup.send(
                    (
                        "This ladder is invite-only."
                        if self.lang == "en"
                        else "Diese Ladder ist nur per Einladung zugänglich."
                    ),
                    ephemeral=True,
                )
                return

        if (
            competition.get("registration_closes_at_start", False)
            and ladder["starts_at"]
            and datetime.now(timezone.utc) >= ladder["starts_at"]
        ):
            await interaction.followup.send(t(self.lang, "registration_closed"), ephemeral=True)
            return

        if registration_mode == "invite":
            await interaction.followup.send(
                "This ladder is invite-only." if self.lang == "en"
                else "Diese Ladder ist nur per Einladung zugänglich.",
                ephemeral=True,
            )
            return

        required_role_id = eligibility.get("required_role_id")
        if required_role_id:
            member = interaction.guild.get_member(interaction.user.id)
            if not member or not any(
                role.id == int(required_role_id)
                for role in member.roles
            ):
                await interaction.followup.send(
                    "Du besitzt nicht die für diese Ladder benötigte Discord-Rolle.",
                    ephemeral=True,
                )
                return

        if (
            ladder["starts_at"]
            and datetime.now(timezone.utc) >= ladder["starts_at"]
            and not (rules.get("join") or {}).get(
                "allow_after_start",
                True,
            )
        ):
            await interaction.followup.send(
                "Beitritt nach Challenge-Start ist geschlossen.",
                ephemeral=True,
            )
            return

        platform = normalize_platform(str(self.region.value))
        account = await riot.get_account_by_riot_id(
            str(self.game_name.value).strip(),
            str(self.tag_line.value).strip().lstrip("#"),
            platform,
        )
        entry = await riot.get_soloq_entry_by_puuid(
            account["puuid"],
            platform,
        )
        if entry is None:
            await interaction.followup.send(
                "Keine SoloQ-Rank-Daten gefunden.",
                ephemeral=True,
            )
            return

        min_lp = eligibility.get("min_lp")
        max_lp = eligibility.get("max_lp")
        minimum_games = int(eligibility.get("minimum_games") or 0)
        if (entry.wins + entry.losses) < minimum_games:
            await interaction.followup.send(
                (
                    f"You need at least {minimum_games} ranked games."
                    if self.lang == "en"
                    else f"Du brauchst mindestens {minimum_games} Ranked Games."
                ),
                ephemeral=True,
            )
            return
        if min_lp is not None and entry.absolute_lp < int(min_lp):
            await interaction.followup.send(
                (
                    "Your current rank is below the minimum requirement."
                    if self.lang == "en"
                    else "Dein aktueller Rang liegt unter der Mindestanforderung."
                ),
                ephemeral=True,
            )
            return
        if max_lp is not None and entry.absolute_lp > int(max_lp):
            await interaction.followup.send(
                (
                    "Your current rank is above the maximum allowed elo."
                    if self.lang == "en"
                    else "Dein aktueller Rang liegt über der maximal erlaubten Elo."
                ),
                ephemeral=True,
            )
            return

        peak_text = str(self.alltime_peak.value).strip()

        if ladder["ladder_type"] == "normal":
            start_lp, start_label = entry.absolute_lp, entry.label
        else:
            if peak_text:
                start_lp, start_label = parse_alltime_peak(
                    peak_text, self.lang
                )
            else:
                start_lp, start_label = entry.absolute_lp, entry.label

            if (rules.get("start") or {}).get("mode") == "current":
                start_lp, start_label = entry.absolute_lp, entry.label

        await db.upsert_riot_account(
            account["puuid"],
            platform,
            account.get("gameName") or str(self.game_name.value).strip(),
            account.get("tagLine") or str(self.tag_line.value).strip(),
            entry,
        )

        approved = True
        await db.register_player_for_ladder(
            interaction.guild.id,
            self.ladder_id,
            interaction.user.id,
            account["puuid"],
            platform,
            interaction.user.display_name,
            start_lp,
            start_label,
            entry,
            approved=approved,
        )

        await post_or_edit_ladder_leaderboard(
            interaction.guild.id,
            self.ladder_id,
        )

        await interaction.followup.send(
            f"✅ {t(self.lang, 'join_success')}",
            ephemeral=True,
        )


class LadderAnnouncementView(discord.ui.View):
    def __init__(self, ladder_id: int, lang: str = "de"):
        super().__init__(timeout=None)
        self.ladder_id = ladder_id
        self.lang = lang

        join_button = discord.ui.Button(
            label=t(lang, "join"),
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"laddergg:join:{ladder_id}",
        )
        leaderboard_button = discord.ui.Button(
            label=t(lang, "leaderboard"),
            style=discord.ButtonStyle.primary,
            emoji="📊",
            custom_id=f"laddergg:leaderboard:{ladder_id}",
        )
        rules_button = discord.ui.Button(
            label=t(lang, "rules"),
            style=discord.ButtonStyle.secondary,
            emoji="ℹ️",
            custom_id=f"laddergg:rules:{ladder_id}",
        )

        join_button.callback = self.join
        leaderboard_button.callback = self.show_leaderboard
        rules_button.callback = self.show_rules

        self.add_item(join_button)
        self.add_item(leaderboard_button)
        self.add_item(rules_button)

    async def join(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            JoinRiotModal(self.ladder_id, self.lang)
        )

    async def show_leaderboard(
        self,
        interaction: discord.Interaction,
    ):
        ladder = await db.get_ladder(
            interaction.guild.id,
            self.ladder_id,
        )
        rows = await db.players_for_ladder(
            interaction.guild.id,
            self.ladder_id,
        )
        embeds = build_leaderboard_embeds(
            rows,
            ladder["name"],
            await db.latest_refresh_for_guild(interaction.guild.id),
            ladder["ladder_type"],
            parse_rules_jsonb(ladder["rules"]),
        )
        await interaction.response.send_message(
            embed=embeds[0],
            ephemeral=True,
        )

    async def show_rules(
        self,
        interaction: discord.Interaction,
    ):
        ladder = await db.get_ladder(
            interaction.guild.id,
            self.ladder_id,
        )
        if not ladder:
            await interaction.response.send_message(
                "Ladder not found." if self.lang == "en" else "Ladder nicht gefunden.",
                ephemeral=True,
            )
            return

        rules = parse_rules_jsonb(ladder["rules"])
        lang = self.lang
        scoring = rules.get("scoring") or {}
        start = rules.get("start") or {}
        join = rules.get("join") or {}
        features = feature_rules(rules)
        extras = features.get("scoring_extras") or {}
        competition = features.get("competition") or {}
        eligibility = features.get("eligibility") or {}

        is_challenge = ladder["ladder_type"] == "challenge"

        embed = discord.Embed(
            title=(
                f"📋 {ladder['name']} — Rules"
                if lang == "en"
                else f"📋 {ladder['name']} — Regeln"
            ),
            description=(
                "Only rules that affect this ladder are shown."
                if lang == "en"
                else "Hier werden nur Einstellungen angezeigt, die für diese Ladder relevant sind."
            ),
        )

        if is_challenge:
            mode = scoring.get("mode", "peak_gain")
            start_mode = start.get("mode", "all_time_peak")

            start_labels = {
                "all_time_peak": "All-Time Peak",
                "current": "Current Rank" if lang == "en" else "Aktueller Rang",
                "join_time": "Rank at join" if lang == "en" else "Rang beim Beitritt",
            }

            embed.add_field(
                name="🏆 Scoring" if lang == "en" else "🏆 Wertung",
                value=f"**{pretty_scoring_mode(mode)}**",
                inline=True,
            )
            embed.add_field(
                name="🚀 Start Value" if lang == "en" else "🚀 Startwert",
                value=f"**{start_labels.get(start_mode, str(start_mode).replace('_', ' ').title())}**",
                inline=True,
            )

            # Multipliers: all Elo tiers are configurable, but keep the output compact.
            _normalize_multiplier_rules(rules)
            mult_lines = []
            for item in rules.get("multipliers") or []:
                tier = str(item.get("tier", "")).upper()
                try:
                    value = float(item.get("multiplier", 1.0))
                except (TypeError, ValueError):
                    value = 1.0
                mult_lines.append(
                    f"`{tier.title():<12} ×{value:g}`"
                )

            embed.add_field(
                name="📈 Multipliers" if lang == "en" else "📈 Multiplikatoren",
                value="\n".join(mult_lines),
                inline=False,
            )

            rule_lines = []

            rule_lines.append(
                (
                    f"Negative progress: **{'Yes' if scoring.get('allow_negative', True) else 'No'}**"
                    if lang == "en"
                    else f"Negative Punkte: **{'Ja' if scoring.get('allow_negative', True) else 'Nein'}**"
                )
            )

            rule_lines.append(
                (
                    f"Join after start: **{'Yes' if join.get('allow_after_start', True) else 'No'}**"
                    if lang == "en"
                    else f"Beitritt nach Start: **{'Ja' if join.get('allow_after_start', True) else 'Nein'}**"
                )
            )

            minimum_games = int(extras.get("minimum_games_for_score", 0) or 0)
            rule_lines.append(
                (
                    f"Minimum games: **{minimum_games if minimum_games else 'None'}**"
                    if lang == "en"
                    else f"Mindestspiele: **{minimum_games if minimum_games else 'Keine'}**"
                )
            )

            floor = scoring.get("floor")
            cap = scoring.get("cap")
            rule_lines.append(
                (
                    f"Score floor: **{floor if floor is not None else 'None'}**"
                    if lang == "en"
                    else f"Mindestscore: **{floor if floor is not None else 'Keiner'}**"
                )
            )
            rule_lines.append(
                (
                    f"Score cap: **{cap if cap is not None else 'None'}**"
                    if lang == "en"
                    else f"Maximalscore: **{cap if cap is not None else 'Keiner'}**"
                )
            )

            points_win = float(extras.get("points_per_win", 0.0) or 0.0)
            points_loss = float(extras.get("points_per_loss", 0.0) or 0.0)
            if points_win or points_loss:
                rule_lines.append(
                    (
                        f"Win/Loss bonus: **{points_win:+g} / {points_loss:+g}**"
                        if lang == "en"
                        else f"Sieg/Niederlage: **{points_win:+g} / {points_loss:+g}**"
                    )
                )

            embed.add_field(
                name="⚙️ Rules" if lang == "en" else "⚙️ Regeln",
                value="\n".join(rule_lines),
                inline=False,
            )

            participation_mode = str(
                competition.get("registration_mode", "public")
            ).lower()
            participation_labels = {
                "public": "Public" if lang == "en" else "Öffentlich",
                "role": "Discord role" if lang == "en" else "Discord-Rolle",
                "invite": "Invite only" if lang == "en" else "Nur Einladung",
            }

            participation_lines = [
                (
                    f"Mode: **{participation_labels.get(participation_mode, participation_mode.title())}**"
                    if lang == "en"
                    else f"Modus: **{participation_labels.get(participation_mode, participation_mode.title())}**"
                ),
                (
                    f"Registration: **{'Open' if competition.get('registration_open', True) else 'Closed'}**"
                    if lang == "en"
                    else f"Anmeldung: **{'Offen' if competition.get('registration_open', True) else 'Geschlossen'}**"
                ),
            ]

            required_role_id = eligibility.get("required_role_id")
            if participation_mode == "role" and required_role_id:
                participation_lines.append(
                    (
                        f"Required role: <@&{int(required_role_id)}>"
                        if lang == "en"
                        else f"Benötigte Rolle: <@&{int(required_role_id)}>"
                    )
                )

            embed.add_field(
                name="👥 Participation" if lang == "en" else "👥 Teilnahme",
                value="\n".join(participation_lines),
                inline=False,
            )

            reward_values = [
                str(competition.get("reward_1") or "").strip(),
                str(competition.get("reward_2") or "").strip(),
                str(competition.get("reward_3") or "").strip(),
            ]
            if any(reward_values):
                reward_lines = []
                medals = ["🥇", "🥈", "🥉"]
                for idx, reward in enumerate(reward_values):
                    if reward:
                        reward_lines.append(f"{medals[idx]} {reward}")
                embed.add_field(
                    name="🎁 Rewards" if lang == "en" else "🎁 Preise",
                    value="\n".join(reward_lines),
                    inline=False,
                )

        else:
            embed.add_field(
                name="🏆 Mode" if lang == "en" else "🏆 Modus",
                value="Solo/Duo Rank",
                inline=False,
            )

        if ladder["starts_at"] or ladder["ends_at"]:
            period_lines = []
            if ladder["starts_at"]:
                period_lines.append(
                    f"Start: <t:{int(ladder['starts_at'].timestamp())}:f>"
                )
            if ladder["ends_at"]:
                period_lines.append(
                    (
                        f"End: <t:{int(ladder['ends_at'].timestamp())}:f>"
                        if lang == "en"
                        else f"Ende: <t:{int(ladder['ends_at'].timestamp())}:f>"
                    )
                )
            embed.add_field(
                name="🕒 Period" if lang == "en" else "🕒 Zeitraum",
                value="\n".join(period_lines),
                inline=False,
            )

        embed.set_footer(text="LadderGG")

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )



async def post_ladder_announcement(
    guild_id: int,
    ladder_id: int,
):
    ladder = await db.get_ladder(guild_id, ladder_id)
    lang = await db.get_guild_language(guild_id)
    if not ladder:
        return

    channel_id = (
        ladder["announcement_channel_id"]
        or ladder["leaderboard_channel_id"]
    )
    channel = bot.get_channel(int(channel_id)) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return

    embed = discord.Embed(
        title=(
            f"🔥 {ladder['name']}"
            if ladder["ladder_type"] == "challenge"
            else f"🏆 {ladder['name']}"
        ),
        description=(
            t(lang, "challenge_started")
            if ladder["ladder_type"] == "challenge"
            else t(lang, "ladder_started")
        ),
    )
    if ladder["starts_at"]:
        embed.add_field(
            name="Start",
            value=f"<t:{int(ladder['starts_at'].timestamp())}:f>",
            inline=True,
        )
    if ladder["ends_at"]:
        embed.add_field(
            name="Ende",
            value=f"<t:{int(ladder['ends_at'].timestamp())}:f>",
            inline=True,
        )

    rules = parse_rules_jsonb(ladder["rules"])
    embed.add_field(
        name=("Scoring" if lang == "en" else "Wertung"),
        value=pretty_scoring_mode(
            (rules.get("scoring") or {}).get(
                "mode",
                "peak_gain",
            )
        ),
        inline=True,
    )
    embed.set_footer(
        text=t(lang, "click_join")
    )

    msg = await channel.send(
        embed=embed,
        view=LadderAnnouncementView(ladder_id, lang),
    )
    await db.set_announcement_message(
        guild_id,
        ladder_id,
        msg.id,
    )


async def archive_ladder(
    guild_id: int,
    ladder_id: int,
):
    ladder = await db.get_ladder(guild_id, ladder_id)
    lang = await db.get_guild_language(guild_id)
    if not ladder:
        raise ValueError(
            "Ladder not found." if lang == "en" else "Ladder nicht gefunden."
        )

    rows = await db.players_for_ladder(
        guild_id,
        ladder_id,
        approved_only=False,
    )
    config = await db.get_guild_config(guild_id)

    if (
        config
        and config["sheets_enabled"]
        and config["google_sheet_id"]
    ):
        client = await get_google_client()
        sheet_id = config["google_sheet_id"]

        safe_name = re.sub(
            r"[^A-Za-z0-9 _-]",
            "",
            ladder["name"],
        )[:40]
        archive_name = (
            f"Archiv - {safe_name} - "
            f"{datetime.now().strftime('%Y-%m-%d')}"
        )
        def write_archive():
            spreadsheet = client.open_by_key(sheet_id)
            try:
                worksheet = spreadsheet.worksheet(
                    archive_name[:100]
                )
                worksheet.clear()
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(
                    title=archive_name[:100],
                    rows=max(100, len(rows) + 25),
                    cols=15,
                )

            write_polished_ladder_sheet(
                spreadsheet,
                worksheet,
                rows,
                ladder,
                archived=True,
                lang=lang,
            )

        await asyncio.to_thread(write_archive)

    await db.set_ladder_status(
        guild_id,
        ladder_id,
        "archived",
    )
    await db.audit(
        guild_id,
        "ladder_archive",
        ladder_id=ladder_id,
        details={"name": ladder["name"]},
    )


@bot.tree.command(
    description="Öffnet das LadderGG Admin-Panel."
)
@app_commands.check(is_ladder_admin_check)
async def setup(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server. / Dieser Command funktioniert nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    lang = await db.get_guild_language(interaction.guild.id)
    counts = await db.dashboard_counts(interaction.guild.id)
    ladders = await db.list_ladders(
        interaction.guild.id,
        include_archived=True,
    )
    active = [l for l in ladders if l["status"] != "archived"]
    archived = [l for l in ladders if l["status"] == "archived"]

    description = "### Aktive Ladders\n"
    description += (
        "\n".join(ladder_summary(l) for l in active[:10])
        or "Noch keine."
    )
    description += "\n\n### Archiv\n"
    description += (
        "\n".join(ladder_summary(l) for l in archived[:10])
        or "Noch leer."
    )

    embed = discord.Embed(
        title="⚙️ LadderGG Admin",
        description=(
            f"**{counts['active']} Active · {counts['scheduled']} Scheduled · "
            f"{counts['paused']} Paused · {counts['archived']} Archived**\n\n"
            + description
        ),
    )
    await interaction.response.send_message(
        embed=embed,
        view=AdminPanelView(lang),
        ephemeral=True,
    )


@bot.tree.command(
    description="Zeigt das LadderGG Admin-Panel."
)
@app_commands.check(is_ladder_admin_check)
async def settings(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server. / Dieser Command funktioniert nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    lang = await db.get_guild_language(interaction.guild.id)
    ladders = await db.list_ladders(
        interaction.guild.id,
        include_archived=True,
    )
    active = [l for l in ladders if l["status"] != "archived"]
    archived = [l for l in ladders if l["status"] == "archived"]

    embed = discord.Embed(
        title="⚙️ LadderGG Einstellungen",
        description=(
            "### Aktive Ladders\n"
            + (
                "\n".join(ladder_summary(l) for l in active[:10])
                or "Keine"
            )
            + "\n\n### Archiv\n"
            + (
                "\n".join(ladder_summary(l) for l in archived[:10])
                or "Leer"
            )
        ),
    )
    await interaction.response.send_message(
        embed=embed,
        view=AdminPanelView(lang),
        ephemeral=True,
    )


@bot.tree.command(
    description="Setzt den Channel für das automatische Leaderboard."
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(
    channel="Channel, in dem LadderGG das Leaderboard pflegen soll."
)
async def setleaderboardchannel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server. / Dieser Command funktioniert nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    await db.ensure_guild(interaction.guild)
    await db.set_leaderboard_channel(
        interaction.guild.id,
        channel.id,
    )

    await interaction.response.send_message(
        f"Leaderboard-Channel gesetzt: {channel.mention}",
        ephemeral=True,
    )

    await post_or_edit_leaderboard(interaction.guild.id)


@bot.tree.command(
    description="Ändert den Namen der Ladder dieses Servers."
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(
    ladder_name="Neuer Ladder-Name, z. B. Summer Split 2026"
)
async def setladdername(
    interaction: discord.Interaction,
    ladder_name: str,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server. / Dieser Command funktioniert nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    ladder_name = ladder_name.strip()
    if not ladder_name:
        await interaction.response.send_message(
            "Der Ladder-Name darf nicht leer sein.",
            ephemeral=True,
        )
        return

    if len(ladder_name) > 80:
        await interaction.response.send_message(
            "Der Ladder-Name darf maximal 80 Zeichen lang sein.",
            ephemeral=True,
        )
        return

    await db.ensure_guild(interaction.guild)
    await db.set_ladder_name(interaction.guild.id, ladder_name)
    await post_or_edit_leaderboard(interaction.guild.id)

    await interaction.response.send_message(
        f"Ladder-Name geändert zu **{ladder_name}**.",
        ephemeral=True,
    )


@bot.tree.command(
    description="Zeigt die Regeln der Challenge Ladder."
)
@app_commands.check(is_ladder_admin_check)
async def challengerules(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server. / Dieser Command funktioniert nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    ladder = await db.get_default_ladder(interaction.guild.id)
    if not ladder or ladder["ladder_type"] != "challenge":
        await interaction.response.send_message(
            "Dieser Server nutzt aktuell keine Challenge Ladder.",
            ephemeral=True,
        )
        return

    rules = parse_rules_jsonb(ladder["rules"])
    await interaction.response.send_message(
        "```json\n" + json.dumps(rules, indent=2, ensure_ascii=False)[:1800] + "\n```",
        ephemeral=True,
    )


@bot.tree.command(
    description="Setzt ein Challenge-Preset."
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(
    preset="alltime_peak, climb, road_to_challenger oder positive_gain"
)
async def challengepreset(
    interaction: discord.Interaction,
    preset: str,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server. / Dieser Command funktioniert nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    preset = preset.strip().lower()
    rules = clone_default_challenge_rules()

    if preset == "alltime_peak":
        pass
    elif preset == "climb":
        rules["start"]["mode"] = "current"
        rules["multipliers"] = []
        rules["scoring"]["mode"] = "peak_gain"
    elif preset == "road_to_challenger":
        rules["start"]["mode"] = "current"
        rules["multipliers"] = [
            {"tier": "MASTER", "multiplier": 1.0, "only_after_entry": False},
            {"tier": "GRANDMASTER", "multiplier": 1.15, "only_after_entry": False},
            {"tier": "CHALLENGER", "multiplier": 1.30, "only_after_entry": True},
        ]
    elif preset == "positive_gain":
        rules["start"]["mode"] = "current"
        rules["multipliers"] = []
        rules["scoring"]["allow_negative"] = False
    else:
        await interaction.response.send_message(
            "Unbekanntes Preset. Nutze `alltime_peak`, `climb`, "
            "`road_to_challenger` oder `positive_gain`.",
            ephemeral=True,
        )
        return

    ladder = await db.get_default_ladder(interaction.guild.id)
    if not ladder or ladder["ladder_type"] != "challenge":
        await interaction.response.send_message(
            "Dieser Server nutzt aktuell keine Challenge Ladder.",
            ephemeral=True,
        )
        return

    await db.update_default_ladder_rules(interaction.guild.id, rules)
    await post_or_edit_leaderboard(interaction.guild.id)

    await interaction.response.send_message(
        f"Challenge-Preset **{preset}** wurde übernommen.",
        ephemeral=True,
    )


@bot.tree.command(
    description="Setzt einen Multiplikator für einen Rang in der Challenge."
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(
    tier="z. B. MASTER, GRANDMASTER oder CHALLENGER",
    multiplier="z. B. 1.3",
    only_after_entry="Bonus erst ab Eintritt in diesen Rang",
)
async def challengemultiplier(
    interaction: discord.Interaction,
    tier: str,
    multiplier: float,
    only_after_entry: bool = False,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server. / Dieser Command funktioniert nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    tier = tier.strip().upper()
    if tier not in TIERS:
        await interaction.response.send_message(
            "Unbekannter Rang.",
            ephemeral=True,
        )
        return

    if multiplier < 0 or multiplier > 10:
        await interaction.response.send_message(
            "Multiplikator muss zwischen 0 und 10 liegen.",
            ephemeral=True,
        )
        return

    ladder = await db.get_default_ladder(interaction.guild.id)
    if not ladder or ladder["ladder_type"] != "challenge":
        await interaction.response.send_message(
            "Dieser Server nutzt aktuell keine Challenge Ladder.",
            ephemeral=True,
        )
        return

    rules = parse_rules_jsonb(ladder["rules"], DEFAULT_CHALLENGE_RULES)
    multipliers = [
        r for r in (rules.get("multipliers") or [])
        if str(r.get("tier", "")).upper() != tier
    ]
    multipliers.append({
        "tier": tier,
        "multiplier": multiplier,
        "only_after_entry": only_after_entry,
    })
    rules["multipliers"] = multipliers

    await db.update_default_ladder_rules(interaction.guild.id, rules)
    await post_or_edit_leaderboard(interaction.guild.id)

    await interaction.response.send_message(
        f"{tier}: **{multiplier}x** gespeichert.",
        ephemeral=True,
    )


@bot.tree.command(
    description="Ändert die Basiswertung der Challenge."
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(
    mode="peak_gain, current_gain oder absolute_rank",
    allow_negative="Dürfen negative Werte zählen?",
)
async def challengescoring(
    interaction: discord.Interaction,
    mode: str,
    allow_negative: bool = True,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server. / Dieser Command funktioniert nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    mode = mode.strip().lower()
    if mode not in {"peak_gain", "current_gain", "absolute_rank"}:
        await interaction.response.send_message(
            "Mode muss `peak_gain`, `current_gain` oder `absolute_rank` sein.",
            ephemeral=True,
        )
        return

    ladder = await db.get_default_ladder(interaction.guild.id)
    if not ladder or ladder["ladder_type"] != "challenge":
        await interaction.response.send_message(
            "Dieser Server nutzt aktuell keine Challenge Ladder.",
            ephemeral=True,
        )
        return

    rules = parse_rules_jsonb(ladder["rules"], DEFAULT_CHALLENGE_RULES)
    rules.setdefault("scoring", {})
    rules["scoring"]["mode"] = mode
    rules["scoring"]["allow_negative"] = allow_negative

    await db.update_default_ladder_rules(interaction.guild.id, rules)
    await post_or_edit_leaderboard(interaction.guild.id)

    await interaction.response.send_message(
        f"Challenge-Wertung: **{mode}** · negative Werte: **{allow_negative}**.",
        ephemeral=True,
    )


@bot.tree.command(
    description="Verknüpft ein Google Sheet mit diesem Discord-Server."
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(
    sheet_id="Google-Sheets-URL oder Sheet-ID",
    worksheet_name="Legacy-Feld; LadderGG verwaltet seine Tabs automatisch",
)
async def setsheet(
    interaction: discord.Interaction,
    sheet_id: str,
    worksheet_name: str = "Leaderboard",
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server.",
            ephemeral=True,
        )
        return

    lang = await db.get_guild_language(interaction.guild.id)

    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )

    try:
        await db.ensure_guild(interaction.guild)

        normalized_id = normalize_google_sheet_id(sheet_id)
        worksheet_name = worksheet_name.strip() or "Leaderboard"

        # Test access BEFORE storing the configuration.
        client = await get_google_client()

        def test_access():
            spreadsheet = client.open_by_key(normalized_id)
            # Reading the title also verifies that the service account
            # can actually access the spreadsheet.
            return spreadsheet.title

        spreadsheet_title = await asyncio.to_thread(test_access)

        await db.set_sheet(
            interaction.guild.id,
            normalized_id,
            worksheet_name,
        )

        await sync_google_sheet_for_guild(interaction.guild.id)

        await interaction.followup.send(
            (
                f"✅ Google Sheet **{spreadsheet_title}** connected successfully."
                if lang == "en"
                else f"✅ Google Sheet **{spreadsheet_title}** erfolgreich verbunden."
            ),
            ephemeral=True,
        )

    except gspread.SpreadsheetNotFound:
        try:
            await db.disable_sheet(interaction.guild.id)
        except Exception:
            pass

        account_email = google_service_account_email()
        if lang == "en":
            share_hint = (
                f"\n\nShare the Google Sheet with this service account as **Editor**:\n"
                f"`{account_email}`"
                if account_email
                else
                "\n\nAlso check whether the sheet has been shared with the "
                "Google service account being used."
            )
            await interaction.followup.send(
                "❌ Google Sheet not found or no access (404). "
                "This usually happens with a wrong sheet ID/URL or when "
                "the service account doesn't have access to the sheet."
                + share_hint,
                ephemeral=True,
            )
        else:
            share_hint = (
                f"\n\nTeile das Google Sheet mit diesem Service Account als **Bearbeiter**:\n"
                f"`{account_email}`"
                if account_email
                else
                "\n\nPrüfe außerdem, ob das Sheet mit dem verwendeten Google-Service-Account geteilt wurde."
            )
            await interaction.followup.send(
                "❌ Google Sheet nicht gefunden bzw. kein Zugriff (404). "
                "Das passiert normalerweise bei einer falschen Sheet-ID/URL oder wenn "
                "der Service Account keinen Zugriff auf das Sheet hat."
                + share_hint,
                ephemeral=True,
            )

    except gspread.exceptions.APIError as exc:
        try:
            await db.disable_sheet(interaction.guild.id)
        except Exception:
            pass

        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)

        await interaction.followup.send(
            (
                f"❌ Google Sheets API error"
                f"{f' ({status})' if status else ''}: {exc}"
                if lang == "en"
                else f"❌ Google-Sheets-API-Fehler"
                f"{f' ({status})' if status else ''}: {exc}"
            ),
            ephemeral=True,
        )

    except Exception as exc:
        try:
            await db.disable_sheet(interaction.guild.id)
        except Exception:
            pass

        logger.exception("/setsheet fehlgeschlagen")
        await interaction.followup.send(
            (
                f"❌ Could not connect Google Sheet: {exc}"
                if lang == "en"
                else f"❌ Google Sheet konnte nicht verbunden werden: {exc}"
            ),
            ephemeral=True,
        )


@bot.tree.command(
    description="Deaktiviert den Google-Sheets-Sync für diesen Server."
)
@app_commands.check(is_ladder_admin_check)
async def disablesheet(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server.",
            ephemeral=True,
        )
        return

    lang = await db.get_guild_language(interaction.guild.id)

    await db.ensure_guild(interaction.guild)
    await db.disable_sheet(interaction.guild.id)

    await interaction.response.send_message(
        (
            "Google Sheets sync has been disabled."
            if lang == "en"
            else "Google-Sheets-Sync wurde deaktiviert."
        ),
        ephemeral=True,
    )


@bot.tree.command(
    description="Aktualisiert Riot-Daten dieses Servers manuell."
)
@app_commands.check(is_ladder_admin_check)
async def refresh(interaction: discord.Interaction):
    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )

    if interaction.guild is None:
        await interaction.followup.send(
            "This command only works on a Discord server. / Dieser Command funktioniert nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    updated, failed = await refresh_unique_riot_accounts(
        interaction.guild.id
    )

    if interaction.guild is not None:
        await db.ensure_guild(interaction.guild)
        await post_or_edit_leaderboard(interaction.guild.id)

        try:
            await sync_google_sheet_for_guild(interaction.guild.id)
        except Exception:
            logger.exception(
                "Sheet-Sync nach /refresh fehlgeschlagen."
            )

    await interaction.followup.send(
        f"Aktualisiert: **{updated}** eindeutige Riot Accounts. "
        f"Fehler: **{failed}**.",
        ephemeral=True,
    )


@bot.tree.command(
    description="Setzt die Ladder dieses Servers für einen neuen Split zurück."
)
@app_commands.check(is_ladder_admin_check)
async def resetladder(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server. / Dieser Command funktioniert nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )

    try:
        await db.ensure_guild(interaction.guild)
        await db.reset_ladder(interaction.guild.id)
        await post_or_edit_leaderboard(interaction.guild.id)

        await interaction.followup.send(
            "Die Ladder dieses Servers wurde zurückgesetzt. "
            "Ein höherer New Peak wurde als neuer All Time Peak übernommen; "
            "die neue Ladder startet danach bei maximal 0 Delta.",
            ephemeral=True,
        )

        try:
            await sync_google_sheet_for_guild(interaction.guild.id)
        except Exception:
            logger.exception(
                "Sheet-Sync nach /resetladder fehlgeschlagen."
            )

    except Exception:
        logger.exception("/resetladder fehlgeschlagen")
        await interaction.followup.send(
            "Beim Zurücksetzen der Ladder ist ein Fehler aufgetreten.",
            ephemeral=True,
        )


@bot.tree.command(
    name="fixalltimepeak",
    description="Stellt All Time Peaks dieses Servers aus dem Ladder Start her."
)
@app_commands.check(is_ladder_admin_check)
async def fixalltimepeak(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a Discord server. / Dieser Command funktioniert nur auf einem Discord-Server.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )

    try:
        await db.ensure_guild(interaction.guild)
        await db.restore_alltime_peak_from_ladder_start(
            interaction.guild.id
        )
        await post_or_edit_leaderboard(interaction.guild.id)

        await interaction.followup.send(
            "All Time Peaks dieses Servers wurden aus dem gespeicherten "
            "Ladder Start wiederhergestellt.",
            ephemeral=True,
        )

        try:
            await sync_google_sheet_for_guild(interaction.guild.id)
        except Exception:
            logger.exception(
                "Sheet-Sync nach /fixalltimepeak fehlgeschlagen."
            )

    except Exception:
        logger.exception("/fixalltimepeak fehlgeschlagen")
        await interaction.followup.send(
            "Fehler beim Wiederherstellen der All Time Peaks.",
            ephemeral=True,
        )


@bot.tree.command(
    description="Tritt einer Ladder über ihre ID bei."
)
@app_commands.describe(ladder_id="Ladder-ID aus dem Admin-Panel / der Ankündigung")
async def joinladder(
    interaction: discord.Interaction,
    ladder_id: int,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works on a server.",
            ephemeral=True,
        )
        return

    lang = await db.get_guild_language(interaction.guild.id)

    ladder = await db.get_ladder(interaction.guild.id, ladder_id)
    if not ladder:
        await interaction.response.send_message(
            "Ladder not found." if lang == "en" else "Ladder nicht gefunden.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(JoinRiotModal(ladder_id, lang))


@bot.tree.command(
    description="Zeigt dein LadderGG-Spielerprofil."
)
@app_commands.describe(
    ladder_id="Optional: eine bestimmte Ladder-ID",
    user="Optional: ein anderer Discord-Nutzer",
)
async def profile(
    interaction: discord.Interaction,
    ladder_id: Optional[int] = None,
    user: Optional[discord.Member] = None,
):
    target = user or interaction.user
    lang = await db.get_guild_language(interaction.guild.id)

    ladders = await db.list_ladders(
        interaction.guild.id,
        include_archived=True,
    )
    if ladder_id is not None:
        ladders = [
            l for l in ladders
            if int(l["ladder_id"]) == ladder_id
        ]

    matches = []
    for ladder in ladders:
        rows = await db.players_for_ladder(
            interaction.guild.id,
            int(ladder["ladder_id"]),
            approved_only=False,
        )
        for row in rows:
            if int(row["discord_user_id"]) == target.id:
                matches.append((ladder, row))

    if not matches:
        await interaction.response.send_message(
            (
                "No LadderGG data found for this user."
                if lang == "en"
                else "Für diesen Nutzer wurden keine LadderGG-Daten gefunden."
            ),
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=f"👤 LadderGG Profil · {target.display_name}",
    )

    for ladder, row in matches[:6]:
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        games = wins + losses
        wr = (wins / games * 100) if games else 0
        badges = " ".join(achievement_badges(row)) or "—"

        if ladder["ladder_type"] == "challenge":
            score = score_label(
                calculate_challenge_score(
                    row,
                    parse_rules_jsonb(ladder["rules"]),
                )
            )
        else:
            score = row["current_label"] or "Unranked"

        embed.add_field(
            name=(
                f"{'🔥' if ladder['ladder_type']=='challenge' else '🏆'} "
                f"{ladder['name']}"
            ),
            value=(
                f"**Wertung:** {score}\n"
                f"**Current:** {row['current_label'] or 'Unranked'}\n"
                f"**Peak:** {row['new_peak_label'] or '—'}\n"
                f"**W/L:** {wins}/{losses} · {wr:.1f}%\n"
                f"**Achievements:** {badges}"
            ),
            inline=False,
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    description="Vergleicht zwei Teilnehmer einer Ladder."
)
@app_commands.describe(
    ladder_id="Ladder-ID",
    player1="Erster Spieler",
    player2="Zweiter Spieler",
)
async def compare(
    interaction: discord.Interaction,
    ladder_id: int,
    player1: discord.Member,
    player2: discord.Member,
):
    lang = await db.get_guild_language(interaction.guild.id)

    ladder = await db.get_ladder(
        interaction.guild.id,
        ladder_id,
    )
    if not ladder:
        await interaction.response.send_message(
            "Ladder not found." if lang == "en" else "Ladder nicht gefunden.",
            ephemeral=True,
        )
        return

    rows = await db.players_for_ladder(
        interaction.guild.id,
        ladder_id,
        approved_only=False,
    )
    by_id = {
        int(r["discord_user_id"]): r
        for r in rows
    }
    a = by_id.get(player1.id)
    b = by_id.get(player2.id)

    if not a or not b:
        await interaction.response.send_message(
            (
                "Both players must be registered in this ladder."
                if lang == "en"
                else "Beide Spieler müssen in dieser Ladder registriert sein."
            ),
            ephemeral=True,
        )
        return

    def compare_value(row):
        if ladder["ladder_type"] == "challenge":
            return score_label(
                calculate_challenge_score(
                    row,
                    parse_rules_jsonb(ladder["rules"]),
                )
            )
        return row["current_label"] or "Unranked"

    embed = discord.Embed(
        title=f"⚔️ {player1.display_name} vs {player2.display_name}",
        description=f"**{ladder['name']}**",
    )
    for member, row in ((player1, a), (player2, b)):
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        games = wins + losses
        wr = (wins / games * 100) if games else 0
        embed.add_field(
            name=member.display_name,
            value=(
                f"**Wertung:** {compare_value(row)}\n"
                f"**Current:** {row['current_label'] or 'Unranked'}\n"
                f"**Peak:** {row['new_peak_label'] or '—'}\n"
                f"**W/L:** {wins}/{losses}\n"
                f"**Winrate:** {wr:.1f}%"
            ),
            inline=True,
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    description="Zeigt Statistiken einer Ladder."
)
@app_commands.describe(ladder_id="Ladder-ID")
async def ladderstats(
    interaction: discord.Interaction,
    ladder_id: int,
):
    lang = await db.get_guild_language(interaction.guild.id)

    ladder = await db.get_ladder(
        interaction.guild.id,
        ladder_id,
    )
    if not ladder:
        await interaction.response.send_message(
            "Ladder not found." if lang == "en" else "Ladder nicht gefunden.",
            ephemeral=True,
        )
        return

    rows = await db.players_for_ladder(
        interaction.guild.id,
        ladder_id,
    )
    if not rows:
        await interaction.response.send_message(
            "Noch keine Teilnehmer.",
            ephemeral=True,
        )
        return

    total_wins = sum(int(r["wins"] or 0) for r in rows)
    total_losses = sum(int(r["losses"] or 0) for r in rows)
    total_games = total_wins + total_losses
    biggest_climb = max(
        rows,
        key=lambda r: int(r["new_peak_lp"] or 0) - int(r["ladder_start_lp"] or 0),
    )
    highest_peak = max(
        rows,
        key=lambda r: int(r["new_peak_lp"] or 0),
    )

    best_wr = max(
        rows,
        key=lambda r: (
            int(r["wins"] or 0)
            / max(1, int(r["wins"] or 0) + int(r["losses"] or 0))
        ),
    )

    embed = discord.Embed(
        title=f"📊 {ladder['name']} · Statistiken",
    )
    embed.add_field(
        name="Teilnehmer",
        value=str(len(rows)),
        inline=True,
    )
    embed.add_field(
        name="Games gesamt",
        value=str(total_games),
        inline=True,
    )
    embed.add_field(
        name="Größter Climb",
        value=(
            f"**{biggest_climb['display_name']}** · "
            f"+{int(biggest_climb['new_peak_lp']) - int(biggest_climb['ladder_start_lp'])} LP"
        ),
        inline=False,
    )
    embed.add_field(
        name="Höchster Peak",
        value=(
            f"**{highest_peak['display_name']}** · "
            f"{highest_peak['new_peak_label']}"
        ),
        inline=False,
    )
    wr_games = int(best_wr["wins"] or 0) + int(best_wr["losses"] or 0)
    wr = int(best_wr["wins"] or 0) / max(1, wr_games) * 100
    embed.add_field(
        name="Beste Winrate",
        value=f"**{best_wr['display_name']}** · {wr:.1f}%",
        inline=False,
    )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    description="Zeigt die Hall of Fame archivierter Ladders."
)
async def halloffame(interaction: discord.Interaction):
    rows = await db.hall_of_fame_rows(
        interaction.guild.id,
        limit=20,
    )
    if not rows:
        await interaction.response.send_message(
            "Die Hall of Fame ist noch leer.",
            ephemeral=True,
        )
        return

    lines = []
    for index, row in enumerate(rows[:10], start=1):
        gain = int(row["new_peak_lp"] or 0) - int(row["ladder_start_lp"] or 0)
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"{index}.")
        lines.append(
            f"{medal} **{row['display_name']}** · "
            f"+{gain} LP · {row['new_peak_label']} · "
            f"*{row['ladder_name']}*"
        )

    embed = discord.Embed(
        title="🏛️ LadderGG Hall of Fame",
        description="\n".join(lines),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    description="Zeigt die letzten getrackten SoloQ-Games."
)
@app_commands.describe(
    user="Spieler",
    limit="Anzahl der Games (1-10)",
)
async def matches(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
    limit: int = 5,
):
    target = user or interaction.user
    limit = max(1, min(limit, 10))
    player = None
    lang = await db.get_guild_language(interaction.guild.id)

    for ladder in await db.list_ladders(
        interaction.guild.id,
        include_archived=False,
    ):
        for row in await db.players_for_ladder(
            interaction.guild.id,
            int(ladder["ladder_id"]),
            approved_only=False,
        ):
            if int(row["discord_user_id"]) == target.id:
                player = row
                break
        if player:
            break

    if not player:
        await interaction.response.send_message(
            t(lang, "no_riot"),
            ephemeral=True,
        )
        return

    rows = await db.recent_matches(
        player["puuid"],
        player["platform"],
        limit,
    )

    if not rows:
        await interaction.response.send_message(
            t(lang, "no_matches"),
            ephemeral=True,
        )
        return

    # Wichtig: EIN Embed für alle gewählten Matches.
    # Der gruppierte /account matches Command ruft exakt diesen Callback auf.
    embed = compact_match_history_embed(
        target.display_name,
        rows,
        lang,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


# ------------------------------------------------------------------
# LadderGG grouped command system
# Inspired by the clean command grouping used by mature Discord bots,
# while keeping ladders/challenges as the product's primary surface.
# ------------------------------------------------------------------

account_group = app_commands.Group(
    name="account",
    description="Manage your Riot account and player profile.",
)

ladder_group = app_commands.Group(
    name="ladder",
    description="Ladders, challenges, standings and statistics.",
)

config_group = app_commands.Group(
    name="config",
    description="Configure LadderGG for this server.",
)

admin_group = app_commands.Group(
    name="admin",
    description="Administrative LadderGG tools.",
)


class PublicLeaderboardPager(discord.ui.View):
    def __init__(
        self,
        embeds: list[discord.Embed],
        ladder_id: Optional[int] = None,
        lang: str = "de",
    ):
        super().__init__(timeout=900)
        self.embeds = embeds
        self.index = 0
        self.ladder_id = ladder_id
        self.lang = lang
        self._sync_buttons()

    def _sync_buttons(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == "laddergg_prev":
                    item.disabled = self.index <= 0
                elif item.custom_id == "laddergg_next":
                    item.disabled = self.index >= len(self.embeds) - 1
                elif item.custom_id == "laddergg_page":
                    item.label = f"{self.index + 1} / {len(self.embeds)}"
                    item.disabled = True

    @discord.ui.button(
        label="Previous",
        style=discord.ButtonStyle.secondary,
        emoji="◀️",
        custom_id="laddergg_prev",
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if self.index > 0:
            self.index -= 1
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.index],
            view=self,
        )

    @discord.ui.button(
        label="1 / 1",
        style=discord.ButtonStyle.secondary,
        custom_id="laddergg_page",
        disabled=True,
    )
    async def page_indicator(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        pass

    @discord.ui.button(
        label="Next",
        style=discord.ButtonStyle.secondary,
        emoji="▶️",
        custom_id="laddergg_next",
    )
    async def next(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if self.index < len(self.embeds) - 1:
            self.index += 1
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=self.embeds[self.index],
            view=self,
        )

    @discord.ui.button(
        label="My Stats",
        style=discord.ButtonStyle.primary,
        emoji="👤",
        custom_id="laddergg_my_stats",
    )
    async def my_stats(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This button only works on a server.",
                ephemeral=True,
            )
            return

        # Prefer this ladder if available.
        player = None
        if self.ladder_id is not None:
            rows = await db.players_for_ladder(
                interaction.guild.id,
                int(self.ladder_id),
                approved_only=False,
            )
            player = next(
                (
                    r for r in rows
                    if int(r["discord_user_id"]) == interaction.user.id
                ),
                None,
            )

        if not player:
            for ladder in await db.list_ladders(
                interaction.guild.id,
                include_archived=False,
            ):
                rows = await db.players_for_ladder(
                    interaction.guild.id,
                    int(ladder["ladder_id"]),
                    approved_only=False,
                )
                player = next(
                    (
                        r for r in rows
                        if int(r["discord_user_id"]) == interaction.user.id
                    ),
                    None,
                )
                if player:
                    break

        if not player:
            await interaction.response.send_message(
                (
                    "You are not registered in this ladder."
                    if self.lang == "en"
                    else "Du bist in dieser Ladder nicht registriert."
                ),
                ephemeral=True,
            )
            return

        wins = int(player["wins"] or 0)
        losses = int(player["losses"] or 0)
        games = wins + losses
        wr = wins / games * 100 if games else 0.0

        embed = discord.Embed(
            title=f"👤 {player['display_name']}",
            description=f"**{player['current_label'] or 'Unranked'}**",
        )
        embed.add_field(
            name="Record",
            value=f"{wins}W · {losses}L · `{wr:.1f}% WR`",
            inline=False,
        )
        embed.add_field(
            name=("Challenge Peak" if self.lang == "en" else "Challenge Peak"),
            value=player["new_peak_label"] or "—",
            inline=True,
        )
        embed.add_field(
            name=("Ladder Start" if self.lang == "en" else "Ladder Start"),
            value=player["ladder_start_label"] or "—",
            inline=True,
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )



class LadderCommandSelect(discord.ui.Select):
    def __init__(self, ladders, action: str, lang: str = "de"):
        self.action = action
        self.lang = lang
        options = [
            discord.SelectOption(
                label=(
                    f"{'🔥' if l['ladder_type'] == 'challenge' else '🏆'} "
                    f"{l['name']}"
                )[:100],
                value=str(l["ladder_id"]),
                description=(
                    f"{l['ladder_type'].title()} · {l['status']} · "
                    f"ID {l['ladder_id']}"
                )[:100],
            )
            for l in ladders[:25]
        ]
        super().__init__(
            placeholder=t(lang, "choose_ladder"),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        ladder_id = int(self.values[0])
        ladder = await db.get_ladder(interaction.guild.id, ladder_id)

        if not ladder:
            await interaction.response.send_message(
                (
                    "Ladder not found."
                    if self.lang == "en"
                    else "Ladder nicht gefunden."
                ),
                ephemeral=True,
            )
            return

        if self.action == "view":
            rows = await db.players_for_ladder(
                interaction.guild.id,
                ladder_id,
            )
            lang = await db.get_guild_language(interaction.guild.id)

            embeds = build_leaderboard_embeds(
                rows,
                ladder["name"],
                await db.latest_refresh_for_guild(interaction.guild.id),
                ladder["ladder_type"],
                parse_rules_jsonb(ladder["rules"]),

                lang=lang,

            )
            view = (
                PublicLeaderboardPager(embeds)
                if len(embeds) > 1
                else None
            )
            await interaction.response.send_message(
                embed=embeds[0],
                view=view,
            )
            return

        if self.action == "join":
            lang = await db.get_guild_language(interaction.guild.id)
            await interaction.response.send_modal(
                JoinRiotModal(ladder_id, lang)
            )
            return

        if self.action == "stats":
            # Reuse the stable existing command callback.
            await ladderstats.callback(interaction, ladder_id)
            return

        await interaction.response.send_message(
            ladder_summary(ladder),
            ephemeral=True,
        )


class LadderCommandSelectView(discord.ui.View):
    def __init__(self, ladders, action: str, lang: str = "de"):
        super().__init__(timeout=300)
        self.add_item(LadderCommandSelect(ladders, action, lang))


@account_group.command(
    name="link",
    description="Link your Riot account to a LadderGG ladder.",
)
async def account_link(
    interaction: discord.Interaction,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command only works in a server.",
            ephemeral=True,
        )
        return

    lang = await db.get_guild_language(interaction.guild.id)

    ladders = await db.list_ladders(
        interaction.guild.id,
        include_archived=False,
    )
    active = [
        l for l in ladders
        if l["status"] in {"active", "scheduled", "paused"}
    ]

    if not active:
        await interaction.response.send_message(
            (
                "Es gibt noch keine aktive Ladder. Bitte einen Admin, "
                "eine mit `/config setup` zu erstellen."
                if lang == "de"
                else "There is no active ladder yet. Ask an admin to create one with `/config setup`."
            ),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        (
            "Wähle die Ladder, der du beitreten möchtest:"
            if lang == "de"
            else "Choose the ladder you want to join:"
        ),
        view=LadderCommandSelectView(active, "join", lang),
        ephemeral=True,
    )


@account_group.command(
    name="profile",
    description="Show a LadderGG player profile.",
)
@app_commands.describe(
    user="Player to inspect",
    ladder_id="Optional ladder ID",
)
async def account_profile(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
    ladder_id: Optional[int] = None,
):
    await profile.callback(
        interaction,
        ladder_id,
        user,
    )


@account_group.command(
    name="matches",
    description="Show recent tracked SoloQ matches.",
)
@app_commands.describe(
    user="Player",
    limit="Number of matches (1-10)",
)
async def account_matches(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
    limit: int = 5,
):
    await matches.callback(
        interaction,
        user,
        limit,
    )


@account_group.command(
    name="stats",
    description="Show detailed tracked SoloQ statistics.",
)
@app_commands.describe(user="Player")
async def account_stats(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
):
    target = user or interaction.user
    player = None
    for ladder in await db.list_ladders(interaction.guild.id, include_archived=True):
        rows = await db.players_for_ladder(
            interaction.guild.id,
            int(ladder["ladder_id"]),
            approved_only=False,
        )
        player = next(
            (r for r in rows if int(r["discord_user_id"]) == target.id),
            None,
        )
        if player:
            break

    if not player:
        await interaction.response.send_message("No Riot account found.", ephemeral=True)
        return

    matches = await db.recent_matches(
        player["puuid"],
        player["platform"],
        50,
    )
    if not matches:
        await interaction.response.send_message("No tracked matches yet.", ephemeral=True)
        return

    champion = {}
    tk = td = ta = tcs = tdmg = tdur = 0
    wins = 0
    current_streak = 0
    streak_type = bool(matches[0]["win"])
    for m in matches:
        c = str(m["champion_name"] or "Unknown")
        champion.setdefault(c, {"games":0, "wins":0})
        champion[c]["games"] += 1
        champion[c]["wins"] += int(bool(m["win"]))
        wins += int(bool(m["win"]))
        tk += int(m["kills"] or 0)
        td += int(m["deaths"] or 0)
        ta += int(m["assists"] or 0)
        tcs += int(m["cs"] or 0)
        tdmg += int(m["damage"] or 0)
        tdur += int(m["duration_seconds"] or 0)
        if bool(m["win"]) == streak_type and current_streak == champion.get("_break", current_streak):
            current_streak += 1

    n = len(matches)
    most = max(
        ((v["games"], name, v["wins"]) for name, v in champion.items() if name != "_break"),
        default=(0, "—", 0),
    )
    minutes = max(1, tdur / 60)
    e = discord.Embed(title=f"📊 {target.display_name} · SoloQ Stats")
    e.add_field(name="Last games", value=f"**{wins}W · {n-wins}L** · `{wins/n*100:.1f}% WR`", inline=True)
    e.add_field(name="KDA", value=f"`{tk/n:.1f}/{td/n:.1f}/{ta/n:.1f}` · `{(tk+ta)/max(1,td):.2f}`", inline=True)
    e.add_field(name="CS/min", value=f"`{tcs/minutes:.2f}`", inline=True)
    e.add_field(name="Damage/min", value=f"`{tdmg/minutes:,.0f}`", inline=True)
    e.add_field(name="Most played", value=f"**{most[1]}** · {most[0]} games", inline=True)
    e.add_field(name="Current form", value=f"{current_streak} {'W' if streak_type else 'L'}", inline=True)
    await interaction.response.send_message(embed=e, ephemeral=True)


@account_group.command(
    name="history",
    description="Show a player's LadderGG history.",
)
@app_commands.describe(user="Player")
async def account_history(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
):
    target = user or interaction.user
    ladders = await db.list_ladders(interaction.guild.id, include_archived=True)
    lines = []
    podiums = 0
    wins_count = 0

    for ladder in ladders:
        rows = await db.players_for_ladder(
            interaction.guild.id,
            int(ladder["ladder_id"]),
            approved_only=False,
        )
        if ladder["ladder_type"] == "challenge":
            ordered = sorted(
                rows,
                key=lambda r: calculate_challenge_score(
                    r, parse_rules_jsonb(ladder["rules"])
                ),
                reverse=True,
            )
        else:
            ordered = sorted(
                rows,
                key=lambda r: int(r["current_lp"] or 0),
                reverse=True,
            )

        for pos, row in enumerate(ordered, start=1):
            if int(row["discord_user_id"]) != target.id:
                continue
            if pos <= 3:
                podiums += 1
            if pos == 1:
                wins_count += 1
            lines.append(
                f"{'#'+str(pos)} · **{ladder['name']}** · "
                f"{ladder['ladder_type'].title()} · {ladder['status']}"
            )
            break

    e = discord.Embed(
        title=f"📚 {target.display_name} · Ladder History",
        description="\\n".join(lines[:20]) if lines else "No ladder history.",
    )
    e.add_field(name="Wins", value=str(wins_count), inline=True)
    e.add_field(name="Podiums", value=str(podiums), inline=True)
    await interaction.response.send_message(embed=e, ephemeral=True)


@ladder_group.command(
    name="list",
    description="List all active ladders and challenges.",
)
async def ladder_list(
    interaction: discord.Interaction,
):
    ladders = await db.list_ladders(
        interaction.guild.id,
        include_archived=False,
    )
    if not ladders:
        await interaction.response.send_message(
            "No active ladders yet.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="🏆 LadderGG · Ladders",
        description="\n".join(
            ladder_summary(l)
            for l in ladders[:25]
        ),
    )
    embed.set_footer(
        text=f"{len(ladders)} active ladder(s)"
    )
    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@ladder_group.command(
    name="view",
    description="View standings for a ladder.",
)
async def ladder_view(
    interaction: discord.Interaction,
):
    await interaction.response.defer(thinking=True)

    try:
        lang = await db.get_guild_language(interaction.guild.id)

        ladders = await db.list_ladders(
            interaction.guild.id,
            include_archived=False,
        )
        active = [
            l for l in ladders
            if l["status"] != "archived"
        ]
        if not active:
            await interaction.followup.send(
                (
                    "Keine aktiven Ladders."
                    if lang == "de"
                    else "No active ladders."
                ),
                ephemeral=True,
            )
            return

        if len(active) == 1:
            ladder = active[0]
            rows = await db.players_for_ladder(
                interaction.guild.id,
                int(ladder["ladder_id"]),
            )

            embeds = build_leaderboard_embeds(
                rows,
                ladder["name"],
                await db.latest_refresh_for_guild(interaction.guild.id),
                ladder["ladder_type"],
                parse_rules_jsonb(ladder["rules"]),
                lang=lang,
            )
            await interaction.followup.send(
                embed=embeds[0],
                view=(
                    PublicLeaderboardPager(embeds)
                    if len(embeds) > 1
                    else None
                ),
            )
            return

        await interaction.followup.send(
            "Ladder auswählen:" if lang == "de" else "Choose a ladder:",
            view=LadderCommandSelectView(active, "view", lang),
            ephemeral=True,
        )
    except Exception:
        logger.exception("/ladder view fehlgeschlagen")
        await interaction.followup.send(
            "❌ Something went wrong loading this ladder.",
            ephemeral=True,
        )


@ladder_group.command(
    name="join",
    description="Join a ladder or challenge.",
)
async def ladder_join(
    interaction: discord.Interaction,
):
    lang = await db.get_guild_language(interaction.guild.id)

    ladders = await db.list_ladders(
        interaction.guild.id,
        include_archived=False,
    )
    active = [
        l for l in ladders
        if l["status"] in {"active", "scheduled"}
    ]
    if not active:
        await interaction.response.send_message(
            "Keine beitretbaren Ladders." if lang == "de" else "No joinable ladders.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "Ladder auswählen:" if lang == "de" else "Choose a ladder:",
        view=LadderCommandSelectView(active, "join", lang),
        ephemeral=True,
    )


@ladder_group.command(
    name="stats",
    description="Show detailed statistics for a ladder.",
)
async def ladder_stats_group(
    interaction: discord.Interaction,
):
    lang = await db.get_guild_language(interaction.guild.id)

    ladders = await db.list_ladders(
        interaction.guild.id,
        include_archived=False,
    )
    if not ladders:
        await interaction.response.send_message(
            "Keine Ladders verfügbar." if lang == "de" else "No ladders available.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "Ladder auswählen:" if lang == "de" else "Choose a ladder:",
        view=LadderCommandSelectView(ladders, "stats", lang),
        ephemeral=True,
    )


@ladder_group.command(
    name="compare",
    description="Compare two players inside a ladder.",
)
@app_commands.describe(
    ladder_id="Ladder ID",
    player1="First player",
    player2="Second player",
)
async def ladder_compare_group(
    interaction: discord.Interaction,
    ladder_id: int,
    player1: discord.Member,
    player2: discord.Member,
):
    await compare.callback(
        interaction,
        ladder_id,
        player1,
        player2,
    )


@ladder_group.command(
    name="halloffame",
    description="Show archived LadderGG records.",
)
async def ladder_halloffame(
    interaction: discord.Interaction,
):
    await halloffame.callback(interaction)


@config_group.command(
    name="setup",
    description="Open the private LadderGG admin panel.",
)
@app_commands.check(is_ladder_admin_check)
async def config_setup(
    interaction: discord.Interaction,
):
    await setup.callback(interaction)


@config_group.command(name="language", description="Set server language / Serversprache festlegen.")
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(language="Language / Sprache")
@app_commands.choices(language=[
    app_commands.Choice(name="Deutsch", value="de"),
    app_commands.Choice(name="English", value="en"),
])
async def config_language(interaction: discord.Interaction, language: app_commands.Choice[str]):
    await db.set_guild_language(interaction.guild.id, language.value)
    await interaction.response.send_message(
        f"✅ {t(language.value, 'server_lang_en' if language.value == 'en' else 'server_lang_de')}",
        ephemeral=True,
    )


@config_group.command(
    name="tracking",
    description="Configure match alerts, streaks and daily summaries.",
)
@app_commands.check(is_ladder_admin_check)
async def config_tracking(
    interaction: discord.Interaction,
):
    lang = await db.get_guild_language(interaction.guild.id)
    await interaction.response.send_modal(
        TrackingSettingsModal(interaction.guild.id, lang)
    )


@config_group.command(
    name="sheet",
    description="Connect a Google Sheet to this server.",
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(
    sheet_id="Google Sheet ID",
    worksheet_name="Worksheet name",
)
async def config_sheet(
    interaction: discord.Interaction,
    sheet_id: str,
    worksheet_name: str = "Leaderboard",
):
    await setsheet.callback(
        interaction,
        sheet_id,
        worksheet_name,
    )


@admin_group.command(
    name="refresh",
    description="Refresh Riot data now.",
)
@app_commands.check(is_ladder_admin_check)
async def admin_refresh(
    interaction: discord.Interaction,
):
    await refresh.callback(interaction)


@admin_group.command(
    name="archive",
    description="Archive a ladder and write its final snapshot.",
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(ladder_id="Ladder ID")
async def admin_archive(
    interaction: discord.Interaction,
    ladder_id: int,
):
    await archiveladder.callback(
        interaction,
        ladder_id,
    )


@admin_group.command(
    name="split",
    description="Archive a ladder and create the next split template.",
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(
    ladder_id="Ladder ID",
    name="Name of the new split",
)
async def admin_split(
    interaction: discord.Interaction,
    ladder_id: int,
    name: str,
):
    await newsplit.callback(
        interaction,
        ladder_id,
        name,
    )


@admin_group.command(
    name="duplicate",
    description="Duplicate a ladder as a template.",
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(
    ladder_id="Ladder ID",
    name="Name of the copy",
)
async def admin_duplicate(
    interaction: discord.Interaction,
    ladder_id: int,
    name: str,
):
    await duplicateladder.callback(
        interaction,
        ladder_id,
        name,
    )


bot.tree.add_command(account_group)
bot.tree.add_command(ladder_group)
bot.tree.add_command(config_group)
bot.tree.add_command(admin_group)






@ladder_group.command(
    name="history",
    description="Show archived ladder history.",
)
async def ladder_history(
    interaction: discord.Interaction,
):
    ladders = await db.list_ladders(
        interaction.guild.id,
        include_archived=True,
    )
    archived = [
        l for l in ladders
        if l["status"] == "archived"
    ]
    if not archived:
        await interaction.response.send_message(
            "No archived ladders yet.",
            ephemeral=True,
        )
        return

    lines = []
    for ladder in archived[:20]:
        archived_at = ladder["archived_at"]
        when = (
            f"<t:{int(archived_at.timestamp())}:d>"
            if archived_at
            else "—"
        )
        lines.append(
            f"{'🔥' if ladder['ladder_type']=='challenge' else '🏆'} "
            f"**{ladder['name']}** · {when} · ID `{ladder['ladder_id']}`"
        )

    await interaction.response.send_message(
        embed=discord.Embed(
            title="📚 LadderGG History",
            description="\n".join(lines),
        )
    )



@ladder_group.command(
    name="period",
    description="Show LP movement for today or this week.",
)
@app_commands.describe(
    ladder_id="Ladder ID",
    period="Today or this week",
)
@app_commands.choices(
    period=[
        app_commands.Choice(name="Today", value="today"),
        app_commands.Choice(name="This week", value="week"),
    ]
)
async def ladder_period(
    interaction: discord.Interaction,
    ladder_id: int,
    period: app_commands.Choice[str],
):
    ladder = await db.get_ladder(interaction.guild.id, ladder_id)
    if not ladder:
        await interaction.response.send_message(
            "Ladder not found.",
            ephemeral=True,
        )
        return

    rows = await db.players_for_ladder(
        interaction.guild.id,
        ladder_id,
    )
    now = datetime.now(timezone.utc)
    if period.value == "today":
        target_date = now.date()
    else:
        target_date = (now - timedelta(days=7)).date()

    ranking = []
    for row in rows:
        snapshot = await db.snapshot_for_player_on_or_before(
            row["puuid"],
            row["platform"],
            target_date,
        )
        if not snapshot or snapshot["current_lp"] is None:
            continue
        current_lp = int(row["current_lp"] or 0)
        delta = current_lp - int(snapshot["current_lp"])
        ranking.append((delta, row))

    ranking.sort(key=lambda x: x[0], reverse=True)
    lang = await db.get_guild_language(interaction.guild.id)

    lines = []
    for idx, (delta, row) in enumerate(ranking[:20], start=1):
        medal = {1:"🥇", 2:"🥈", 3:"🥉"}.get(idx, f"`#{idx}`")
        lines.append(
            f"{medal} **{row['display_name']}** · `{delta:+d} LP` · "
            f"{row['current_label'] or 'Unranked'}"
        )

    title_period = (
        t(lang, "today")
        if period.value == "today"
        else t(lang, "this_week")
    )
    await interaction.response.send_message(
        embed=discord.Embed(
            title=f"📈 {ladder['name']} · {title_period}",
            description="\\n".join(lines) if lines else "No snapshot data yet.",
        )
    )


@ladder_group.command(
    name="registration",
    description="Show the registration status of a ladder.",
)
@app_commands.describe(ladder_id="Ladder ID")
async def ladder_registration(
    interaction: discord.Interaction,
    ladder_id: int,
):
    ladder = await db.get_ladder(interaction.guild.id, ladder_id)
    if not ladder:
        await interaction.response.send_message("Ladder not found.", ephemeral=True)
        return
    rules = parse_rules_jsonb(ladder["rules"])
    competition = feature_rules(rules).get("competition") or {}
    lang = await db.get_guild_language(interaction.guild.id)
    mode = competition.get("registration_mode", "public")
    mode_text = {
        "public": t(lang, "public_join"),
        "role": t(lang, "role_only"),
        "invite": t(lang, "invite_only"),
    }.get(mode, mode)
    open_text = (
        t(lang, "registration_open")
        if competition.get("registration_open", True)
        else t(lang, "registration_closed")
    )
    await interaction.response.send_message(
        embed=discord.Embed(
            title=f"📝 {ladder['name']}",
            description=f"**{open_text}**\\n{mode_text}",
        ),
        ephemeral=True,
    )


@ladder_group.command(
    name="invite-add",
    description="Invite a player to an invite-only ladder.",
)
@app_commands.describe(ladder_id="Ladder ID", user="Player")
async def ladder_invite_add(
    interaction: discord.Interaction,
    ladder_id: int,
    user: discord.Member,
):
    if not await require_ladder_admin(interaction):
        return
    await db.add_ladder_invite(
        interaction.guild.id,
        ladder_id,
        user.id,
        interaction.user.id,
    )
    await interaction.response.send_message(
        f"✅ {user.mention} invited.",
        ephemeral=True,
    )


@ladder_group.command(
    name="invite-remove",
    description="Remove a ladder invitation.",
)
@app_commands.describe(ladder_id="Ladder ID", user="Player")
async def ladder_invite_remove(
    interaction: discord.Interaction,
    ladder_id: int,
    user: discord.Member,
):
    if not await require_ladder_admin(interaction):
        return
    await db.remove_ladder_invite(
        interaction.guild.id,
        ladder_id,
        user.id,
    )
    await interaction.response.send_message("✅ Invite removed.", ephemeral=True)


@ladder_group.command(
    name="invites",
    description="List invitations for a ladder.",
)
@app_commands.describe(ladder_id="Ladder ID")
async def ladder_invites_command(
    interaction: discord.Interaction,
    ladder_id: int,
):
    if not await require_ladder_admin(interaction):
        return
    rows = await db.ladder_invites(interaction.guild.id, ladder_id)
    lines = [f"<@{r['discord_user_id']}>" for r in rows]
    await interaction.response.send_message(
        "\\n".join(lines) if lines else "No invites.",
        ephemeral=True,
    )






@config_group.command(
    name="admin-role",
    description="Set the Ladder Admin role.",
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(role="Role allowed to manage LadderGG")
async def config_admin_role(
    interaction: discord.Interaction,
    role: Optional[discord.Role] = None,
):
    await db.set_ladder_admin_role(
        interaction.guild.id,
        role.id if role else None,
    )
    await interaction.response.send_message(
        f"✅ Ladder Admin role: {role.mention if role else 'disabled'}",
        ephemeral=True,
    )


@admin_group.command(
    name="registration",
    description="Open or close registration for a ladder.",
)
@app_commands.describe(ladder_id="Ladder ID", open="Registration open?")
async def admin_registration(
    interaction: discord.Interaction,
    ladder_id: int,
    open: bool,
):
    if not await require_ladder_admin(interaction):
        return
    ladder = await db.get_ladder(interaction.guild.id, ladder_id)
    if not ladder:
        await interaction.response.send_message("Ladder not found.", ephemeral=True)
        return
    rules = parse_rules_jsonb(ladder["rules"])
    feature_rules(rules).setdefault("competition", {})["registration_open"] = open
    await db.update_ladder_rules(interaction.guild.id, ladder_id, rules)
    await interaction.response.send_message(
        f"✅ Registration {'opened' if open else 'closed'}.",
        ephemeral=True,
    )


@admin_group.command(
    name="delete",
    description="Permanently delete a ladder.",
)
@app_commands.describe(
    ladder_id="Ladder ID",
    confirmation="Type DELETE to confirm",
)
async def admin_delete(
    interaction: discord.Interaction,
    ladder_id: int,
    confirmation: str,
):
    if not await require_ladder_admin(interaction):
        return
    if confirmation.strip().upper() != "DELETE":
        await interaction.response.send_message(
            "❌ Type `DELETE` to confirm.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        # Vor dem DB-Löschen die zugehörige(n) Leaderboard-Nachricht(en)
        # merken und aus Discord entfernen - sonst bleibt eine "Geister"-
        # Nachricht im Channel stehen, die nie mehr aktualisiert wird.
        ladder = await db.get_ladder(interaction.guild.id, ladder_id)
        message_ids = await db.get_ladder_message_ids(
            interaction.guild.id,
            ladder_id,
        )

        await db.delete_ladder(interaction.guild.id, ladder_id)

        deleted_messages = 0
        channel_id = ladder["leaderboard_channel_id"] if ladder else None
        if channel_id and message_ids:
            channel = interaction.guild.get_channel(int(channel_id))
            if channel is not None:
                for message_id in message_ids:
                    try:
                        message = await channel.fetch_message(message_id)
                        await message.delete()
                        deleted_messages += 1
                    except discord.NotFound:
                        pass
                    except discord.Forbidden:
                        logger.warning(
                            "Keine Berechtigung, Leaderboard-Nachricht %s "
                            "in Channel %s zu löschen.",
                            message_id,
                            channel_id,
                        )
                    except Exception:
                        logger.exception(
                            "Leaderboard-Nachricht %s konnte nicht "
                            "gelöscht werden.",
                            message_id,
                        )

        note = (
            f" ({deleted_messages} Discord message(s) also deleted.)"
            if deleted_messages
            else ""
        )
        await interaction.followup.send(
            f"✅ Ladder permanently deleted.{note}",
            ephemeral=True,
        )
    except Exception:
        logger.exception("/admin delete fehlgeschlagen")
        await interaction.followup.send(
            "❌ Something went wrong while deleting this ladder.",
            ephemeral=True,
        )


@admin_group.command(
    name="template-delete",
    description="Delete a saved ladder template.",
)
@app_commands.describe(template_id="Template ID")
async def admin_template_delete(
    interaction: discord.Interaction,
    template_id: int,
):
    if not await require_ladder_admin(interaction):
        return
    await db.delete_template(interaction.guild.id, template_id)
    await interaction.response.send_message("✅ Template deleted.", ephemeral=True)


@admin_group.command(
    name="template-rename",
    description="Rename a saved ladder template.",
)
@app_commands.describe(template_id="Template ID", name="New name")
async def admin_template_rename(
    interaction: discord.Interaction,
    template_id: int,
    name: str,
):
    if not await require_ladder_admin(interaction):
        return
    await db.rename_template(interaction.guild.id, template_id, name.strip())
    await interaction.response.send_message("✅ Template renamed.", ephemeral=True)


@admin_group.command(
    name="template-preview",
    description="Preview a ladder template.",
)
@app_commands.describe(template_id="Template ID")
async def admin_template_preview(
    interaction: discord.Interaction,
    template_id: int,
):
    if not await require_ladder_admin(interaction):
        return
    tpl = await db.get_template(interaction.guild.id, template_id)
    if not tpl:
        await interaction.response.send_message("Template not found.", ephemeral=True)
        return
    rules = parse_rules_jsonb(tpl["rules"])
    e = discord.Embed(
        title=f"🧩 {tpl['name']}",
        description=f"Type: **{tpl['ladder_type']}**",
    )
    if tpl["ladder_type"] == "challenge":
        e.add_field(
            name="Scoring",
            value=pretty_scoring_mode((rules.get("scoring") or {}).get("mode", "peak_gain")),
            inline=True,
        )
        comp = feature_rules(rules).get("competition") or {}
        e.add_field(name="Registration", value=str(comp.get("registration_mode", "public")).title(), inline=True)
    await interaction.response.send_message(embed=e, ephemeral=True)


@admin_group.command(
    name="audit",
    description="Show detailed ladder audit entries.",
)
@app_commands.describe(ladder_id="Optional ladder ID", limit="1-20")
async def admin_audit(
    interaction: discord.Interaction,
    ladder_id: Optional[int] = None,
    limit: int = 10,
):
    if not await require_ladder_admin(interaction):
        return
    rows = await db.audit_rows(
        interaction.guild.id,
        ladder_id,
        max(1, min(limit, 20)),
    )
    lines = []
    for row in rows:
        details = row["details"]
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except Exception:
                details = {"raw": details}
        details_text = ", ".join(
            f"{k}={v}" for k, v in (details or {}).items()
        )[:250]
        actor = f"<@{row['actor_user_id']}>" if row["actor_user_id"] else "System"
        lines.append(
            f"<t:{int(row['created_at'].timestamp())}:R> · "
            f"**{row['action']}** · {actor}"
            + (f" · `{details_text}`" if details_text else "")
        )
    await interaction.response.send_message(
        embed=discord.Embed(
            title="📜 LadderGG Audit",
            description="\\n".join(lines) if lines else "No entries.",
        ),
        ephemeral=True,
    )


@admin_group.command(
    name="health",
    description="Check LadderGG services and background tasks.",
)
async def admin_health(
    interaction: discord.Interaction,
):
    if not await require_ladder_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    checks = []

    try:
        await db._pool().fetchval("SELECT 1")
        checks.append(("Database", True, "PostgreSQL"))
    except Exception as exc:
        checks.append(("Database", False, str(exc)[:100]))

    try:
        version = await db.get_schema_version()
        checks.append(("Schema", version == "9", f"v{version}"))
    except Exception as exc:
        checks.append(("Schema", False, str(exc)[:100]))

    checks.append((
        "Periodic Update",
        periodic_update.is_running(),
        f"every {UPDATE_MINUTES} min",
    ))
    checks.append((
        "Riot API",
        bool(RIOT_API_KEY),
        "API key loaded" if RIOT_API_KEY else "missing API key",
    ))

    try:
        config = await db.get_guild_config(interaction.guild.id)
        sheet_ok = bool(config and config["sheets_enabled"] and config["google_sheet_id"])
        checks.append((
            "Google Sheets",
            True,
            "connected" if sheet_ok else "not configured",
        ))
    except Exception as exc:
        checks.append(("Google Sheets", False, str(exc)[:100]))

    desc = "\n".join(
        f"{'✅' if ok else '❌'} **{name}** · {detail}"
        for name, ok, detail in checks
    )
    await interaction.followup.send(
        embed=discord.Embed(title="🩺 LadderGG Health", description=desc),
        ephemeral=True,
    )


@admin_group.command(
    name="diagnostics",
    description="Show server-specific LadderGG diagnostics.",
)
async def admin_diagnostics(
    interaction: discord.Interaction,
):
    if not await require_ladder_admin(interaction):
        return
    config = await db.get_guild_config(interaction.guild.id)
    counts = await db.dashboard_counts(interaction.guild.id)
    version = await db.get_schema_version()
    e = discord.Embed(title="🔧 LadderGG Diagnostics")
    e.add_field(name="Guild", value=f"{interaction.guild.name}\\n`{interaction.guild.id}`", inline=True)
    e.add_field(name="Schema", value=f"v{version}", inline=True)
    e.add_field(name="Language", value=str(config["language"] if config else "de"), inline=True)
    e.add_field(
        name="Ladders",
        value=(
            f"Active {counts['active']} · Scheduled {counts['scheduled']} · "
            f"Paused {counts['paused']} · Archived {counts['archived']}"
        ),
        inline=False,
    )
    e.add_field(
        name="Background",
        value=f"Periodic running: `{periodic_update.is_running()}`",
        inline=False,
    )
    await interaction.response.send_message(embed=e, ephemeral=True)


@admin_group.command(
    name="export",
    description="Export LadderGG server configuration as JSON.",
)
async def admin_export(
    interaction: discord.Interaction,
):
    if not await require_ladder_admin(interaction):
        return
    ladders = await db.list_ladders(interaction.guild.id, include_archived=True)
    config = await db.get_guild_config(interaction.guild.id)
    payload = {
        "guild_id": interaction.guild.id,
        "guild_name": interaction.guild.name,
        "language": config["language"] if config else "de",
        "schema_version": await db.get_schema_version(),
        "ladders": [
            {
                "ladder_id": int(l["ladder_id"]),
                "name": l["name"],
                "type": l["ladder_type"],
                "status": l["status"],
                "starts_at": l["starts_at"].isoformat() if l["starts_at"] else None,
                "ends_at": l["ends_at"].isoformat() if l["ends_at"] else None,
                "rules": parse_rules_jsonb(l["rules"]),
            }
            for l in ladders
        ],
    }
    data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    await interaction.response.send_message(
        file=discord.File(io.BytesIO(data), filename=f"laddergg-{interaction.guild.id}.json"),
        ephemeral=True,
    )



@bot.tree.command(
    description="Archiviert eine Ladder und erstellt daraus einen neuen Split."
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(
    ladder_id="Bestehende Ladder-ID",
    new_name="Name für den neuen Split",
)
async def newsplit(
    interaction: discord.Interaction,
    ladder_id: int,
    new_name: str,
):
    lang = await db.get_guild_language(interaction.guild.id)

    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )

    try:
        ladder = await db.get_ladder(
            interaction.guild.id,
            ladder_id,
        )
        if not ladder:
            await interaction.followup.send(
                "Ladder not found." if lang == "en" else "Ladder nicht gefunden.",
                ephemeral=True,
            )
            return

        await archive_ladder(
            interaction.guild.id,
            ladder_id,
        )
        new_id = await db.duplicate_ladder(
            interaction.guild.id,
            ladder_id,
            new_name.strip() or (
                f"{ladder['name']} New Split"
                if lang == "en"
                else f"{ladder['name']} · Neuer Split"
            ),
        )

        await interaction.followup.send(
            (
                f"✅ Old split archived. New template: `{new_id}`."
                if lang == "en"
                else f"✅ Alter Split archiviert. Neue Vorlage: `{new_id}`."
            ),
            ephemeral=True,
        )
    except Exception:
        logger.exception("/newsplit fehlgeschlagen")
        await interaction.followup.send(
            (
                "❌ Something went wrong while creating the new split."
                if lang == "en"
                else "❌ Beim Erstellen des neuen Splits ist etwas schiefgelaufen."
            ),
            ephemeral=True,
        )


@bot.tree.command(
    description="Archiviert eine Ladder und schreibt einen finalen Sheet-Snapshot."
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(ladder_id="ID der Ladder aus /setup")
async def archiveladder(
    interaction: discord.Interaction,
    ladder_id: int,
):
    await interaction.response.defer(
        ephemeral=True,
        thinking=True,
    )
    try:
        await archive_ladder(
            interaction.guild.id,
            ladder_id,
        )
        await interaction.followup.send(
            "✅ Ladder archiviert. "
            "Falls Google Sheets aktiv ist, wurde ein Archiv-Tab erstellt.",
            ephemeral=True,
        )
    except Exception as exc:
        logger.exception("/archiveladder fehlgeschlagen")
        await interaction.followup.send(
            f"Fehler beim Archivieren: {exc}",
            ephemeral=True,
        )


@bot.tree.command(
    description="Pausiert oder aktiviert eine Ladder."
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(
    ladder_id="ID der Ladder",
    status="active oder paused",
)
async def ladderstatus(
    interaction: discord.Interaction,
    ladder_id: int,
    status: str,
):
    status = status.strip().lower()
    if status not in {"active", "paused"}:
        await interaction.response.send_message(
            "Status muss `active` oder `paused` sein.",
            ephemeral=True,
        )
        return

    await db.set_ladder_status(
        interaction.guild.id,
        ladder_id,
        status,
    )
    await interaction.response.send_message(
        f"✅ Ladder-Status: **{status}**",
        ephemeral=True,
    )


@bot.tree.command(
    description="Erstellt aus einer Ladder eine neue Vorlage."
)
@app_commands.check(is_ladder_admin_check)
@app_commands.describe(
    ladder_id="ID der bestehenden Ladder",
    name="Name der neuen Vorlage",
)
async def duplicateladder(
    interaction: discord.Interaction,
    ladder_id: int,
    name: str,
):
    new_id = await db.duplicate_ladder(
        interaction.guild.id,
        ladder_id,
        name.strip() or "Ladder Kopie",
    )
    await interaction.response.send_message(
        f"✅ Vorlage erstellt. Neue Ladder-ID: `{new_id}`",
        ephemeral=True,
    )


async def send_permission_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, app_commands.CheckFailure):
        message = str(error) or (
            "Du brauchst Administrator-Rechte oder die konfigurierte "
            "Ladder-Admin-Rolle für diesen Command."
        )
    else:
        logger.error("Slash-Command Fehler: %s", error)
        message = "Der Command konnte nicht ausgeführt werden."

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
        )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    # Globaler Fallback für ALLE Slash-Commands, die keinen eigenen
    # .error()-Handler registriert haben. Verhindert, dass Nutzer ohne
    # die passende Rolle nur eine generische/leere Discord-Fehlermeldung
    # sehen oder der Command endlos bei "thinking..." hängen bleibt.
    await send_permission_error(interaction, error)


setup.error(send_permission_error)
settings.error(send_permission_error)
setleaderboardchannel.error(send_permission_error)
setladdername.error(send_permission_error)
setsheet.error(send_permission_error)
disablesheet.error(send_permission_error)
resetladder.error(send_permission_error)
fixalltimepeak.error(send_permission_error)
refresh.error(send_permission_error)
archiveladder.error(send_permission_error)
ladderstatus.error(send_permission_error)


COMMAND_HELP = {
    "de": {
        "title": "📚 LadderGG Commands",
        "user": "👤 Commands für alle",
        "admin": "🛠️ Admin Commands",
        "footer": "Nur Commands, für die du Berechtigungen hast, sind nutzbar.",
        "empty": "Keine Commands gefunden.",
    },
    "en": {
        "title": "📚 LadderGG Commands",
        "user": "👤 Commands for everyone",
        "admin": "🛠️ Admin Commands",
        "footer": "You can only use commands you have permission for.",
        "empty": "No commands found.",
    },
}

COMMAND_DESCRIPTIONS = {
    "de": {
        "account": "Riot-Account, Profil und Match-Historie.",
        "ladder": "Ladders, Challenges, Rankings und Statistiken.",
        "config": "Server-Setup, Tracking und Google Sheets.",
        "admin": "Refresh, Archiv, Split und Vorlagen.",
    },
    "en": {
        "account": "Riot account, profile and match history.",
        "ladder": "Ladders, challenges, standings and statistics.",
        "config": "Server setup, tracking and Google Sheets.",
        "admin": "Refresh, archive, split and templates.",
    },
}


def command_help_description(name: str, fallback: str, lang: str) -> str:
    return COMMAND_DESCRIPTIONS.get(lang, {}).get(name, fallback)


@bot.tree.command(
    name="commands",
    description="LadderGG command overview / Befehlsübersicht.",
)
@app_commands.describe(language="Language / Sprache")
@app_commands.choices(
    language=[
        app_commands.Choice(name="Deutsch", value="de"),
        app_commands.Choice(name="English", value="en"),
    ]
)
async def commands(
    interaction: discord.Interaction,
    language: Optional[app_commands.Choice[str]] = None,
):
    lang = language.value if language else await db.get_guild_language(
        interaction.guild.id
    )

    if lang == "en":
        description = (
            "**Account**\n"
            "`/account link` · link Riot account\n"
            "`/account profile` · profile\n"
            "`/account matches` · recent matches\n\n"
            "**Ladders**\n"
            "`/ladder list` · available ladders\n"
            "`/ladder view` · leaderboard\n"
            "`/ladder join` · join a ladder\n"
            "`/ladder stats` · statistics\n"
            "`/ladder history` · past ladders\n\n"
            "**Configuration**\n"
            "`/config setup` · create/manage ladders\n"
            "`/config language` · server language\n"
            "`/config sheet` · Google Sheets\n\n"
            "**Admin**\n"
            "`/admin refresh` · force refresh\n"
            "`/admin health` · service status"
        )
    else:
        description = (
            "**Account**\n"
            "`/account link` · Riot-Account verbinden\n"
            "`/account profile` · Profil\n"
            "`/account matches` · letzte Matches\n\n"
            "**Ladders**\n"
            "`/ladder list` · verfügbare Ladders\n"
            "`/ladder view` · Leaderboard\n"
            "`/ladder join` · Ladder beitreten\n"
            "`/ladder stats` · Statistiken\n"
            "`/ladder history` · vergangene Ladders\n\n"
            "**Konfiguration**\n"
            "`/config setup` · Ladders erstellen/verwalten\n"
            "`/config language` · Serversprache\n"
            "`/config sheet` · Google Sheets\n\n"
            "**Admin**\n"
            "`/admin refresh` · Update erzwingen\n"
            "`/admin health` · Systemstatus"
        )

    await interaction.response.send_message(
        embed=discord.Embed(
            title="📚 LadderGG Commands",
            description=description,
        ),
        ephemeral=True,
    )


# ------------------------------------------------------------------
# Public slash-command surface cleanup
# The old flat commands remain as callbacks internally for backwards
# compatible code paths, but are removed from Discord's command list.
# ------------------------------------------------------------------
LEGACY_FLAT_COMMANDS = {
    "register",
    "unregister",
    "leaderboard",
    "setup",
    "settings",
    "setleaderboardchannel",
    "setladdername",
    "challengerules",
    "challengepreset",
    "challengemultiplier",
    "challengescoring",
    "setsheet",
    "disablesheet",
    "refresh",
    "resetladder",
    "fixalltimepeak",
    "joinladder",
    "profile",
    "compare",
    "ladderstats",
    "halloffame",
    "matches",
    "newsplit",
    "archiveladder",
    "ladderstatus",
    "duplicateladder",
}

for _legacy_name in LEGACY_FLAT_COMMANDS:
    bot.tree.remove_command(_legacy_name)

bot.run(DISCORD_TOKEN)
