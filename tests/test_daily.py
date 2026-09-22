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


# ------------------------------------------------------------------ mains libres et anticipation
def test_hands_free_vad_cuts_one_utterance(monkeypatch):
    import sys
    import types

    import numpy as np

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace())
    from voxjev.audio import HandsFree

    clips = []
    hf = HandsFree(on_clip=lambda audio, dur: clips.append(dur))
    block = HandsFree.BLOCK
    rng = np.random.default_rng(0)
    feed = lambda amp, seconds: [hf._callback((rng.standard_normal((block, 1)) * amp).astype("float32"), block, None, None)
                                 for _ in range(int(seconds * 16000 / block))]
    feed(0.002, 1.0)   # bruit de fond
    feed(0.2, 1.2)     # voix
    feed(0.002, 1.0)   # silence -> fin de phrase
    feed(0.2, 0.1)     # clic isolé : trop court
    feed(0.002, 1.0)
    assert len(clips) == 1 and 1.2 <= clips[0] <= 2.2


def test_wake_word():
    from voxjev.audio import strip_wake_word

    assert strip_wake_word("Hey Jarvis, ouvre Chrome", ("jarvis",)) == (True, "ouvre Chrome")
    assert strip_wake_word("le film Jarvis était bien", ("jarvis",))[0] is False


def test_speculation_reuses_jev_answer(config):
    lz = launcher(config, {"quelle heure est-il": res("info")})
    lz.speculate("quelle heure est-il")
    lz._spec[next(iter(lz._spec))][1].result(timeout=5)
    out = lz.plan("quelle heure est-il")
    assert out.timings.get("speculated") == 1 and len(lz.client.calls) == 1
    lz.plan("quelle heure est-il maintenant")  # phrase différente : nouvel appel
    assert len(lz.client.calls) == 2


# ------------------------------------------------------------------ réglages personnels
def test_user_config_overrides_and_validation():
    from voxjev.config import apply_user_config

    base = load_config()
    user = {"settings": {"wake_words": ["vox", "jarvis"], "threshold": 0.8, "app_aliases": {"musique": "Spotify"}},
            "disabled_commands": ["wifi_off", "open_app"],
            "routines": [{"id": "routine_soir", "description": "Soirée", "examples": ["mode soirée"],
                          "steps": [{"run": "dark_mode", "with": {"theme": "sombre"}}]}],
            "removed_routines": ["routine_matin"]}
    c = load_config(user=user)
    assert c.settings.wake_words == ("vox", "jarvis") and c.settings.threshold == 0.8
    assert c.settings.app_aliases["musique"] == "Spotify" and "chrome" in c.settings.app_aliases
    ids = {x.id for x in c.commands_for_mode("defaut")}
    assert "wifi_off" not in ids and "open_app" not in ids and "open_app" not in c.settings.safe_commands
    assert "routine_soir" in ids and "routine_matin" not in ids and "routine_concentration" in ids
    assert "open_app" in {x.id for x in base.commands_for_mode("defaut")}  # la config de base est intacte
    for bad in ({"settings": {"exec_allowlist": ["rm"]}},                     # non modifiable
                {"settings": {"threshold": 2}},                               # invalide
                {"routines": [{"id": "r", "description": "d", "examples": ["e"], "steps": [{"run": "nope"}]}]}):
        with pytest.raises(ConfigError):
            load_config(user=bad)
    assert apply_user_config({"settings": {}}, {}) == {"settings": {}, "routines": []}


# ------------------------------------------------------------------ liens de la page Chrome
def test_page_link_ordinal_and_choice(config, monkeypatch):
    import voxjev.chrome as chrome

    links = [chrome.Link("https://fr.wikipedia.org/wiki/Lisbonne", "Lisbonne — Wikipédia", True),
             chrome.Link("https://www.visitlisboa.com/", "Visit Lisboa, site officiel", True),
             chrome.Link("https://accounts.google.com/", "Connexion", False)]
    assert chrome.ordinal("clique sur le 2ᵉ lien") == 2 and chrome.ordinal("ouvre le dernier résultat") == -1
    assert chrome.ordinal("ouvre le meilleur résultat") is None
    assert chrome.pick(FakeJevClient(), "ouvre le deuxième résultat", "t", "u", links)[0].domain == "visitlisboa.com"
    with pytest.raises(chrome.ChromeError):
        chrome.pick(FakeJevClient(), "ouvre le 5e résultat", "t", "u", links)
    monkeypatch.setattr(chrome, "page_links", lambda: ("Lisbonne - Recherche Google",
                                                       "https://www.google.com/search?q=lisbonne", links))
    lz = launcher(config, {"clique sur le premier lien": res("page_link")}, executor=Recorder())
    out = lz.handle("clique sur le premier lien")
    assert out.status == "executed" and out.steps[0].kind == "navigate"
    assert out.steps[0].argv == ("https://fr.wikipedia.org/wiki/Lisbonne",)
    with pytest.raises(chrome.ChromeError):
        chrome.navigate("javascript:alert(1)")


def test_page_link_is_deferred_in_compound_plans(config):
    from voxjev.multi import MultiRunner, Splitter

    lz = launcher(config, {"cherche la météo": res("web_search"), "ouvre le premier résultat": res("page_link"),
                           "cherche la météo puis ouvre le premier résultat": JevResult(
                               "web_search", {"web_search": 0.9, NONE: 0.1}, 0.9, 0.95, 0.02, compound=0.95)})
    plan = MultiRunner(lz, Splitter()).handle("cherche la météo puis ouvre le premier résultat")
    step = plan.items[1].steps[0]
    assert step.kind == "page_link" and step.argv == ("ouvre le premier résultat",)  # choisi à l'exécution


# ------------------------------------------------------------------ rangement des fenêtres
def test_layout_slots_never_overlap():
    from voxjev.layout import position_frame, slots

    area = (0, 25, 1440, 875)
    for n in range(1, 8):
        frames = slots(n, area)
        assert len(frames) == n
        for i, a in enumerate(frames):
            assert a[0] >= area[0] and a[1] >= area[1] and a[0] + a[2] <= 1440 and a[1] + a[3] <= 900
            for b in frames[i + 1:]:  # aucune superposition
                assert a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0] or a[1] + a[3] <= b[1] or b[1] + b[3] <= a[1]
    left, right = slots(2, area)
    assert left[0] < right[0] and left[2] == right[2]
    big, top, bottom = slots(3, area)
    assert big[3] > top[3] and top[0] == bottom[0]
    assert position_frame("right", area)[0] > 700


def test_window_commands(config):
    lz = launcher(config, {"mets Chrome à gauche et Safari à droite": res("window_pair"),
                           "mets cette fenêtre en haut à droite": res("window_place"),
                           "range les fenêtres": res("tile_windows")})
    out = lz.plan("mets Chrome à gauche et Safari à droite")
    assert out.steps[0].kind == "layout_pair" and out.steps[0].argv == ("Google Chrome", "Safari")
    assert lz.plan("mets cette fenêtre en haut à droite").steps[0].argv == ("top_right",)
    assert lz.plan("range les fenêtres").steps[0].argv == ("tile",)
    assert config.commands["tile_windows"].undo == {"type": "layout", "arrangement": "restore"}


def test_layout_config_validation(tmp_path):
    body = textwrap.dedent("""
    modes: {defaut: {commands: []}}
    common: [x]
    commands:
      - id: x
        description: d
        examples: [e]
        destructive: false
        action: ACTION
    """)
    p = tmp_path / "c.yaml"
    p.write_text(body.replace("ACTION", "{type: layout, arrangement: tile}"))
    load_config(p)
    p.write_text(body.replace("ACTION", "{type: layout, arrangement: explode}"))
    with pytest.raises(ConfigError):
        load_config(p)


def test_whisper_repetition_filter():
    from voxjev.stt import is_repetition

    assert is_repetition("Oh, oh, oh, oh, oh, oh, oh, oh")
    assert is_repetition("la la la la la la")
    assert not is_repetition("ouvre Spotify et mets du jazz")
    assert not is_repetition("oui oui")
