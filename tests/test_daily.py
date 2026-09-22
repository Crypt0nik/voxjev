"""Tests hors ligne des fonctions « quotidien » : dates, candidats choisis par Jev, routines, garde-fous."""

from __future__ import annotations

import textwrap
from datetime import datetime

import pytest

from voxjev.actions import ActionError, keycombo_argv, plan_command
from voxjev.axmenu import MenuItem
from voxjev.candidates import Provider, spans
from voxjev.config import ConfigError, load_config
from voxjev.context import Session
from voxjev.decide import Verdict
from voxjev.jev_client import NONE, FakeJevClient, JevResult, build_questions
from voxjev.pipeline import Launcher
from voxjev.when import parse_duration, parse_when

APPS = ("Google Chrome", "Safari", "Mail", "Calendar", "Notes", "Finder", "Ghostty")
NOW = datetime(2026, 9, 22, 15, 10)  # un mardi


@pytest.fixture(scope="module")
def config():
    return load_config()


class Recorder:
    def __init__(self):
        self.runs = []
        self.client = None
        self.settings = None

    def run(self, steps):
        self.runs.append(steps)
        return ["ok"]


def res(cmd, p=0.97, addressed=0.95, destructive=0.03, picks=None):
    return JevResult(cmd, {cmd: p, NONE: 1 - p}, p, addressed, destructive, picks=picks or {})


def launcher(config, responses, *, menus=(), shortcuts=(), executor=None, confirmer=None):
    provider = Provider(menu_reader=lambda pid: list(menus), shortcut_lister=lambda: list(shortcuts))
    return Launcher(config, FakeJevClient(responses=responses), Session(mode="defaut", path=None), executor=executor,
                    confirmer=confirmer, frontmost=lambda: "Safari", apps=APPS, provider=provider)


# ------------------------------------------------------------------ dates et durées
@pytest.mark.parametrize("text,seconds", [
    ("mets un minuteur de 10 minutes", 600), ("minuteur 1h30", 5400), ("une demi-heure", 1800),
    ("vingt-cinq minutes", 1500), ("2 minutes et 30 secondes", 150), ("une heure et demie", 5400),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text,expected,rest", [
    ("rappelle-moi d'appeler Paul demain à 18h", datetime(2026, 9, 23, 18, 0), "rappelle-moi d'appeler Paul"),
    ("sortir le linge dans 20 minutes", datetime(2026, 9, 22, 15, 30), "sortir le linge"),
    ("réunion lundi à 14h30", datetime(2026, 9, 28, 14, 30), "réunion"),
    ("dentiste le 12 octobre à 9 heures et quart", datetime(2026, 10, 12, 9, 15), "dentiste"),
    ("appel à 8h du soir", datetime(2026, 9, 22, 20, 0), "appel"),
    ("appeler maman à midi", datetime(2026, 9, 23, 12, 0), "appeler maman"),
    ("appeler un ami", None, None),
])
def test_parse_when(text, expected, rest):
    w = parse_when(text, NOW)
    if expected is None:
        assert w is None
    else:
        assert w.at == expected and w.rest == rest


# ------------------------------------------------------------------ candidats
def test_spans_select_rather_than_generate():
    s = spans("mets la chanson Bohemian Rhapsody de Queen sur Spotify")
    assert "Bohemian Rhapsody" in s and "Bohemian Rhapsody de Queen" in s
    assert all(not x.startswith(("de ", "la ", "sur ")) for x in s)
    assert spans("écris « je serai en retard » dans le message")[0] == "je serai en retard"


def test_pick_questions_are_added_with_none(config):
    cands = Provider(menu_reader=lambda pid: [MenuItem(("Fichier", "Exporter au format PDF…"))],
                     shortcut_lister=lambda: ["Créer un code QR"]).gather({"menu", "shortcut", "span"}, "exporte en PDF")
    q = build_questions(config.commands_for_mode("defaut"), config.settings.none_option, cands)
    assert set(q["pick_menu"]["criteria"]) == {"m0", NONE}
    assert q["pick_shortcut"]["criteria"]["s0"]["what"] == "Créer un code QR"
    assert "pick_span" in q


def test_menu_item_uses_observed_path_and_confirms_destructive(config):
    menus = [MenuItem(("Fichier", "Exporter au format PDF…")), MenuItem(("Fichier", "Supprimer la page"))]
    ex = Recorder()
    asked = []
    lz = launcher(config, {"exporte en PDF": res("menu_item", picks={"menu": (0, 0.9)}),
                           "supprime la page": res("menu_item", picks={"menu": (1, 0.9)})},
                  menus=menus, executor=ex, confirmer=lambda *a: asked.append(a) or False)
    out = lz.handle("exporte en PDF")
    assert out.status == "executed" and ex.runs[-1][0].kind == "menu"
    assert ex.runs[-1][0].argv == ("Fichier", "Exporter au format PDF…")
    out = lz.handle("supprime la page")
    assert out.decision.verdict == Verdict.CONFIRM and out.decision.destructive and out.status == "cancelled"


def test_uncertain_pick_is_refused(config):
    lz = launcher(config, {"exporte en PDF": res("menu_item", picks={"menu": (0, 0.3)})},
                  menus=[MenuItem(("Fichier", "Exporter…"))])
    out = lz.plan("exporte en PDF")
    assert out.status == "error" and "item" in out.error


def test_shortcut_must_exist(config, monkeypatch):
    import voxjev.candidates as cand

    monkeypatch.setattr(cand, "list_shortcuts", lambda: ["Créer un code QR"])
    lz = launcher(config, {"lance le raccourci code QR": res("run_shortcut", picks={"shortcut": (0, 0.95)})},
                  shortcuts=["Créer un code QR"])
    out = lz.plan("lance le raccourci code QR")
    assert out.steps[0].argv == ("shortcuts", "run", "Créer un code QR")
    monkeypatch.setattr(cand, "list_shortcuts", lambda: [])
    assert lz.plan("lance le raccourci code QR").status == "error"


def test_span_fallback_when_regex_fails(config):
    # « cherche » absent : la regex de web_search échoue, Jev choisit le segment
    text = "la météo à Lyon sur Google"
    lz = launcher(config, {text: res("web_search")})
    idx = spans(text).index("la météo à Lyon") if "la météo à Lyon" in spans(text) else spans(text).index("météo à Lyon")
    lz.client.responses[text].picks = {"span": (idx, 0.9)}
    out = lz.plan(text)
    assert out.status == "planned" and "Lyon" in out.args.values["query"] and out.args.sources["query"] == "jev"


# ------------------------------------------------------------------ quotidien
def test_reminder_title_and_date_pass_as_argv(config):
    lz = launcher(config, {"rappelle-moi d'appeler Paul demain à 18h": res("reminder_add")})
    out = lz.plan("rappelle-moi d'appeler Paul demain à 18h")
    argv = out.steps[0].argv
    assert argv[0] == "osascript" and "appeler Paul" in argv and "18" in argv
    assert not any("appeler Paul" in a for a in argv if a.startswith("tell"))  # jamais dans le script
    assert out.args.display["when"].startswith("demain à 18")


def test_timer_and_label(config):
    lz = launcher(config, {"mets un minuteur de 3 minutes pour les pâtes": res("set_timer")})
    out = lz.plan("mets un minuteur de 3 minutes pour les pâtes")
    assert out.steps[0].kind == "timer" and out.steps[0].argv == ("180", "pâtes")


def test_keycombo_grammar(config):
    assert keycombo_argv("cmd+shift+t")[-1].endswith("using {command down, shift down}")
    assert keycombo_argv("code:121")[-1].endswith("key code 121")
    with pytest.raises(ActionError):
        keycombo_argv('cmd+" & do shell script "rm')


def test_type_text_refused_in_terminal(config):
    from voxjev.actions import Step
    from voxjev.executor import SubprocessExecutor

    ex = SubprocessExecutor(settings=config.settings, speak_answers=False)
    ex._front = lambda: ("Ghostty", 1)
    with pytest.raises(ActionError, match="terminal"):
        ex.run([Step("type", ("rm -rf ~", "1"))])


def test_routine_expands_to_fixed_steps(config):
    cmd = config.commands["routine_concentration"]
    steps = plan_command(cmd, {}, config, APPS)
    kinds = [s.kind for s in steps]
    assert kinds[0] == "set_mode" and "timer" in kinds and steps[-1].argv[0] == "1500"


def test_routine_validation(tmp_path):
    base = textwrap.dedent("""
    modes: {defaut: {commands: []}}
    common: []
    commands:
      - id: x
        description: d
        examples: [e]
        destructive: true
        action: {type: exec, argv: [pmset, displaysleepnow]}
    settings: {exec_allowlist: [pmset]}
    routines:
      - id: r
        description: d
        examples: [e]
        steps: STEPS
    """)
    p = tmp_path / "c.yaml"
    p.write_text(base.replace("STEPS", "[{run: x}]"))
    assert load_config(p).commands["r"].destructive  # une étape destructive rend la routine destructive
    for bad in ("[{run: inconnue}]", "[{run: x, with: {z: 1}}]", "[{wait: 99}]", "[]"):
        p.write_text(base.replace("STEPS", bad))
        with pytest.raises(ConfigError):
            load_config(p)


def test_config_rejects_bad_typed_fields(tmp_path):
    body = textwrap.dedent("""
    modes: {defaut: {commands: []}}
    common: [x]
    commands:
      - id: x
        description: d
        examples: [e]
        destructive: false
        args: ARGS
        action: ACTION
    """)
    p = tmp_path / "c.yaml"
    cases = [
        ("{t: {type: text, patterns: ['(?P<t>.+)']}}", "{type: menu, item: '{t}'}"),         # menu exige pick
        ("{t: {type: text, patterns: ['(?P<t>.+)']}}", "{type: timer, seconds: '{t}'}"),     # timer exige duration
        ("{}", "{type: keycombo, combo: 'cmd+q; rm'}"),                                     # grammaire
        ("{t: {type: pick, source: shortcut}}", "{type: menu, item: '{t}'}"),               # mauvaise source
    ]
    for args, action in cases:
        p.write_text(body.replace("ARGS", args).replace("ACTION", action))
        with pytest.raises(ConfigError):
            load_config(p)


def test_memory_roundtrip(tmp_path):
    from voxjev.memory import Memory

    m = Memory(tmp_path / "mem.json")
    m.add("mon dentiste c'est le docteur Martin")
    m.add("le code du portail est 1234")
    fact, p = m.find(FakeJevClient(), "c'est qui mon dentiste")
    assert fact == "mon dentiste c'est le docteur Martin"
    assert m.remove(fact) and len(m.load()) == 1
    assert oct((tmp_path / "mem.json").stat().st_mode)[-3:] == "600"


def test_file_query_is_sanitized(monkeypatch):
    import voxjev.files as files

    seen = []
    monkeypatch.setattr(files, "_mdfind", lambda q, home: seen.append(q) or [])
    files.search('rapport" || kMDItemFSName == "*')
    assert seen and all('"*rapport*"' in q and "||" not in q.split("&&")[0] for q in seen[:1])
    assert all(c not in q.replace('"*', "").replace('*"', "") for q in seen for c in ['"'])


def test_triage_one_call_two_nouls_per_mail():
    from voxjev.apple import Mail, describe_triage, triage

    class C:
        def nouls(self, state, questions):
            assert set(state) == {"m0", "m1"} and set(questions) == {"a0", "i0", "a1", "i1"}
            return {"a0": 0.9, "i0": 0.8, "a1": 0.1, "i1": 0.2}

    rows = triage(C(), [Mail("Paul <p@x.fr>", "Signature du contrat", "peux-tu signer"), Mail("Promo", "Soldes", "…")])
    text = describe_triage(rows)
    assert "1 demande une action" in text and "Paul — Signature du contrat" in text
