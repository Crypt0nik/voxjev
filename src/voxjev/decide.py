"""Décision déterministe à partir de la réponse Jev : exécuter, confirmer ou ignorer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .config import Command, Settings
from .jev_client import NONE, JevResult


class Verdict(str, Enum):
    EXECUTE = "execute"
    CONFIRM = "confirm"
    IGNORE = "ignore"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    command: Command | None
    reason: str
    destructive: bool = False


def decide(result: JevResult, commands: dict[str, Command], s: Settings) -> Decision:
    """Règles, dans l'ordre :

    1. ``none`` ou commande inconnue              -> ignorer
    2. adressé < addressed_floor                  -> ignorer
    3. p(commande) < confirm_floor                -> ignorer
    4. destructive (config OU Noul >= seuil)      -> confirmer, quelle que soit p
    5. adressé < addressed_threshold              -> confirmer (adressé incertain)
    6. p(commande) < threshold                    -> confirmer
    7. sinon                                      -> exécuter
    """
    if result.command == NONE or result.command not in commands:
        return Decision(Verdict.IGNORE, None, f"hors sujet (p_none={result.probabilities.get(NONE, 0):.2f})")
    cmd = commands[result.command]
    p = result.p_command
    if result.addressed < s.addressed_floor:
        return Decision(Verdict.IGNORE, cmd, f"non adressé ({result.addressed:.2f} < {s.addressed_floor:.2f})")
    if p < s.confirm_floor:
        return Decision(Verdict.IGNORE, cmd, f"trop incertain (p={p:.2f} < {s.confirm_floor:.2f})")
    destructive = cmd.destructive or result.destructive >= s.destructive_threshold
    if destructive:
        why = "config" if cmd.destructive else f"Noul={result.destructive:.2f}"
        return Decision(Verdict.CONFIRM, cmd, f"action destructrice ({why})", destructive=True)
    if result.addressed < s.addressed_threshold:
        return Decision(Verdict.CONFIRM, cmd, f"adressé incertain ({result.addressed:.2f} < {s.addressed_threshold:.2f})")
    if p < s.threshold:
        return Decision(Verdict.CONFIRM, cmd, f"confiance moyenne (p={p:.2f} < {s.threshold:.2f})")
    return Decision(Verdict.EXECUTE, cmd, f"p={p:.2f} >= {s.threshold:.2f}")


_YES = re.compile(r"^\W*(oui|ouais|yes|ok|okay|d'accord|vas-?y|go|confirme\w*|valide\w*|exécute\w*|c'est bon|bien sûr)\b", re.I)
_NO = re.compile(r"^\W*(non|no|nan|annule\w*|stop|arrête\w*|laisse tomber|pas maintenant|surtout pas)\b", re.I)


def parse_yes_no(text: str) -> bool | None:
    """Réponse vocale à une confirmation : True (oui), False (non), None (autre chose).

    Déterministe, sans appel à Jev : seule une réponse explicite en tête de phrase compte.
    """
    text = text.replace("’", "'").strip()
    if _NO.match(text):
        return False
    if _YES.match(text):
        return True
    return None
