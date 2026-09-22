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
        "Pick `none` for conversation, thinking aloud, general knowledge questions, noise, "
        "or a request that none of the listed commands covers.",
    ],
}
ADDRESSED_INSTRUCTIONS = (
    "Is `transcript` an instruction spoken to the computer's voice assistant, "
    "rather than speech meant for another person, thinking aloud, or background noise?"
)
ADDRESSED_CRITERIA = {
    "true": "A direct request or command for the computer to do something",
    "false": "Conversation with someone else, a remark, a narration of past events, a general question, or noise",
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


def build_questions(commands: list[Command], none_option: dict) -> dict:
    criteria = {c.id: {"what": c.description, "examples": list(c.examples)} for c in commands}
    criteria[NONE] = {
        "what": none_option.get("what", "No command applies"),
        "examples": list(none_option.get("examples", [])),
    }
    return {
        "command": {"type": "choice", "instructions": COMMAND_INSTRUCTIONS, "criteria": criteria},
        "addressed": {"type": "noul", "instructions": ADDRESSED_INSTRUCTIONS, "criteria": ADDRESSED_CRITERIA},
        "destructive": {"type": "noul", "instructions": DESTRUCTIVE_INSTRUCTIONS, "criteria": DESTRUCTIVE_CRITERIA},
        "compound": {"type": "noul", "instructions": COMPOUND_INSTRUCTIONS, "criteria": COMPOUND_CRITERIA},
    }


class DecisionClient(Protocol):
    def evaluate(self, state: dict, commands: list[Command], none_option: dict) -> JevResult: ...


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

    def evaluate(self, state: dict, commands: list[Command], none_option: dict) -> JevResult:
        from typesafe_sdk import TypeSafeError

        questions = build_questions(commands, none_option)
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
        )

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

    def evaluate(self, state: dict, commands: list[Command], none_option: dict) -> JevResult:
        self.calls.append(state)
        if self.fail:
            raise JevError("appel Jev échoué (faux client, échec simulé)")
        transcript = state["transcript"]
        if transcript in self.responses:
            return self.responses[transcript]
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
