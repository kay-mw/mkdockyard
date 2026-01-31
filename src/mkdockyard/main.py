import subprocess
from pathlib import Path

from mkdocs.config import base
from mkdocs.config import config_options as c
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.plugins import BasePlugin


class _Repo(base.Config):
    """This is a docstring."""

    name = c.Type(str)
    url = c.Type(str)
    ref = c.Type(str)


class MkdockyardConfig(base.Config):
    repos = c.ListOfItems(c.SubConfig(_Repo))


class MkdockyardPlugin(BasePlugin[MkdockyardConfig]):
    def on_config(self, config: MkDocsConfig):
        cache_dir = Path.home().joinpath(".cache").joinpath("mkdockyard")
        repos = self.config.repos
        for repo in repos:
            url = repo.url
            ref = repo.ref
            name = repo.name
            output_path = cache_dir.joinpath(ref + "-" + name)

            config.plugins["mkdocstrings"].config["handlers"]["python"]["paths"].append(
                str(output_path)
            )
            print(config.plugins["mkdocstrings"].config["handlers"]["python"]["paths"])

            if not output_path.exists():
                subprocess.run(
                    [
                        "git",
                        "clone",
                        url,
                        output_path,
                        "--depth=1",
                        f"--revision={ref}",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                )
