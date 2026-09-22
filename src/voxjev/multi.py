"""Demandes composées : « ouvre Spotify, cherche Daft Punk puis lance-la ».

Principe (cf. la démo « smart home » de TypeSafe) :
1. l'appel Jev habituel contient un Noul « plusieurs actions ? » : la détection ne coûte rien ;
2. si oui, un petit LLM (OpenRouter) découpe la phrase en sous-demandes simples ; sans clé, un
   découpage par règles prend le relais ;
3. chaque sous-demande repasse par le pipeline normal (Jev choisit une commande de la liste
   blanche, le code extrait les arguments). **Le texte du LLM n'est jamais exécuté** : ce n'est
   qu'une nouvelle phrase soumise aux mêmes règles.
4. tout le plan est évalué avant d'agir ; rien n'est exécuté si une étape est invalide ; une
   seule confirmation couvre le plan si une étape est sensible.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from .decide import Verdict
from .pipeline import Launcher, Outcome

# ----------------------------------------------------------------------------- découpage par règles
ACTION_VERBS = (
    "ouvre", "ouvrir", "lance", "lancer", "démarre", "cherche", "recherche", "trouve", "mets", "met",
    "joue", "like", "aime", "ajoute", "enregistre", "sauvegarde", "ferme", "quitte", "monte", "baisse",
    "coupe", "remets", "passe", "va", "affiche", "montre", "crée", "écris", "rédige", "note", "envoie",
    "active", "désactive", "vide", "verrouille", "fais", "prends", "éteins", "allume", "reprends",
    "arrête", "zappe", "supprime", "lis", "change", "bascule", "donne", "dis", "raconte",
)
_CONNECTOR = re.compile(r"\s*(?:,\s*(?:et\s+|puis\s+)?|\s(?:et\s+puis|puis|ensuite|et\s+après|et|après)\s+)", re.I)
_VERB = re.compile(r"(?:" + "|".join(ACTION_VERBS) + r")(?:[-'’]\w+)*\b", re.I)


def split_rules(text: str) -> list[str]:
    """Coupe sur « et / puis / ensuite / , » seulement si la suite commence par un verbe d'action."""
    text = " ".join(text.replace("’", "'").split()).strip(" .!?")
    parts, start = [], 0
    for m in _CONNECTOR.finditer(text):
        rest = text[m.end():]
        if m.start() > start and _VERB.match(rest):
            parts.append(text[start:m.start()].strip(" ,"))
            start = m.end()
    parts.append(text[start:].strip(" ,"))
    return _resolve_context([p for p in parts if p])


_PRONOUN = re.compile(r"^(?P<verb>\w+)-(?:la|le|les|lui)\b\s*(?P<rest>.*)$", re.I)
_OBJECT = re.compile(r"^\w+(?:\s+-?moi)?\s+(?P<obj>.+?)(?:\s+(?:sur|dans|avec)\s+\S+)?$", re.I)
_APP_CTX = re.compile(r"(?:^ouvr\w*\s+(?:l'app(?:lication)?\s+)?|\s(?:sur|dans)\s+)(?P<app>[A-Za-zÀ-ÿ][\w-]*)\s*$", re.I)
_CONTEXT_VERBS = re.compile(r"^(?:cherche|recherche|trouve|mets|joue|lance|like|aime|ajoute|écoute)\b", re.I)


def _resolve_context(parts: list[str]) -> list[str]:
    """Rend chaque étape autonome, comme le demanderait le LLM :
    « lance-la » -> « lance Get Lucky » ; après « ouvre Spotify », « cherche X » -> « cherche X sur Spotify »."""
    out: list[str] = []
    obj = app = None
    for part in parts:
        m = _PRONOUN.match(part)
        if m and m.group("verb").lower() in ("like", "aime", "like-la"):
            part = f"{m.group('verb')} le morceau en cours" + (f" {m.group('rest')}" if m.group("rest") else "")
        elif m and obj:
            part = f"{m.group('verb')} {obj}" + (f" {m.group('rest')}" if m.group("rest") else "")
        if app and _CONTEXT_VERBS.match(part) and not re.search(r"\s(?:sur|dans)\s+\S+", part):
            part = f"{part} sur {app}"
        ctx = _APP_CTX.search(part)
        if ctx:
            app = ctx.group("app")
        o = _OBJECT.match(part)
        if o and not part.lower().startswith(("ouvr", "passe", "active")):
            obj = o.group("obj")
        out.append(part)
    return out


# ----------------------------------------------------------------------------- découpage par LLM
SPLIT_PROMPT = """Tu découpes une commande vocale française, destinée à un assistant macOS, en étapes atomiques.

Règles :
- Une étape = une seule action, formulée comme un ordre court à l'impératif.
- Garde l'ordre et le sens exacts. N'ajoute AUCUNE action qui n'est pas demandée.
- Chaque étape doit se comprendre seule : remplace les pronoms par ce qu'ils désignent
  (« cherche Get Lucky puis lance-la » -> « lance Get Lucky ») et garde le contexte d'app
  (« ouvre Spotify et cherche Daft Punk » -> « cherche Daft Punk dans Spotify »).
- Ne cite JAMAIS une application, un site ou un navigateur qui n'apparaît pas dans la phrase.
- Réutilise les MOTS de la phrase, surtout les verbes (« cherche » reste « cherche », jamais « ouvre ») ;
  ne reformule pas, ne résume pas.
- Si la phrase ne demande qu'une seule action, renvoie une seule étape.
- Au plus {max_steps} étapes.

Réponds UNIQUEMENT avec un objet JSON : {{"steps": ["...", "..."]}}"""


_ADDED_CONTEXT = re.compile(r"\s+(?:dans|sur|avec|via)\s+([A-ZÀ-Ö][\w'.-]*(?:\s+[A-ZÀ-Ö][\w'.-]*)*)\s*$")


def drop_invented_context(steps: list[str], original: str) -> list[str]:
    """Garde-fou : retire « dans Safari » & co. si l'app n'était pas dans la phrase d'origine."""
    low = original.lower()
    out = []
    for step in steps:
        m = _ADDED_CONTEXT.search(step)
        if m and m.group(1).lower() not in low:
            step = step[: m.start()].rstrip()
        out.append(step)
    return out


class LLMSplitter:
    """Découpage via OpenRouter (API compatible OpenAI). Renvoie None en cas d'échec."""

    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str, timeout: float = 6.0):
        self.api_key, self.model, self.timeout = api_key, model, timeout

    def split(self, text: str, max_steps: int) -> list[str] | None:
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 400,
            "reasoning": {"enabled": False},
            "messages": [
                {"role": "system", "content": SPLIT_PROMPT.format(max_steps=max_steps)},
                {"role": "user", "content": text},
            ],
        }
        req = urllib.request.Request(self.URL, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
            "X-Title": "voxjev",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content = json.loads(resp.read())["choices"][0]["message"]["content"]
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as exc:
            print(f"  découpage LLM indisponible ({exc}) : repli sur les règles")
            return None
        return parse_steps(content, max_steps)


def parse_steps(content: str, max_steps: int) -> list[str] | None:
    """Valide la sortie du LLM : un objet JSON {"steps": [chaînes courtes]}."""
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        return None
    try:
        steps = json.loads(m.group(0)).get("steps")
    except (ValueError, AttributeError):
        return None
    if not isinstance(steps, list) or not steps or not all(isinstance(x, str) for x in steps):
        return None
    steps = [" ".join(x.split())[:200] for x in steps if x.strip()]
    return steps[:max_steps] or None


class Splitter:
    def __init__(self, llm: LLMSplitter | None = None):
        self.llm = llm

    @property
    def kind(self) -> str:
        return f"LLM ({self.llm.model})" if self.llm else "règles"

    def split(self, text: str, max_steps: int) -> list[str]:
        if self.llm:
            steps = self.llm.split(text, max_steps)
            steps = drop_invented_context(steps, text) if steps else steps
            if steps:
                return steps
        return split_rules(text)[:max_steps]


# ----------------------------------------------------------------------------- plan
@dataclass
class PlanOutcome:
    transcript: str
    parts: list[str] = field(default_factory=list)
    items: list[Outcome] = field(default_factory=list)
    status: str = "ignored"  # executed | dry_run | cancelled | error | ignored
    error: str | None = None
    splitter: str = ""
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def command_id(self) -> str:
        """Pour l'historique : « open_app+volume_up »."""
        return "+".join(o.command_id or "—" for o in self.items)

    @property
    def runnable(self) -> list[Outcome]:
        return [o for o in self.items if o.status in ("planned", "executed", "dry_run")]

    @property
    def dropped(self) -> list[Outcome]:
        return [o for o in self.items if o.status == "ignored"]

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.dropped) or any(o.decision.verdict == Verdict.CONFIRM for o in self.runnable)

    @property
    def destructive(self) -> bool:
        return any(o.decision.destructive for o in self.runnable)


PlanConfirmer = Callable[[PlanOutcome], bool]


def _single_covers(single: Outcome, items: list[Outcome]) -> bool:
    """« ouvre Chrome et cherche X » est UNE commande (recherche dans Chrome) : si le découpage
    ne fait qu'ajouter l'ouverture d'une app déjà utilisée par la commande unique, on garde celle-ci."""
    if single.status != "planned" or single.decision.verdict != Verdict.EXECUTE:
        return False
    single_args = single.args.values if single.args else {}
    used_apps = set(single_args.values())
    for o in items:
        if o.status != "planned":
            return False  # une étape invalide ne doit jamais disparaître silencieusement
        cid = o.command_id
        if cid == single.command_id and o.args and all(single_args.get(k) == v for k, v in o.args.values.items()):
            continue
        if cid == "open_app" and o.args and o.args.values.get("app") in used_apps:
            continue
        return False
    return True


class MultiRunner:
    """Point d'entrée unique : énoncé simple ou composé."""

    def __init__(self, launcher: Launcher, splitter: Splitter, confirm_plan: PlanConfirmer | None = None,
                 on_plan: Callable[[PlanOutcome], None] | None = None):
        self.launcher = launcher
        self.splitter = splitter
        self.confirm_plan = confirm_plan
        self.on_plan = on_plan  # notifié quand un plan est prêt (affichage)

    def handle(self, text: str) -> Outcome | PlanOutcome:
        s = self.launcher.config.settings
        first = self.launcher.plan(text)
        r = first.result
        if not r or r.compound < s.compound_threshold:
            return self.launcher.finish(first)
        t0 = time.perf_counter()
        parts = self.splitter.split(text, s.max_plan_steps)
        split_ms = (time.perf_counter() - t0) * 1000
        if len(parts) < 2:
            return self.launcher.finish(first)
        plan = PlanOutcome(transcript=text, parts=parts, splitter=self.splitter.kind)
        plan.items = self._plan_parts(parts)
        plan.timings = {"jev_detect_ms": r.latency_ms, "split_ms": split_ms,
                        "plan_ms": (time.perf_counter() - t0) * 1000 - split_ms}
        if _single_covers(first, plan.items):
            return self.launcher.finish(first)
        return self._run(plan)

    # ---------------------------------------------------------------- planification
    def _plan_parts(self, parts: list[str]) -> list[Outcome]:
        mode = self.launcher.session.mode
        # « …puis ouvre le premier résultat » : la page n'existe pas encore au moment du plan,
        # le lien sera choisi à l'exécution, une fois la page chargée.
        self.launcher.defer_page_links = True
        try:
            with ThreadPoolExecutor(max_workers=len(parts)) as pool:
                items = list(pool.map(lambda p: self.launcher.plan(p, mode=mode), parts))
        finally:
            self.launcher.defer_page_links = False
        # Une étape qui change de mode : les suivantes sont replanifiées dans le nouveau mode.
        for i, item in enumerate(items):
            new_mode = next((st.mode for st in item.steps if st.kind == "set_mode"), None)
            if new_mode and new_mode != mode:
                mode = new_mode
                for j in range(i + 1, len(items)):
                    items[j] = self.launcher.plan(parts[j], mode=mode)
        return items

    # ---------------------------------------------------------------- exécution
    def _run(self, plan: PlanOutcome) -> PlanOutcome:
        failed = next((i for i, o in enumerate(plan.items) if o.status == "error"), None)
        if failed is not None:
            plan.status = "error"
            plan.error = f"étape {failed + 1} « {plan.parts[failed]} » : {plan.items[failed].error}"
            return plan
        if not plan.runnable:
            plan.status = "ignored"
            return plan
        if self.on_plan:
            self.on_plan(plan)
        if self.launcher.dry_run:
            for o in plan.runnable:
                o.status = "dry_run"
            plan.status = "dry_run"
            return plan
        if plan.needs_confirmation and not (self.confirm_plan and self.confirm_plan(plan)):
            plan.status = "cancelled"
            return plan
        s = self.launcher.config.settings
        runnable = plan.runnable
        for k, item in enumerate(runnable):
            self.launcher.execute(item)
            if item.status != "executed":
                plan.status = "error"
                plan.error = f"étape « {item.transcript} » : {item.error}"
                for rest in runnable[k + 1:]:
                    rest.status = "skipped"
                return plan
            opened_app = any(st.argv[:2] == ("open", "-a") for st in item.steps)
            if opened_app and k + 1 < len(runnable):
                time.sleep(s.step_delay_seconds)  # laisser l'app s'ouvrir avant l'étape suivante
        plan.status = "executed"
        return plan


def build_splitter(settings) -> Splitter:
    import os

    key = os.environ.get("OPENROUTER_API_KEY")
    return Splitter(LLMSplitter(key, settings.split_model) if key else None)
