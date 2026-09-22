"""Le journal des actions va dans un dossier temporaire pendant les tests."""

import pytest


@pytest.fixture(autouse=True)
def _journal_tmp(tmp_path, monkeypatch):
    import voxjev.context as ctx

    monkeypatch.setattr(ctx, "JOURNAL", tmp_path / "history.jsonl")
