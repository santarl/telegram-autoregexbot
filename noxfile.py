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

    session.run("pyrefly", "check", "bot/")
    session.run("ruff", "format", "bot/")
    session.run("ruff", "check", "bot/")
