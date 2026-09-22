"""Tests hors ligne : config, extraction d'arguments, décision, plan d'action, pipeline (FakeJev)."""

from __future__ import annotations

import textwrap

import pytest

from voxjev.actions import ActionError, plan_command
from voxjev.args import extract_args, normalize
from voxjev.config import ConfigError, Settings, load_config
from voxjev.context import Session
from voxjev.decide import Verdict, decide
from voxjev.jev_client import NONE, FakeJevClient, JevError, JevResult, build_questions, build_state
from voxjev.pipeline import Launcher

APPS = ("Google Chrome", "Safari", "Spotify", "Notion", "Visual Studio Code", "Ghostty", "Terminal",
        "Burp Suite Community Edition", "Mail", "Calendar", "Notes", "Finder")


@pytest.fixture(scope="module")
def config():
    return load_config()


def result(cmd: str, p: float = 0.95, addressed: float = 0.95, destructive: float = 0.05) -> JevResult:
    return JevResult(cmd, {cmd: p, NONE: 1 - p}, p, addressed, destructive)


def make_launcher(config, responses=None, executor=None, confirmer=None, mode="defaut", tmp_path=None):
    session = Session(mode=mode, path=(tmp_path / "state.json") if tmp_path else None)
    return Launcher(config, FakeJevClient(responses=responses or {}), session, executor=executor,
                    confirmer=confirmer, frontmost=lambda: "Ghostty", apps=APPS)


# ------------------------------------------------------------------ config
def test_default_config_loads(config):
    assert {"defaut", "ctf", "travail"} <= set(config.modes)
    ids = {c.id for c in config.commands_for_mode("ctf")}
    assert {"open_app", "exploitdb_search"} <= ids
    assert "exploitdb_search" not in {c.id for c in config.commands_for_mode("defaut")}


def _write(tmp_path, body: str):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent(body))
    return p


BASE = """
settings: {exec_allowlist: [pmset]}
modes: {defaut: {commands: []}}
common: [x]
commands:
  - id: x
    description: d
    examples: [e]
    destructive: false
"""


@pytest.mark.parametrize("action", [
    "{type: shell, cmd: 'rm -rf /'}",                                  # type hors liste blanche
    "{type: exec, argv: [rm, -rf, /]}",                               # binaire hors allowlist
    "{type: applescript, script: 'do shell script \"{q}\"'}",          # placeholder dans un script
    "{type: open_url, url: 'file:///etc/passwd'}",                    # schéma interdit
    "{type: open_url, url: 'https://x/{undeclared}'}",                # argument non déclaré
])
def test_config_rejects_unsafe_actions(tmp_path, action):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, BASE + f"    action: {action}\n"))


# ------------------------------------------------------------------ arguments
def test_normalize_strips_politeness_and_punct():
    assert normalize("Lance Spotify, s'il te plaît !", ("s'il te plaît",)) == "Lance Spotify"


@pytest.mark.parametrize("text,expected", [
    ("ouvre chrome", "Google Chrome"),
    ("lance l'application Visual Studio Code", "Visual Studio Code"),
    ("ouvre vscode", "Visual Studio Code"),
    ("démarre burp", "Burp Suite Community Edition"),
    ("Ouvre Spotify s'il te plaît.", "Spotify"),
])
def test_app_fuzzy_matching(config, text, expected):
    assert extract_args(config.commands["open_app"], text, config, APPS).values["app"] == expected


def test_unknown_app_is_missing(config):
    r = extract_args(config.commands["open_app"], "ouvre photoshop", config, APPS)
    assert r.missing == ["app"]


def test_web_search_query_and_browser(config):
    r = extract_args(config.commands["web_search"], "Ouvre Chrome et cherche la météo à Lyon.", config, APPS)
    assert r.values == {"query": "la météo à Lyon", "browser": "Google Chrome"}
    r = extract_args(config.commands["web_search"], "cherche des crêpes dans safari", config, APPS)
    assert r.values == {"query": "des crêpes", "browser": "Safari"}


def test_mode_and_enum_args(config):
    assert extract_args(config.commands["switch_mode"], "passe en mode boulot", config, APPS).values["mode"] == "travail"
    assert extract_args(config.commands["open_website"], "va sur github", config, APPS).values["site"] == "https://github.com"


# ------------------------------------------------------------------ décision
def test_decide_rules(config):
    cmds, s = config.commands, config.settings
    assert decide(result("open_app", 0.9), cmds, s).verdict == Verdict.EXECUTE
    assert decide(result("open_app", 0.6), cmds, s).verdict == Verdict.CONFIRM
    assert decide(result("open_app", 0.3), cmds, s).verdict == Verdict.IGNORE
    assert decide(result("open_app", 0.9, addressed=0.2), cmds, s).verdict == Verdict.IGNORE
    assert decide(result("open_app", 0.9, addressed=0.5), cmds, s).verdict == Verdict.CONFIRM
    assert decide(result(NONE, 0.99), cmds, s).verdict == Verdict.IGNORE
    # destructive : config OU Noul -> confirmation systématique, même à p=1
    assert decide(result("empty_trash", 1.0), cmds, s).verdict == Verdict.CONFIRM
    assert decide(result("open_app", 1.0, destructive=0.8), cmds, s).verdict == Verdict.CONFIRM


def test_threshold_is_configurable(config):
    s = Settings(threshold=0.95, confirm_floor=0.4)
    assert decide(result("open_app", 0.9), config.commands, s).verdict == Verdict.CONFIRM


# ------------------------------------------------------------------ plan / sécurité
def test_query_is_url_encoded_never_raw(config):
    evil = "x; rm -rf ~ && open /Applications/Calculator.app"
    steps = plan_command(config.commands["web_search"], {"query": evil}, config, APPS)
    (argv,) = [s.argv for s in steps]
    assert argv[0] == "open" and argv[-1].startswith("https://www.google.com/search?q=")
    assert " " not in argv[-1] and ";" not in argv[-1]


def test_app_names_passed_as_argv_not_script(config):
    steps = plan_command(config.commands["quit_app"], {"app": "Notion"}, config, APPS)
    argv = steps[0].argv
    assert argv[-1] == "Notion" and all("Notion" not in a for a in argv[:-1])


def test_uninstalled_app_refused(config):
    with pytest.raises(ActionError):
        plan_command(config.commands["open_app"], {"app": "Evil"}, config, APPS)


def test_set_mode_runs_on_enter(config):
    steps = plan_command(config.commands["switch_mode"], {"mode": "ctf"}, config, APPS)
    assert steps[0].kind == "set_mode" and ("open", "-a", "Burp Suite Community Edition") in [s.argv for s in steps]


# ------------------------------------------------------------------ pipeline (FakeJev)
def test_questions_shape(config):
    q = build_questions(config.commands_for_mode("defaut"), config.settings.none_option)
    assert set(q) == {"command", "addressed", "destructive", "compound"}
    assert NONE in q["command"]["criteria"] and q["addressed"]["type"] == "noul"


def test_state_contains_only_allowed_fields():
    state = build_state("ouvre chrome", "Ghostty", APPS, "defaut", None)
    assert set(state) == {"transcript", "frontmost_app", "installed_apps", "assistant_mode", "last_command"}


class RecordingExecutor:
    def __init__(self):
        self.runs = []

    def run(self, steps):
        self.runs.append(steps)


def test_pipeline_dry_run_single_call(config):
    launcher = make_launcher(config, {"ouvre chrome": result("open_app")})
    out = launcher.handle("ouvre chrome")
    assert out.status == "dry_run" and out.steps[0].argv == ("open", "-a", "Google Chrome")
    assert len(launcher.client.calls) == 1


def test_pipeline_executes_and_updates_session(config, tmp_path):
    ex = RecordingExecutor()
    launcher = make_launcher(config, {"passe en mode ctf": result("switch_mode")}, executor=ex, tmp_path=tmp_path)
    out = launcher.handle("passe en mode ctf")
    assert out.status == "executed" and launcher.session.mode == "ctf"
    assert launcher.session.last_command == "switch_mode" and (tmp_path / "state.json").exists()


def test_pipeline_confirmation_refused(config):
    ex = RecordingExecutor()
    launcher = make_launcher(config, {"vide la corbeille": result("empty_trash", 1.0)}, executor=ex,
                             confirmer=lambda *a: False)
    assert launcher.handle("vide la corbeille").status == "cancelled" and not ex.runs


def test_pipeline_ignores_offtopic_with_heuristic_fake(config):
    out = make_launcher(config).handle("on mange quoi ce midi les gars")
    assert out.status == "ignored" and not out.triggered


def test_pipeline_jev_failure(config):
    launcher = make_launcher(config)
    launcher.client.fail = True
    out = launcher.handle("ouvre chrome")
    assert out.status == "error" and "Jev" in out.error


def test_mode_filters_options(config):
    launcher = make_launcher(config, {"x": result(NONE)})
    launcher.handle("x")
    # le faux client reçoit l'état ; les options dépendent du mode (défaut : pas de commandes CTF)
    assert launcher.client.calls[0]["assistant_mode"] == "defaut"


# ------------------------------------------------------------------ exécution
def test_executor_runs_argv_without_shell(tmp_path):
    from voxjev.actions import Step
    from voxjev.executor import SubprocessExecutor

    marker = tmp_path / "ok; touch pwned"
    SubprocessExecutor().run([Step("run", ("touch", str(marker)), label="touch"), Step("note", label="info")])
    assert marker.exists() and not (tmp_path / "pwned").exists()


def test_executor_reports_failure():
    from voxjev.actions import Step
    from voxjev.executor import SubprocessExecutor

    with pytest.raises(ActionError):
        SubprocessExecutor().run([Step("run", ("false",), label="false")])


def test_pipeline_reports_execution_error(config):
    class Failing:
        def run(self, steps):
            raise ActionError("boom")

    launcher = make_launcher(config, {"ouvre chrome": result("open_app")}, executor=Failing())
    out = launcher.handle("ouvre chrome")
    assert out.status == "error" and launcher.session.last_command is None


def test_cve_rewrite(config):
    r = extract_args(config.commands["cve_search"], "cherche la CVE 2014 0160", config, APPS)
    assert r.values["query"] == "CVE-2014-0160"


@pytest.mark.parametrize("text,expected", [
    ("Oui.", True), ("ouais vas-y", True), ("Confirme", True), ("OK", True), ("d’accord", True),
    ("Non.", False), ("annule", False), ("laisse tomber", False), ("Non, oui, enfin non", False),
    ("ouvre Spotify", None), ("je sais pas", None), ("", None),
])
def test_parse_yes_no(text, expected):
    from voxjev.decide import parse_yes_no

    assert parse_yes_no(text) is expected


def test_explain_is_human_readable(config):
    from voxjev.decide import explain

    d = decide(result("open_app", 0.95), config.commands, config.settings)
    assert explain(d, 0.95) == "Confiance 95 % : exécution directe."
    assert "aurait été exécuté" in explain(d, 0.95, dry_run=True)
    assert explain(decide(result("empty_trash", 1.0), config.commands, config.settings), 1.0).startswith("Action destructrice")
    assert explain(decide(result(NONE, 0.99), config.commands, config.settings), 0.99).startswith("Ce n'est pas une commande")


def test_command_short_label(config):
    assert config.commands["open_app"].short({"app": "Spotify"}) == "Ouvrir Spotify"
    assert config.commands["volume_up"].short() == "Monter le volume"
    assert all(c.label for c in config.commands.values())


def test_runner_up_fallback_when_args_invalid(config):
    r = JevResult("open_app", {"open_app": 0.52, "open_website": 0.47, NONE: 0.01}, 0.49, 0.92, 0.04)
    launcher = make_launcher(config, {"ouvre YouTube": r})
    out = launcher.handle("ouvre YouTube")
    assert out.command_id == "open_website" and out.decision.verdict == Verdict.CONFIRM
    assert out.args.values["site"] == "https://www.youtube.com"


def test_web_agent_guard_blocks_risky_labels():
    from voxjev.webagent import RISKY

    for label in ["Acheter maintenant", "Passer la commande", "Payer 49 €", "Book now", "Envoyer",
                  "Supprimer le compte", "Réserver ce vol", "Publier"]:
        assert RISKY.search(label), label
    for label in ["Rechercher", "Pays", "Bookmarks", "Explorer", "Aller simple", "Paris (CDG)", "Ordre"]:
        assert not RISKY.search(label), label


def test_web_task_plan(config):
    launcher = make_launcher(config, {"trouve un vol Paris Lisbonne": result("web_task")})
    out = launcher.handle("trouve un vol Paris Lisbonne")
    assert out.decision.verdict == Verdict.CONFIRM  # toujours confirmé
    (step,) = out.steps
    assert step.kind == "web" and step.argv[1].startswith("https://www.google.com/travel/flights")
