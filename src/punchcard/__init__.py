"""PunchCard package — uv script entry point lives here."""


def main() -> None:
    """Launch PunchCard with `uv run punchcard`."""
    from .application import Application

    Application().run()
