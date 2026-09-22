"""Lecture des données personnelles locales : agenda (EventKit) et mails non lus (Mail.app).

Rien n'est modifié ici : lecture seule. L'agenda n'est lu que localement (rien ne part vers
l'API). Les mails lus pour le tri partent vers l'API TypeSafe (expéditeur, objet, début du
texte) : uniquement quand vous le demandez (« quels mails demandent une action ? »).
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

FS, RS = "\x1f", "\x1e"  # séparateurs de champ / d'enregistrement (absents des textes courants)


class AppleDataError(RuntimeError):
    pass


# ------------------------------------------------------------------ agenda (EventKit)
@dataclass(frozen=True)
class Event:
    start: datetime
    title: str
    all_day: bool
    calendar: str


def _event_store():
    import EventKit

    store = EventKit.EKEventStore.alloc().init()
    done = threading.Event()
    granted: list[bool] = []

    def cb(ok, err):
        granted.append(bool(ok))
        done.set()

    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):  # macOS 14+
        store.requestFullAccessToEventsWithCompletion_(cb)
    else:
        store.requestAccessToEntityType_completion_(0, cb)
    done.wait(30)
    if not (granted and granted[0]):
        raise AppleDataError("accès au Calendrier refusé : Réglages › Confidentialité › Calendriers")
    return store


def agenda(day: datetime) -> list[Event]:
    from Foundation import NSDate

    store = _event_store()
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)
    to_ns = lambda d: NSDate.dateWithTimeIntervalSince1970_(d.timestamp())
    pred = store.predicateForEventsWithStartDate_endDate_calendars_(to_ns(start), to_ns(end), None)
    events = []
    for e in store.eventsMatchingPredicate_(pred) or []:
        at = datetime.fromtimestamp(e.startDate().timeIntervalSince1970())
        events.append(Event(at, str(e.title() or "(sans titre)"), bool(e.isAllDay()), str(e.calendar().title())))
    return sorted(events, key=lambda ev: (not ev.all_day, ev.start))


def describe_agenda(day: datetime, events: list[Event], now: datetime | None = None) -> str:
    now = now or datetime.now()
    delta = (day.date() - now.date()).days
    when = {0: "Aujourd'hui", 1: "Demain", -1: "Hier"}.get(delta, day.strftime("Le %d/%m"))
    if not events:
        return f"{when}, rien à l'agenda."
    parts = [f"{ev.title} (toute la journée)" if ev.all_day else f"{ev.start.hour} h {ev.start.minute:02d} : {ev.title}"
             for ev in events]
    return f"{when}, {len(events)} événement{'s' if len(events) > 1 else ''} : " + " ; ".join(parts) + "."


# ------------------------------------------------------------------ mails non lus (Mail.app)
_UNREAD_SCRIPT = [
    "on run argv",
    "set n to (item 1 of argv) as integer",
    "set fs to character id 31",
    "set rs to character id 30",
    'set out to ""',
    'tell application "Mail"',
    "set msgs to (messages of inbox whose read status is false)",
    "set c to count of msgs",
    "if c > n then set c to n",
    "repeat with i from 1 to c",
    "set m to item i of msgs",
    "set body to content of m",
    "if length of body > 400 then set body to text 1 thru 400 of body",
    "set out to out & (sender of m) & fs & (subject of m) & fs & body & rs",
    "end repeat",
    "end tell",
    "return out",
    "end run",
]


@dataclass(frozen=True)
class Mail:
    sender: str
    subject: str
    excerpt: str


def unread_mails(limit: int = 15) -> list[Mail]:
    argv = ["osascript", *[x for line in _UNREAD_SCRIPT for x in ("-e", line)], str(limit)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired as exc:
        raise AppleDataError("Mail ne répond pas") from exc
    if proc.returncode != 0:
        raise AppleDataError(f"lecture de Mail impossible : {proc.stderr.strip()[-200:]}")
    mails = []
    for rec in proc.stdout.split(RS):
        fields = rec.strip("\n").split(FS)
        if len(fields) == 3:
            mails.append(Mail(fields[0].strip(), fields[1].strip(), " ".join(fields[2].split())[:400]))
    return mails


ACTION_Q = ("Does email `m{i}` ask the recipient to do something (reply, decide, pay, sign, attend, send a document) "
            "rather than just inform, advertise or notify automatically?")
IMPORTANT_Q = ("Is email `m{i}` personally important to the recipient (from a real person or about money, work, "
               "health, administration, deadlines), rather than a newsletter, promotion or automatic notification?")


def triage(client, mails: list[Mail]) -> list[tuple[Mail, float, float]]:
    """Un seul appel Jev : deux Nouls par mail (demande une action ? important ?)."""
    if not mails:
        return []
    state = {f"m{i}": {"from": m.sender, "subject": m.subject, "excerpt": m.excerpt} for i, m in enumerate(mails)}
    questions = {}
    for i in range(len(mails)):
        questions[f"a{i}"] = (ACTION_Q.format(i=i), None)
        questions[f"i{i}"] = (IMPORTANT_Q.format(i=i), None)
    scores = client.nouls(state, {k: (q, c) for k, (q, c) in questions.items()})
    return [(m, scores[f"a{i}"], scores[f"i{i}"]) for i, m in enumerate(mails)]


def describe_triage(rows: list[tuple[Mail, float, float]]) -> str:
    if not rows:
        return "Aucun mail non lu."
    todo = [r for r in rows if r[1] >= 0.6]
    important = [r for r in rows if r[1] < 0.6 and r[2] >= 0.6]
    lines = [f"{len(rows)} mail{'s' if len(rows) > 1 else ''} non lu{'s' if len(rows) > 1 else ''}, "
             f"{len(todo)} demande{'nt' if len(todo) > 1 else ''} une action."]
    short = lambda s: s.split("<")[0].strip().strip('"') or s
    lines += [f"À traiter : {short(m.sender)} — {m.subject}" for m, _, _ in sorted(todo, key=lambda r: -r[1])[:6]]
    lines += [f"Important : {short(m.sender)} — {m.subject}" for m, _, _ in important[:4]]
    return "\n".join(lines)
