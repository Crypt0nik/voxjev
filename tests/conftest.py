"""Tests isolés : pas de réglages personnels, journal dans un dossier temporaire."""

import os

os.environ["VOXJEV_USER_CONFIG"] = "/nonexistent/voxjev-tests/settings.yaml"  # avant tout import de voxjev

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _journal_tmp(tmp_path, monkeypatch):
    import voxjev.context as ctx

    monkeypatch.setattr(ctx, "JOURNAL", tmp_path / "history.jsonl")
