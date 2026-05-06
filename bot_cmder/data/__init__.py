"""Package-internal data files for bot-cmder.

Lives inside the importable package (instead of repo-root `config/`)
so it ships in the wheel automatically — `bot-cmder init` reads
`app.yaml.example` via `importlib.resources.files("bot_cmder.data")`,
which works equally from a source checkout and from a `pip install`-ed
site-packages location.
"""
