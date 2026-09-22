"""Couche de décision Jev : UN appel par énoncé, trois questions.

- ``command``     (Choice) : quelle commande de la config, ou ``none``
- ``addressed``   (Noul)   : l'énoncé est-il adressé à l'assistant ?
- ``destructive`` (Noul)   : l'action demandée est-elle destructrice ?

Seules ces données partent vers l'API : transcript, app au premier plan, liste des apps
installées, id du mode actif, id de la dernière commande (voir README).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from rapidfuzz import fuzz

from .config import Command

NONE = "none"

COMMAND_INSTRUCTIONS = {
    "question": "Which launcher command does `transcript` ask the voice assistant to perform?",
    "notes": [
        "`transcript` is French speech-to-text output and may contain recognition errors.",
        "Short imperative orders are commands even without a verb object or politeness: dictation "
        "('tape …', 'écris …'), editing keys ('copie', 'colle'), scrolling ('descends'), the time or the "
        "agenda ('quelle heure est-il', 'qu'est-ce que j'ai demain').",
        "Pick `none` for conversation with someone else, thinking aloud, narration of past events, noise, "
        "or a request that none of the listed commands covers.",
    ],
}
ADDRESSED_INSTRUCTIONS = (
    "Is `transcript` an instruction spoken to the computer's voice assistant, "
    "rather than speech meant for another person, thinking aloud, or background noise?"
)
ADDRESSED_CRITERIA = {
    "true": "A direct request or command for the computer to do something, including very short or "
            "casual orders such as 'like ce son', 'next', 'moins fort', 'mets du jazz', 'copie', 'descends', "
            "dictation such as 'tape bonjour tout le monde' or 'écris je serai en retard', and questions asked to "
            "the assistant such as 'quelle heure est-il', 'qu'est-ce que j'ai demain', 'dis-moi …', "
            "'lance ma routine du matin', and personal-assistant requests such as 'note que je dois acheter des piles', "
            "'retiens que …', 'qu'est-ce que je t'avais dit sur …', 'rappelle-moi de …'",
    "false": "Conversation with someone else (often naming them: 'Marc, ouvre la porte'), a remark, a narration "
             "of past events ('j'ai tapé mon rapport hier'), thinking aloud, or noise",
}
DESTRUCTIVE_INSTRUCTIONS = (
    "Would carrying out the request in `transcript` delete or erase data, empty something, "
    "quit an application or close work that may be unsaved, or otherwise be hard to undo?"
)
DESTRUCTIVE_CRITERIA = {
    "true": "The requested action destroys, deletes, empties, quits or discards something",
    "false": "The requested action only opens, shows, searches, plays or adjusts something reversibly",
}

COMPOUND_INSTRUCTIONS = (
    "Does `transcript` ask the assistant to perform two or more distinct actions in sequence, "
    "for example open an app and then search, play or like something?"
)
COMPOUND_CRITERIA = {
    "true": "Several separate actions are requested, e.g. 'ouvre Spotify et mets du jazz', 'cherche X puis lance-la'",
    "false": "A single action, even if it has details, e.g. 'cherche la météo à Lyon dans Chrome'",
}


PICK_QUESTIONS = {
    "menu": (
        "Which menu item of the frontmost application (`frontmost_app`) would carry out the request in "
        "`transcript`? Pick `none` if no listed item does exactly that."
    ),
    "shortcut": (
        "Which of the user's macOS Shortcuts does `transcript` ask to run? Pick `none` if the request "
        "does not name or clearly describe one of them."
    ),
    "span": (
        "Which fragment of `transcript` is the free-text value of the request: the search query, the song "
        "or artist, the text to type, the title of a reminder, event or note, the fact to remember, the "
        "question or goal? Pick the exact fragment without the command words ('cherche', 'tape', "
        "'rappelle-moi de', 'retiens que'), app or site names, or dates. Pick `none` if there is no such value."
    ),
}
PICK_PREFIX = {"menu": "m", "shortcut": "s", "span": "t"}


class JevError(RuntimeError):
    pass


@dataclass
class JevResult:
    command: str
    probabilities: dict[str, float]
    confidence: float
    addressed: float
    destructive: float
    compound: float = 0.0  # probabilité que l'énoncé demande plusieurs actions
    latency_ms: float = 0.0
    model: str = ""
    input_tokens: int | None = None
    request_id: str | None = None
    picks: dict[str, tuple[int | None, float]] = field(default_factory=dict)  # source -> (indice|None, p)

    @property
    def p_command(self) -> float:
        return self.probabilities.get(self.command, 0.0)

    def runners_up(self, n: int = 2) -> list[tuple[str, float]]:
        ranked = sorted(self.probabilities.items(), key=lambda kv: -kv[1])
        return [kv for kv in ranked if kv[0] != self.command][:n]


def build_state(transcript: str, frontmost: str | None, apps: list[str] | tuple[str, ...],
                mode: str, last_command: str | None) -> dict:
    return {
        "transcript": transcript,
        "frontmost_app": frontmost or "unknown",
        "installed_apps": list(apps),
        "assistant_mode": mode,
        "last_command": last_command or "none",
    }


def build_questions(commands: list[Command], none_option: dict, picks: dict | None = None,
                    hints: dict[str, str] | None = None) -> dict:
    hints = hints or {}
    criteria = {c.id: {"what": c.description + (f" ({hints[c.id]})" if c.id in hints else ""),
                       "examples": list(c.examples)} for c in commands}
    criteria[NONE] = {
        "what": none_option.get("what", "No command applies"),
        "examples": list(none_option.get("examples", [])),
    }
    return {
        "command": {"type": "choice", "instructions": COMMAND_INSTRUCTIONS, "criteria": criteria},
        "addressed": {"type": "noul", "instructions": ADDRESSED_INSTRUCTIONS, "criteria": ADDRESSED_CRITERIA},
        "destructive": {"type": "noul", "instructions": DESTRUCTIVE_INSTRUCTIONS, "criteria": DESTRUCTIVE_CRITERIA},
        "compound": {"type": "noul", "instructions": COMPOUND_INSTRUCTIONS, "criteria": COMPOUND_CRITERIA},
    } | pick_questions(picks or {})


def pick_questions(picks: dict) -> dict:
    """Une question Choice par source de candidats (menus, Raccourcis, segments), options + none."""
    out = {}
    for source, cands in picks.items():
        if not cands:
            continue
        prefix = PICK_PREFIX[source]
        options = {f"{prefix}{i}": {"what": c.label} for i, c in enumerate(cands[:240])}
        options[NONE] = {"what": "None of these"}
        out[f"pick_{source}"] = {"type": "choice", "instructions": PICK_QUESTIONS[source], "criteria": options}
    return out


def read_pick(choice: str, probabilities: dict[str, float], source: str) -> tuple[int | None, float]:
    p = probabilities.get(choice, 0.0)
    if choice == NONE or not choice.startswith(PICK_PREFIX[source]):
        return None, p
    return int(choice[len(PICK_PREFIX[source]):]), p


class DecisionClient(Protocol):
    def evaluate(self, state: dict, commands: list[Command], none_option: dict, picks: dict | None = None,
                 hints: dict | None = None) -> JevResult: ...


class JevClient:
    """Client réel. La clé est lue par le SDK dans TYPESAFE_API_KEY (jamais en dur)."""

    def __init__(self, model: str = "jev-latest", timeout: float = 8.0):
        from typesafe_sdk import RetryPolicy, TypeSafeClient

        self.model = model
        # Peu de retries : un lanceur vocal doit échouer vite plutôt qu'attendre.
        self._client = TypeSafeClient(
            model=model, timeout=timeout,
            retry=RetryPolicy(max_retries=1, backoff_initial=0.2, backoff_max=0.5, timeout=timeout),
        )

    def evaluate(self, state: dict, commands: list[Command], none_option: dict, picks: dict | None = None,
                 hints: dict | None = None) -> JevResult:
        from typesafe_sdk import TypeSafeError

        questions = build_questions(commands, none_option, picks, hints)
        started = time.perf_counter()
        try:
            r = self._client.system_one(state=state, questions=questions)
        except TypeSafeError as exc:
            raise JevError(f"appel Jev échoué : {exc}") from exc
        latency = (time.perf_counter() - started) * 1000
        cmd = r.choices["command"]
        return JevResult(
            command=cmd.choice,
            probabilities=dict(cmd.probabilities),
            confidence=cmd.confidence,
            addressed=r.nouls["addressed"].noul,
            destructive=r.nouls["destructive"].noul,
            compound=r.nouls["compound"].noul if "compound" in r.nouls else 0.0,
            latency_ms=latency,
            model=r.model,
            input_tokens=r.usage.input_tokens,
            request_id=getattr(r, "request_id", None),
            picks={src: read_pick(r.choices[f"pick_{src}"].choice, dict(r.choices[f"pick_{src}"].probabilities), src)
                   for src in (picks or {}) if f"pick_{src}" in r.choices},
        )

    def choose(self, state: dict, question: str, options: list[str], none: str = "None of these") -> tuple[int | None, float, float]:
        """Question Choice isolée (2e étape : fichier, souvenir…). -> (indice|None, p, latence_ms)."""
        from typesafe_sdk import TypeSafeError

        criteria = {f"o{i}": {"what": o} for i, o in enumerate(options[:240])} | {NONE: {"what": none}}
        started = time.perf_counter()
        try:
            r = self._client.system_one(state=state, questions={
                "pick": {"type": "choice", "instructions": question, "criteria": criteria}})
        except TypeSafeError as exc:
            raise JevError(f"appel Jev échoué : {exc}") from exc
        c = r.choices["pick"]
        p = dict(c.probabilities).get(c.choice, 0.0)
        idx = None if c.choice == NONE else int(c.choice[1:])
        return idx, p, (time.perf_counter() - started) * 1000

    def nouls(self, state: dict, questions: dict[str, tuple[str, dict]]) -> dict[str, float]:
        """Plusieurs Nouls en un appel (tri des mails…). questions : clé -> (instructions, critères)."""
        from typesafe_sdk import TypeSafeError

        try:
            r = self._client.system_one(state=state, questions={
                k: {"type": "noul", "instructions": q} | ({"criteria": c} if c else {})
                for k, (q, c) in questions.items()})
        except TypeSafeError as exc:
            raise JevError(f"appel Jev échoué : {exc}") from exc
        return {k: r.nouls[k].noul for k in questions}

    def raw(self, state: dict, questions: dict):
        """Requête libre (agent bureau). Renvoie la réponse du SDK."""
        from typesafe_sdk import TypeSafeError

        try:
            return self._client.system_one(state=state, questions=questions)
        except TypeSafeError as exc:
            raise JevError(f"appel Jev échoué : {exc}") from exc

    def warm(self) -> None:
        """Ouvre la connexion TLS à l'avance (à l'appui sur la touche) : ~100–300 ms gagnées."""
        try:
            self._client.system_one(state={"x": "ping"}, questions={
                "ok": {"type": "noul", "instructions": "Is `x` equal to 'ping'?"}})
        except Exception:
            pass

    def close(self) -> None:
        self._client.close()


@dataclass
class FakeJevClient:
    """Faux client hors ligne pour les tests.

    - ``responses`` : réponses scriptées par transcript exact (prioritaires) ;
    - sinon heuristique déterministe : similarité floue avec les exemples de chaque commande.
    """

    responses: dict[str, JevResult] = field(default_factory=dict)
    fail: bool = False
    calls: list[dict] = field(default_factory=list)

    def evaluate(self, state: dict, commands: list[Command], none_option: dict, picks: dict | None = None,
                 hints: dict | None = None) -> JevResult:
        self.calls.append(state)
        self.last_picks = picks or {}
        if self.fail:
            raise JevError("appel Jev échoué (faux client, échec simulé)")
        transcript = state["transcript"]
        if transcript in self.responses:
            res = self.responses[transcript]
            if not res.picks and picks:
                res.picks = self._fake_picks(transcript, picks)
            return res
        res = self._heuristic(transcript, commands)
        res.picks = self._fake_picks(transcript, picks or {})
        return res

    @staticmethod
    def _fake_picks(transcript: str, picks: dict) -> dict:
        out = {}
        for src, cands in picks.items():
            if src == "span":
                continue  # le faux client ne sait pas choisir un segment : les regex suffisent
            scored = [(fuzz.token_set_ratio(transcript.lower(), c.label.lower()) / 100, i) for i, c in enumerate(cands)]
            best = max(scored, default=(0.0, None))
            out[src] = (best[1], best[0]) if best[0] >= 0.7 else (None, 1 - best[0])
        return out

    def choose(self, state: dict, question: str, options: list[str], none: str = "None of these"):
        target = str(state.get("transcript") or state.get("question") or "").lower()
        scored = [(fuzz.token_set_ratio(target, o.lower()) / 100, i) for i, o in enumerate(options)]
        best = max(scored, default=(0.0, None))
        return (best[1], best[0], 0.0) if best[0] >= 0.5 else (None, 1 - best[0], 0.0)

    def nouls(self, state: dict, questions: dict) -> dict[str, float]:
        return {k: 0.5 for k in questions}

    def warm(self) -> None:
        pass

    def _heuristic(self, transcript: str, commands: list[Command]) -> JevResult:
        from .multi import split_rules  # heuristique : plusieurs verbes d'action reliés

        compound = 0.9 if len(split_rules(transcript)) > 1 else 0.05
        text = transcript.lower()
        scores = {
            c.id: max(fuzz.token_set_ratio(text, ex.lower()) for ex in c.examples) / 100
            for c in commands
        }
        best = max(scores, key=scores.get) if scores else NONE
        if not scores or scores[best] < 0.6:
            probs = {c.id: 0.0 for c in commands} | {NONE: 1.0}
            return JevResult(NONE, probs, 1.0, addressed=0.2, destructive=0.05, compound=compound)
        rest = (1 - scores[best]) / max(len(commands), 1)
        probs = {c.id: (scores[best] if c.id == best else rest) for c in commands} | {NONE: rest}
        chosen = next(c for c in commands if c.id == best)
        return JevResult(best, probs, scores[best], addressed=0.95,
                         destructive=0.9 if chosen.destructive else 0.05, compound=compound)
