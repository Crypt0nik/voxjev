"""Runner d'évaluation : `voxjev --eval [cases.tsv]`.

Format de cases.tsv (tabulations, lignes # ignorées) :
    phrase <TAB> attendu <TAB> [mode]
où « attendu » vaut un id de commande, « none » (rien ne doit se déclencher), ou une séquence
« open_app>volume_up » pour une demande composée.

Chaque phrase passe par le vrai point d'entrée (MultiRunner, avec détection des demandes
composées) en dry-run : une phrase simple découpée à tort compte comme une erreur. Le contexte est
figé (app au premier plan = Finder, pas de dernière commande) pour des résultats reproductibles.
"""

from __future__ import annotations

import csv
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .context import Session, installed_apps
from .multi import MultiRunner, PlanOutcome, build_splitter
from .pipeline import Launcher, Outcome


@dataclass
class Case:
    line: int
    phrase: str
    expected: str
    mode: str

    @property
    def is_plan(self) -> bool:
        return ">" in self.expected


@dataclass
class Summary:
    """Ce que l'évaluation retient d'un énoncé, simple ou composé."""

    got: str  # id, « none », ou séquence « a>b »
    verdict: str  # execute | confirm | ignore | plan | error
    p: float | None = None
    addressed: float | None = None
    destructive: float | None = None
    latency_ms: float = 0.0
    tokens: int = 0
    args: str = ""
    args_ok: bool = True
    error: str | None = None
    api_error: bool = False
    model: str = ""
    detail: list[str] = field(default_factory=list)


def summarize(out: Outcome | PlanOutcome) -> Summary:
    if isinstance(out, PlanOutcome):
        ids = [o.command_id or "?" for o in out.runnable]
        results = [o.result for o in out.items if o.result]
        s = Summary(
            got=">".join(ids) if out.status != "error" and ids else ("error" if out.status == "error" else "none"),
            verdict="plan" if out.status != "error" else "error",
            p=min((r.p_command for r in results), default=None),
            latency_ms=out.timings.get("jev_detect_ms", 0) + out.timings.get("plan_ms", 0),
            tokens=sum(r.input_tokens or 0 for r in results),
            error=out.error, model=results[0].model if results else "",
            detail=[f"« {part} » → {o.command_id or o.status}" for part, o in zip(out.parts, out.items)],
        )
        return s
    r = out.result
    got = out.command_id if out.triggered else "none"
    return Summary(
        got=got,
        verdict=out.decision.verdict.value if out.decision else "error",
        p=r.p_command if r else None, addressed=r.addressed if r else None,
        destructive=r.destructive if r else None, latency_ms=r.latency_ms if r else 0.0,
        tokens=r.input_tokens or 0 if r else 0,
        args=", ".join(f"{k}={v}" for k, v in out.args.values.items()) if out.args and out.args.values else "",
        args_ok=out.args is None or out.args.ok, error=out.error,
        api_error=out.status == "error" and r is None, model=r.model if r else "",
    )


def load_cases(path: Path, config: Config) -> list[Case]:
    cases = []
    with path.open(encoding="utf-8") as f:
        for i, row in enumerate(csv.reader(f, delimiter="\t"), start=1):
            if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
                continue
            if len(row) < 2:
                raise ValueError(f"{path}:{i}: il faut au moins 2 colonnes (phrase, attendu)")
            mode = row[2].strip() if len(row) > 2 and row[2].strip() else config.default_mode
            expected = row[1].strip()
            if mode not in config.modes:
                raise ValueError(f"{path}:{i}: mode inconnu {mode!r}")
            known = set(config.commands)
            for cid in expected.split(">"):
                if cid != "none" and cid not in known:
                    raise ValueError(f"{path}:{i}: commande inconnue {cid!r}")
            cases.append(Case(i, row[0].strip(), expected, mode))
    return cases


def classify(case: Case, s: Summary) -> str:
    if s.api_error:
        return "api_error"
    if case.expected == "none":
        return "ok" if s.got == "none" else "false_trigger"
    if s.got in ("none", "error"):
        return "missed"
    if s.got != case.expected:
        return "wrong_command"
    if not s.args_ok:
        return "missing_args"
    return "ok"


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def run_eval(path: Path, config: Config, client, workers: int = 6, verbose: bool = True) -> int:
    cases = load_cases(path, config)
    apps = installed_apps(config.settings.app_dirs)
    splitter = build_splitter(config.settings)

    def run(case: Case) -> Summary:
        session = Session(mode=case.mode, path=None)
        launcher = Launcher(config, client, session, dry_run=True, frontmost=lambda: "Finder", apps=apps)
        return summarize(MultiRunner(launcher, splitter).handle(case.phrase))

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        summaries = list(pool.map(run, cases))
    wall = time.perf_counter() - started
    labels = [classify(c, s) for c, s in zip(cases, summaries)]

    if verbose:
        print(f"{'':2}{'attendu':<24}{'obtenu':<24}{'décision':<9}{'p':>5}{'adr':>6}{'destr':>6}{'ms':>6}  phrase")
        for c, s, lab in zip(cases, summaries, labels):
            mark = "✓" if lab == "ok" else "✗"
            nums = "".join(f"{v:>{w}.2f}" if v is not None else " " * w
                           for v, w in ((s.p, 5), (s.addressed, 6), (s.destructive, 6))) + f"{s.latency_ms:>6.0f}"
            extra = f"  <- {lab}" + (f" ({s.error})" if s.error else "") if lab != "ok" else ""
            shown = f"  [{s.args}]" if s.args else ""
            print(f"{mark} {c.expected[:23]:<24}{s.got[:23]:<24}{s.verdict:<9}{nums}  {c.phrase}{extra}{shown}")
            if s.detail and lab != "ok":
                for d in s.detail:
                    print(f"      {d}")

    n = len(cases)
    ok = labels.count("ok")
    single = [(c, s, lab) for c, s, lab in zip(cases, summaries, labels) if not c.is_plan]
    plans = [(c, s, lab) for c, s, lab in zip(cases, summaries, labels) if c.is_plan]
    positives = [(c, s, lab) for c, s, lab in single if c.expected != "none"]
    negatives = [(c, s, lab) for c, s, lab in single if c.expected == "none"]
    triggered = [(c, s, lab) for c, s, lab in single if s.got != "none"]
    correct = sum(1 for c, s, _ in triggered if s.got == c.expected)
    false_triggers = [s for _, s, lab in negatives if lab == "false_trigger"]
    false_exec = sum(1 for s in false_triggers if s.verdict == "execute")
    latencies = [s.latency_ms for s in summaries if s.latency_ms]
    tokens = [s.tokens for s in summaries if s.tokens]
    model = next((s.model for s in summaries if s.model), "?")

    print(f"\n=== Résultats ({path.name}, {n} cas, modèle {model}, découpage : {splitter.kind})")
    print(f"  exactitude globale     : {ok}/{n} = {ok / n:.1%}")
    if triggered:
        print(f"  précision (déclenchés) : {correct}/{len(triggered)} = {correct / len(triggered):.1%}")
    if positives:
        found = sum(1 for _, _, lab in positives if lab in ("ok", "missing_args"))
        print(f"  rappel (commandes)     : {found / len(positives):.1%}  ({[lab for *_, lab in positives].count('missed')} "
              f"ratés, {[lab for *_, lab in positives].count('wrong_command')} mauvaise commande, "
              f"{[lab for *_, lab in positives].count('missing_args')} arguments non extraits)")
    if negatives:
        print(f"  faux déclenchements    : {len(false_triggers)}/{len(negatives)} phrases « none » = "
              f"{len(false_triggers) / len(negatives):.1%}  (dont {false_exec} exécutés directement, "
              f"{len(false_triggers) - false_exec} avec confirmation demandée)")
    if plans:
        good = sum(1 for *_, lab in plans if lab == "ok")
        print(f"  demandes composées     : {good}/{len(plans)} plans exacts (bonne séquence de commandes)")
    if latencies:
        print(f"  latence Jev            : p50 {statistics.median(latencies):.0f} ms · p95 {_percentile(latencies, 0.95):.0f} ms"
              f" · max {max(latencies):.0f} ms  (parallélisme {workers}, total {wall:.1f} s)")
    if tokens:
        print(f"  tokens d'entrée        : {statistics.mean(tokens):.0f} en moyenne par énoncé")
    if labels.count("api_error"):
        print(f"  erreurs API            : {labels.count('api_error')}")
    return 0 if ok == n else 1
