"""Demandes composées : découpage, validation de la sortie LLM, planification et exécution."""

from __future__ import annotations

import pytest

from voxjev.config import load_config
from voxjev.context import Session
from voxjev.jev_client import NONE, FakeJevClient, JevResult
from voxjev.multi import MultiRunner, PlanOutcome, Splitter, parse_steps, split_rules
from voxjev.candidates import Provider
from voxjev.pipeline import Launcher

OFFLINE = Provider(menu_reader=lambda pid: [], shortcut_lister=lambda: [])

APPS = ("Google Chrome", "Safari", "Spotify", "Notion", "Ghostty", "Terminal", "Burp Suite Community Edition",
        "Calculator", "Finder")


@pytest.fixture(scope="module")
def config():
    return load_config()


class RecordingExecutor:
    def __init__(self):
        self.runs = []

    def run(self, steps):
        self.runs.append([s.argv for s in steps if s.argv])


def res(cmd, p=0.97, compound=0.05, destructive=0.05, addressed=0.95):
    return JevResult(cmd, {cmd: p, NONE: 1 - p}, p, addressed, destructive, compound=compound)


def runner(config, responses, confirm=None, dry_run=False, mode="defaut"):
    ex = RecordingExecutor()
    launcher = Launcher(config, FakeJevClient(responses=responses), Session(mode=mode, path=None), provider=OFFLINE,
                        executor=None if dry_run else ex, confirmer=lambda *a: True,
                        frontmost=lambda: "Finder", apps=APPS, dry_run=dry_run)
    config.settings.__dict__  # (réglages lus tels quels)
    return MultiRunner(launcher, Splitter(None), confirm_plan=confirm), ex


# ------------------------------------------------------------------ découpage par règles
@pytest.mark.parametrize("text,expected", [
    ("ouvre Spotify et monte le son", ["ouvre Spotify", "monte le son"]),
    ("Ouvre la calculatrice, puis verrouille l'écran.", ["Ouvre la calculatrice", "verrouille l'écran"]),
    ("cherche Daft Punk ensuite lance-la", ["cherche Daft Punk", "lance Daft Punk"]),
    ("ouvre Spotify, cherche Get Lucky puis lance-la et like-la",
     ["ouvre Spotify", "cherche Get Lucky sur Spotify", "lance Get Lucky sur Spotify",
      "like le morceau en cours sur Spotify"]),
    ("cherche des crêpes et des gaufres", ["cherche des crêpes et des gaufres"]),  # « et » sans verbe
    ("tu as vu le match et le résumé", ["tu as vu le match et le résumé"]),
    ("ouvre notion", ["ouvre notion"]),
])
def test_split_rules(text, expected):
    assert split_rules(text) == expected


def test_parse_steps_validates_llm_output():
    assert parse_steps('{"steps": ["ouvre Spotify", "lance Get Lucky"]}', 6) == ["ouvre Spotify", "lance Get Lucky"]
    assert parse_steps('Voici : {"steps": ["a", "b", "c"]} merci', 2) == ["a", "b"]
    assert parse_steps("pas de json", 6) is None
    assert parse_steps('{"steps": "ouvre"}', 6) is None
    assert parse_steps('{"steps": [1, 2]}', 6) is None


# ------------------------------------------------------------------ plans
def test_compound_plan_executes_every_step(config):
    r, ex = runner(config, {
        "ouvre Spotify et monte le son": res("open_app", compound=0.9),
        "ouvre Spotify": res("open_app"),
        "monte le son": res("volume_up"),
    })
    config_delay = config.settings.step_delay_seconds
    object.__setattr__(config.settings, "step_delay_seconds", 0)
    try:
        plan = r.handle("ouvre Spotify et monte le son")
    finally:
        object.__setattr__(config.settings, "step_delay_seconds", config_delay)
    assert isinstance(plan, PlanOutcome) and plan.status == "executed"
    assert [o.command_id for o in plan.items] == ["open_app", "volume_up"]
    assert ex.runs[0] == [("open", "-a", "Spotify")] and ex.runs[1][0][0] == "osascript"


def test_low_compound_stays_single(config):
    r, _ = runner(config, {"ouvre Spotify et monte le son": res("open_app", compound=0.2)})
    out = r.handle("ouvre Spotify et monte le son")
    assert not isinstance(out, PlanOutcome) and out.command_id == "open_app"


def test_single_command_that_covers_the_split_wins(config):
    text = "ouvre chrome et cherche la météo"
    r, _ = runner(config, {
        text: res("web_search", compound=0.8),
        "ouvre chrome": res("open_app"),
        "cherche la météo": res("web_search"),
    }, dry_run=True)
    out = r.handle(text)
    assert not isinstance(out, PlanOutcome)
    assert out.args.values == {"query": "la météo", "browser": "Google Chrome"}


def test_one_invalid_step_blocks_the_whole_plan(config):
    r, ex = runner(config, {
        "ouvre Spotify et ouvre Photoshop": res("open_app", compound=0.9),
        "ouvre Spotify": res("open_app"),
        "ouvre Photoshop": res("open_app"),  # app non installée -> erreur d'argument
    })
    plan = r.handle("ouvre Spotify et ouvre Photoshop")
    assert plan.status == "error" and "étape 2" in plan.error and not ex.runs


def test_destructive_step_requires_plan_confirmation(config):
    asked = []
    r, ex = runner(config, {
        "ouvre Spotify et vide la corbeille": res("open_app", compound=0.9),
        "ouvre Spotify": res("open_app"),
        "vide la corbeille": res("empty_trash", destructive=0.9),
    }, confirm=lambda plan: asked.append(plan) or False)
    plan = r.handle("ouvre Spotify et vide la corbeille")
    assert plan.status == "cancelled" and len(asked) == 1 and asked[0].destructive and not ex.runs


def test_dropped_step_requires_confirmation(config):
    asked = []
    r, _ = runner(config, {
        "ouvre Spotify et raconte une blague": res("open_app", compound=0.9),
        "ouvre Spotify": res("open_app"),
        "raconte une blague": res(NONE, p=0.99),
    }, confirm=lambda plan: asked.append(plan) or True)
    object.__setattr__(config.settings, "step_delay_seconds", 0)
    plan = r.handle("ouvre Spotify et raconte une blague")
    assert asked and plan.status == "executed" and [o.status for o in plan.items] == ["executed", "ignored"]


def test_mode_switch_replans_following_steps(config):
    r, _ = runner(config, {
        "passe en mode ctf et ouvre cyberchef": res("switch_mode", compound=0.9),
        "passe en mode ctf": res("switch_mode"),
        "ouvre cyberchef": res("open_cyberchef"),  # n'existe qu'en mode ctf
    }, dry_run=True)
    plan = r.handle("passe en mode ctf et ouvre cyberchef")
    assert plan.status == "dry_run" and [o.command_id for o in plan.items] == ["switch_mode", "open_cyberchef"]


def test_dry_run_plan_executes_nothing(config):
    r, ex = runner(config, {
        "ouvre Spotify et monte le son": res("open_app", compound=0.9),
        "ouvre Spotify": res("open_app"),
        "monte le son": res("volume_up"),
    }, dry_run=True)
    plan = r.handle("ouvre Spotify et monte le son")
    assert plan.status == "dry_run" and not ex.runs
