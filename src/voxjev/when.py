"""Dates et durées en français, calculées par du code (jamais par un modèle).

    parse_duration("mets un minuteur de 2 minutes 30")   -> 150
    parse_when("rappelle-moi d'appeler Paul demain à 18h", now) -> When(datetime(…, 18, 0), "rappelle-moi d'appeler Paul")

Couvre : « dans 10 minutes / 2 heures / 3 jours », « aujourd'hui », « demain », « après-demain »,
« ce soir / ce matin / cet après-midi / ce midi », les jours de la semaine (« lundi », « mardi
prochain »), « le 12 octobre », « le 12/10 », « le 3 », et les heures « à 14h », « à 14h30 »,
« à 9 heures et quart », « à midi », « à minuit », « à 8h du soir ». Les nombres écrits en
lettres (« dix », « vingt-cinq ») sont acceptés.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

UNITS = {
    "zéro": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7,
    "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15,
    "seize": 16, "dix-sept": 17, "dix-huit": 18, "dix-neuf": 19,
}
TENS = {"vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50, "soixante": 60}
MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
WEEKDAYS = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6}
DEFAULT_HOUR = 9  # « demain » sans heure : 9 h


_UNIT_WORDS = r"(?:secondes?|minutes?|heures?|h\b|jours?|semaines?|et demie?|et quart)"


def _word_numbers(text: str) -> str:
    """« vingt-cinq minutes » -> « 25 minutes », « à dix heures » -> « à 10 heures ».

    Seuls les nombres suivis d'une unité de temps sont convertis (« appeler un ami » reste intact).
    """
    tens = "|".join(TENS)
    units = "|".join(sorted(UNITS, key=len, reverse=True))
    number = rf"(?:(?:{tens})(?:[- ]et[- ]|-)?(?:{units})?|{units})"

    def value(words: str) -> str:
        w = words.lower()
        m = re.fullmatch(rf"({tens})(?:[- ]et[- ]|-)?({units})?", w)
        if m:
            return str(TENS[m.group(1)] + (UNITS[m.group(2)] if m.group(2) else 0))
        return str(UNITS[w])

    return re.sub(rf"\b({number})(?= {_UNIT_WORDS})", lambda m: value(m.group(1)), text, flags=re.I)


def _prep(text: str) -> str:
    text = text.replace("’", "'")
    text = re.sub(r"\b(?:une )?demi[- ]heure\b", "30 minutes", text, flags=re.I)
    text = re.sub(r"\btrois quarts? d'heure\b", "45 minutes", text, flags=re.I)
    text = re.sub(r"\b(?:un )?quart d'heure\b", "15 minutes", text, flags=re.I)
    text = _word_numbers(text)
    return re.sub(r"\s+", " ", text).strip()


_DUR_UNIT = {"s": 1, "sec": 1, "seconde": 1, "secondes": 1, "min": 60, "mn": 60, "minute": 60, "minutes": 60,
             "h": 3600, "heure": 3600, "heures": 3600, "jour": 86400, "jours": 86400}
_DURATION = re.compile(
    r"(\d+(?:[.,]\d+)?) ?(secondes?|sec|s|minutes?|min|mn|heures?|h|jours?)(?:\b|(?=\d))(?: ?(?:et )?(demie?|\d{1,2})\b(?! ?(?:secondes?|sec|s|minutes?|min|mn|heures?|h|jours?)\b))?", re.I
)


def parse_duration(text: str) -> int | None:
    """Somme des durées citées, en secondes (« 1 heure 30 », « 2 minutes et 30 secondes »)."""
    t = _prep(text)
    total = 0.0
    for m in _DURATION.finditer(t):
        n = float(m.group(1).replace(",", "."))
        unit = _DUR_UNIT[m.group(2).lower()]
        total += n * unit
        extra = (m.group(3) or "").lower()
        if extra:  # « 1h30 », « 2 minutes 30 », « 1 heure et demie »
            sub = {3600: 60, 60: 1}.get(unit, 0)
            total += (unit / 2) if extra.startswith("demi") else int(extra) * sub
    return int(total) if total > 0 else None


@dataclass(frozen=True)
class When:
    at: datetime
    rest: str  # la phrase sans l'expression de date (ex. le titre d'un rappel)
    has_time: bool = True


_TIME = re.compile(
    r"\b(?:à|a|vers|pour)? ?(?:(midi|minuit)|(\d{1,2}) ?(?:h|heures?|:)(?: ?(\d{2})| ?(et quart|et demie?|moins le quart|moins 15))?"
    r"(?: ?(du matin|de l'après-midi|de l'aprem|du soir))?)(?![\w/])", re.I
)
_REL = re.compile(r"\bdans (\d+) ?(minutes?|min|heures?|h|jours?|semaines?)\b", re.I)
_DAYWORDS = re.compile(r"\b(aujourd'hui|après-demain|apres-demain|demain)\b", re.I)
_PART = re.compile(r"\b(?:ce|cet|cette) ?(matin|midi|après-midi|aprem|soir)\b|(?<!du )(?<!de l')\b(matin|soir|après-midi)\b(?= |$)", re.I)
_WEEKDAY = re.compile(rf"\b({'|'.join(WEEKDAYS)})(?: (prochain))?\b", re.I)
_DATE_NAMED = re.compile(rf"\b(?:le )?(\d{{1,2}}|1er) ({'|'.join(MONTHS)})(?: (\d{{4}}))?\b", re.I)
_DATE_NUM = re.compile(r"\b(?:le )?(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", re.I)
_DATE_DAY = re.compile(r"\ble (\d{1,2}|1er)\b(?! ?(?:h|heures?|minutes?|:))", re.I)
_PART_HOUR = {"matin": 9, "midi": 12, "après-midi": 15, "aprem": 15, "soir": 20}


def _cut(text: str, m: re.Match) -> str:
    return (text[: m.start()] + " " + text[m.end():]).strip()


def parse_when(text: str, now: datetime | None = None) -> When | None:
    """Premier instant désigné par la phrase, ou None. Toujours dans le futur (sauf date passée explicite)."""
    now = now or datetime.now()
    t = _prep(text)

    m = _REL.search(t)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "j": timedelta(days=n),
                 "s": timedelta(weeks=n)}[unit[0]]
        return When(now + delta, _clean(_cut(t, m)))

    day: date | None = None
    rest = t
    for rx in (_DATE_NAMED, _DATE_NUM):
        m = rx.search(rest)
        if m:
            d = 1 if m.group(1) == "1er" else int(m.group(1))
            month = MONTHS[m.group(2).lower()] if rx is _DATE_NAMED else int(m.group(2))
            year = int(m.group(3)) if m.group(3) else now.year
            year += 2000 if year < 100 else 0
            try:
                day = date(year, month, d)
            except ValueError:
                return None
            if not m.group(3) and day < now.date():
                day = day.replace(year=year + 1)
            rest = _cut(rest, m)
            break
    if day is None and (m := _DAYWORDS.search(rest)):
        offset = {"aujourd'hui": 0, "demain": 1}.get(m.group(1).lower(), 2)
        day = now.date() + timedelta(days=offset)
        rest = _cut(rest, m)
    if day is None and (m := _WEEKDAY.search(rest)):
        ahead = (WEEKDAYS[m.group(1).lower()] - now.weekday()) % 7 or 7
        day = now.date() + timedelta(days=ahead)
        rest = _cut(rest, m)
    if day is None and (m := _DATE_DAY.search(rest)):
        d = 1 if m.group(1) == "1er" else int(m.group(1))
        y, mo = now.year, now.month
        if d < now.day:
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        try:
            day = date(y, mo, d)
        except ValueError:
            return None
        rest = _cut(rest, m)

    part = None
    if (pm := _PART.search(rest)):
        part = (pm.group(1) or pm.group(2)).lower()
        rest = _cut(rest, pm)

    hour = minute = None
    tm = _TIME.search(rest)
    if tm and (tm.group(1) or tm.group(2)):
        if tm.group(1):
            hour, minute = (12, 0) if tm.group(1).lower() == "midi" else (0, 0)
        else:
            hour = int(tm.group(2))
            minute = int(tm.group(3)) if tm.group(3) else 0
            frac = (tm.group(4) or "").lower()
            if frac == "et quart":
                minute = 15
            elif frac.startswith("et demi"):
                minute = 30
            elif frac.startswith("moins"):
                hour, minute = hour - 1, 45
            if (tm.group(5) or "").lower() in ("de l'après-midi", "de l'aprem", "du soir") and hour < 12:
                hour += 12
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        rest = _cut(rest, tm)
    else:
        tm = None
    if part is not None:
        if hour is None:
            hour, minute = _PART_HOUR[part], 0
        elif part in ("soir", "après-midi", "aprem") and hour < 12:
            hour += 12

    if day is None and hour is None:
        return None
    has_time = hour is not None
    if hour is None:
        hour, minute = DEFAULT_HOUR, 0
    if day is None:
        day = now.date()
        if datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute) <= now:
            if part is None and tm is not None and 1 <= hour <= 7 and hour + 12 > now.hour:
                hour += 12  # « à 6h » à 15 h : 18 h aujourd'hui plutôt que 6 h demain
            else:
                day += timedelta(days=1)
    at = datetime(day.year, day.month, day.day, hour, minute)
    return When(at, _clean(rest), has_time)


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" ,.;:")
    return re.sub(r"\s+(?:à|a|pour|le|vers|de|d')$", "", text, flags=re.I).strip(" ,.;:")


def format_when(at: datetime, now: datetime | None = None) -> str:
    """« demain à 18 h 00 », « lundi 12 octobre à 9 h 00 »."""
    now = now or datetime.now()
    days = (at.date() - now.date()).days
    hm = f"{at.hour} h {at.minute:02d}"
    if days == 0:
        return f"aujourd'hui à {hm}"
    if days == 1:
        return f"demain à {hm}"
    names = list(WEEKDAYS)
    months = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
              "octobre", "novembre", "décembre"]
    return f"{names[at.weekday()]} {at.day} {months[at.month - 1]} à {hm}"


def format_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h} h" if h else "", f"{m} min" if m else "", f"{s} s" if s else ""]
    return " ".join(p for p in parts if p) or "0 s"
