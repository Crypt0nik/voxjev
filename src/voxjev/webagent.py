"""Agent web : confie une tâche en plusieurs étapes dans Chrome à jev-ultrafast (Browser Use × TypeSafe).

jev-ultrafast lit la page, Jev choisit une opération (clic, saisie, sélection…) et un élément,
un petit LLM écrit le texte à taper. voxjev le pilote **pas à pas** pour ajouter ses garde-fous :

- chaque action choisie est inspectée AVANT exécution ; si son libellé ressemble à un achat, un
  paiement, une réservation, un envoi, une publication ou une suppression, l'agent s'arrête et
  vous rend la main (rien n'est cliqué) ;
- l'objectif transmis rappelle ces interdits et demande de s'arrêter dès que le résultat est visible ;
- budget de temps et d'actions ; l'onglet (en arrière-plan pendant le travail) est ensuite mis au
  premier plan et laissé ouvert pour que vous voyiez le résultat ;
- la télémétrie de browser-harness est désactivée.

Pré-requis : Chrome avec « Allow remote debugging » (chrome://inspect/#remote-debugging), et une
clé OpenRouter (OPENROUTER_API_KEY) pour les étapes où il faut taper du texte.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

os.environ.setdefault("BH_TELEMETRY", "0")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

GUARD = (
    "\n\nContraintes strictes : ne jamais acheter, payer, commander, réserver, s'abonner, envoyer un message, "
    "publier ou supprimer quoi que ce soit ; ne jamais saisir d'informations personnelles. "
    "Arrête-toi (DONE) dès que le résultat demandé est visible à l'écran."
)
RISKY = re.compile(
    r"\b(?:achet\w*|achat\w*|payer|paiement\w*|pay|checkout|commander|passer (?:la )?commande|order|"
    r"réserv\w*|book|s'abonner|abonnement|subscribe|envoy\w*|send|publi\w*|poster|post|tweet|"
    r"supprim\w*|delete|remove|buy|purchase|donat\w*|faire un don)\b",
    re.I,
)


class WebAgentError(RuntimeError):
    pass


@dataclass
class WebResult:
    status: str  # done | blocked | stopped | timeout
    summary: str
    url: str = ""
    actions: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


def _configure_text_model() -> None:
    """jev-ultrafast lit TEXT_MODEL_* ; on les dérive de la clé OpenRouter si absents."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key and not os.environ.get("TEXT_MODEL_API_KEY"):
        os.environ["TEXT_MODEL_API_KEY"] = key
        os.environ.setdefault("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
        os.environ.setdefault("TEXT_MODEL", "inception/mercury-2.5")
        os.environ.setdefault("TEXT_MODEL_REASONING", "none")


def run_web_task(goal: str, url: str, *, max_actions: int = 25, timeout_s: float = 90,
                 on_progress: Callable[[str], None] | None = None) -> WebResult:
    _configure_text_model()
    try:
        from jev_ultrafast import Agent
        from jev_ultrafast.browser import StalePage, cdp
    except Exception as exc:  # dépendance absente ou Chrome non joignable
        raise WebAgentError(f"agent web indisponible : {exc}") from exc

    started = time.monotonic()
    progress = on_progress or (lambda msg: None)
    progress("connexion à Chrome…")
    try:
        agent = Agent(url, goal + GUARD)
    except Exception as exc:
        raise WebAgentError(
            f"connexion à Chrome impossible ({exc}). Activez chrome://inspect/#remote-debugging "
            "(« Allow remote debugging ») puis cliquez « Allow » sur la fenêtre de Chrome."
        ) from exc

    state = agent.state
    actions: list[str] = []
    result = None
    try:
        while state["status"] not in {"done", "blocked"}:
            if time.monotonic() - started > timeout_s:
                result = WebResult("timeout", f"temps écoulé ({timeout_s:.0f} s)")
                break
            if len(state["history"]) >= max_actions:
                result = WebResult("stopped", f"budget de {max_actions} actions atteint")
                break
            try:
                agent.command("predict")
                decision = state["decision"]
                choice = decision["choice"]
                if choice not in {"DONE", "BLOCKED"}:
                    action = next(a for a in state["page"]["actions"] if a["id"] == choice)
                    label = action.get("label", "")
                    if RISKY.search(label):
                        result = WebResult("stopped", f"arrêt avant une action sensible : « {label} ». "
                                                      "Terminez vous-même si c'est voulu.")
                        break
                    progress(f"{action['kind']} : {label[:60]}")
                agent.command("act", {"fingerprint": state["page"]["fingerprint"]})
                if state["history"] and (not actions or actions[-1] != state["history"][-1]["action"]):
                    actions.append(state["history"][-1]["action"])
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = agent.browser.observe(screenshot=False)
        if result is None:
            ok = state["status"] == "done"
            result = WebResult("done" if ok else "blocked",
                               "objectif atteint" if ok else "l'agent est bloqué sur cette page")
    except Exception as exc:
        result = WebResult("blocked", f"erreur : {exc}")
    finally:
        # Montrer le résultat : onglet au premier plan, laissé ouvert (pas de agent.close()).
        try:
            cdp("Target.activateTarget", targetId=agent.browser.target)
            subprocess.run(["open", "-a", "Google Chrome"], check=False, timeout=5)
        except Exception:
            pass
    result.url = state["page"].get("url", "")
    result.actions = actions
    result.elapsed_s = time.monotonic() - started
    return result
