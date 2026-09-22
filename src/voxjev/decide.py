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
    code: str = ""  # cause, pour une explication lisible (voir explain)


def decide(result: JevResult, commands: dict[str, Command], s: Settings) -> Decision:
    """Règles, dans l'ordre :

    1. ``none`` ou commande inconnue              -> ignorer
    2. adressé < addressed_floor                  -> ignorer
    3. p(commande) < confirm_floor                -> ignorer
    4. destructive (config OU Noul >= seuil)      -> confirmer, quelle que soit p
    4b. mode sans confirmation + commande sans risque (settings.safe_commands) -> exécuter
    5. adressé < addressed_threshold              -> confirmer (adressé incertain)
    6. p(commande) < threshold                    -> confirmer
    7. sinon                                      -> exécuter
    """
    if result.command == NONE or result.command not in commands:
        return Decision(Verdict.IGNORE, None, code="none", reason=f"hors sujet (p_none={result.probabilities.get(NONE, 0):.2f})")
    cmd = commands[result.command]
    p = result.p_command
    if result.addressed < s.addressed_floor:
        return Decision(Verdict.IGNORE, cmd, code="not_addressed", reason=f"non adressé ({result.addressed:.2f} < {s.addressed_floor:.2f})")
    if p < s.confirm_floor:
        return Decision(Verdict.IGNORE, cmd, code="low_p", reason=f"trop incertain (p={p:.2f} < {s.confirm_floor:.2f})")
    # « annule ça » : c'est le code qui juge l'action inverse réelle (Launcher._plan_undo), pas le Noul.
    jev_destructive = result.destructive >= s.destructive_threshold and cmd.action.get("type") != "undo"
    destructive = cmd.destructive or jev_destructive
    if destructive:
        why = "config" if cmd.destructive else f"Noul={result.destructive:.2f}"
        return Decision(Verdict.CONFIRM, cmd, f"action destructrice ({why})", destructive=True, code="destructive")
    if cmd.always_confirm:
        return Decision(Verdict.CONFIRM, cmd, "confirmation systématique (config)", code="always_confirm")
    if result.addressed < s.addressed_threshold:
        doubt = Decision(Verdict.CONFIRM, cmd, code="uncertain_addressed", reason=f"adressé incertain ({result.addressed:.2f} < {s.addressed_threshold:.2f})")
    elif p < s.threshold:
        doubt = Decision(Verdict.CONFIRM, cmd, code="medium_p", reason=f"confiance moyenne (p={p:.2f} < {s.threshold:.2f})")
    else:
        return Decision(Verdict.EXECUTE, cmd, code="ok", reason=f"p={p:.2f} >= {s.threshold:.2f}")
    if s.quiet_mode and cmd.id in s.safe_commands:  # action sans risque : pas de confirmation
        return Decision(Verdict.EXECUTE, cmd, code="safe", reason=f"action sans risque, sans confirmation ({doubt.reason})")
    return doubt


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


def explain(decision: Decision, p: float, dry_run: bool = False) -> str:
    """La décision en une phrase lisible par un humain (affichée dans l'interface)."""
    pct = f"{p * 100:.0f} %"
    return {
        "none": "Ce n'est pas une commande — ignoré.",
        "not_addressed": "Ne semble pas m'être adressé — ignoré.",
        "low_p": f"Commande trop incertaine ({pct}) — ignoré.",
        "destructive": "Action destructrice : confirmation obligatoire.",
        "always_confirm": "Cette commande demande toujours une confirmation.",
        "fallback": "Jev hésitait : 2e option retenue, confirmation demandée.",
        "uncertain_addressed": "Pas sûr que ça m'était adressé : confirmation demandée.",
        "medium_p": f"Confiance moyenne ({pct}) : confirmation demandée.",
        "safe": "Action sans risque : " + ("aurait été exécutée sans confirmation." if dry_run else "exécutée sans confirmation."),
        "ok": f"Confiance {pct} : " + ("aurait été exécuté directement." if dry_run else "exécution directe."),
    }.get(decision.code, decision.reason)
