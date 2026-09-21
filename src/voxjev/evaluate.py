"""Runner d'évaluation : `voxjev --eval [cases.tsv]`.

Format de cases.tsv (tabulations, lignes # ignorées) :
    phrase <TAB> id attendu | none <TAB> [mode]

Un cas « déclenche » si la décision est EXECUTE ou CONFIRM. Rien n'est jamais exécuté
(dry-run), et le contexte est figé (app au premier plan = Finder, pas de dernière commande)
pour que les résultats soient reproductibles.
"""

from __future__ import annotations

import csv
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .context import Session, installed_apps
from .pipeline import Launcher, Outcome


@dataclass
class Case:
    line: int
    phrase: str
    expected: str
    mode: str


def load_cases(path: Path, config: Config) -> list[Case]:
    cases = []
    with path.open(encoding="utf-8") as f:
        for i, row in enumerate(csv.reader(f, delimiter="\t"), start=1):
            if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
                continue
            if len(row) < 2:
                raise ValueError(f"{path}:{i}: il faut au moins 2 colonnes (phrase, id)")
            mode = row[2].strip() if len(row) > 2 and row[2].strip() else config.default_mode
            expected = row[1].strip()
            if mode not in config.modes:
                raise ValueError(f"{path}:{i}: mode inconnu {mode!r}")
            if expected != "none" and expected not in {c.id for c in config.commands_for_mode(mode)}:
                raise ValueError(f"{path}:{i}: commande {expected!r} absente du mode {mode!r}")
            cases.append(Case(i, row[0].strip(), expected, mode))
    return cases


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def classify(case: Case, out: Outcome) -> str:
    got = out.command_id if out.triggered else "none"
    if out.status == "error" and out.result is None:
        return "api_error"
    if case.expected == "none":
        return "ok" if got == "none" else "false_trigger"
    if got == "none":
        return "missed"
    if got != case.expected:
        return "wrong_command"
    if out.args is not None and not out.args.ok:
        return "missing_args"
    return "ok"


def run_eval(path: Path, config: Config, client, workers: int = 6, verbose: bool = True) -> int:
    cases = load_cases(path, config)
    apps = installed_apps(config.settings.app_dirs)

    def run(case: Case) -> Outcome:
        session = Session(mode=case.mode, path=None)
        launcher = Launcher(config, client, session, dry_run=True, frontmost=lambda: "Finder", apps=apps)
        return launcher.handle(case.phrase)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        outcomes = list(pool.map(run, cases))
    wall = time.perf_counter() - started

    labels = [classify(c, o) for c, o in zip(cases, outcomes)]
    positives = [(c, o, l) for c, o, l in zip(cases, outcomes, labels) if c.expected != "none"]
    negatives = [(c, o, l) for c, o, l in zip(cases, outcomes, labels) if c.expected == "none"]
    triggered = [(c, o, l) for c, o, l in zip(cases, outcomes, labels) if o.triggered]
    latencies = [o.result.latency_ms for o in outcomes if o.result]
    tokens = [o.result.input_tokens for o in outcomes if o.result and o.result.input_tokens]

    if verbose:
        print(f"{'':2}{'attendu':<18}{'obtenu':<18}{'décision':<9}{'p':>5}{'adr':>6}{'destr':>6}{'ms':>6}  phrase")
        for c, o, l in zip(cases, outcomes, labels):
            r = o.result
            mark = "✓" if l == "ok" else "✗"
            got = r.command if r else "ERR"
            verdict = o.decision.verdict.value if o.decision else o.status
            nums = f"{r.p_command:>5.2f}{r.addressed:>6.2f}{r.destructive:>6.2f}{r.latency_ms:>6.0f}" if r else " " * 23
            extra = f"  <- {l}" + (f" ({o.error})" if o.error else "") if l != "ok" else ""
            shown = "  [" + ", ".join(f"{k}={v}" for k, v in o.args.values.items()) + "]" if o.args and o.args.values else ""
            print(f"{mark} {c.expected:<18}{got:<18}{verdict:<9}{nums}  {c.phrase}{extra}{shown}")

    n = len(cases)
    ok = labels.count("ok")
    correct_triggers = sum(1 for c, o, l in triggered if o.command_id == c.expected)
    precision = correct_triggers / len(triggered) if triggered else 1.0
    recall = sum(1 for c, o, l in positives if l in ("ok", "missing_args")) / len(positives) if positives else 1.0
    false_triggers = sum(1 for _, _, l in negatives if l == "false_trigger")
    false_exec = sum(1 for _, o, l in negatives if l == "false_trigger" and o.decision.verdict.value == "execute")
    confirms = sum(1 for _, o, _ in triggered if o.decision and o.decision.verdict.value == "confirm")

    print("\n=== Résultats", f"({path.name}, {n} cas, modèle {outcomes[0].result.model if outcomes and outcomes[0].result else '?'})")
    print(f"  exactitude globale     : {ok}/{n} = {ok / n:.1%}")
    print(f"  précision (déclenchés) : {correct_triggers}/{len(triggered)} = {precision:.1%}")
    print(f"  rappel (commandes)     : {recall:.1%}  ({labels.count('missed')} ratés, "
          f"{labels.count('wrong_command')} mauvaise commande, {labels.count('missing_args')} arguments non extraits)")
    print(f"  faux déclenchements    : {false_triggers}/{len(negatives)} phrases « none » = "
          f"{false_triggers / len(negatives) if negatives else 0:.1%}  "
          f"(dont {false_exec} exécutés directement, {false_triggers - false_exec} avec confirmation demandée)")
    print(f"  confirmations          : {confirms}/{len(triggered)} déclenchements")
    if latencies:
        print(f"  latence Jev            : p50 {statistics.median(latencies):.0f} ms · p95 {_percentile(latencies, 0.95):.0f} ms"
              f" · max {max(latencies):.0f} ms  (parallélisme {workers}, total {wall:.1f} s)")
    if tokens:
        print(f"  tokens d'entrée        : {statistics.mean(tokens):.0f} en moyenne par appel")
    if labels.count("api_error"):
        print(f"  erreurs API            : {labels.count('api_error')}")
    return 0 if ok == n else 1
