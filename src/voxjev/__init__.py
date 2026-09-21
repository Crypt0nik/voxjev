"""voxjev : lanceur vocal macOS en français, avec Jev (TypeSafe) comme couche de décision."""


def main() -> int:
    from .cli import main as _main

    return _main()
