import nox


@nox.session
def lint(session):
    session.run(
        "uv",
        "sync",
        "--active",
        "--locked",
        "--inexact",
    )

    session.run("pyrefly", "check", "telegram_autoregexbot/")
    session.run("ruff", "format", "telegram_autoregexbot/")
    session.run("ruff", "check", "telegram_autoregexbot/")
