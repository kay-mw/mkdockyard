# TODO:
# - Add some functionality that gets the size of the cache dir, and if it exceeds a
# certain size, prunes unused entries by comparing the contents against the current
# mkdocs.yml repos configuration.
# - Add a check for the git version. If it's not at least 2.49, don't even let the
# plugin start doing anything.

import concurrent.futures
import hashlib
import logging
import subprocess
from pathlib import Path

from mkdocs.config import base
from mkdocs.config import config_options as c
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.exceptions import ConfigurationError
from mkdocs.plugins import BasePlugin
from platformdirs import user_cache_dir

log = logging.getLogger(f"mkdocs.plugins.{__name__}")


class _Repos(base.Config):
    """The repos config option. Defines"""

    url = c.Type(str)
    ref = c.Type(str)


class MkdockyardConfig(base.Config):
    repos = c.ListOfItems(c.SubConfig(_Repos))


class MkdockyardPlugin(BasePlugin[MkdockyardConfig]):
    def on_config(self, config: MkDocsConfig):
        cache_dir = Path(user_cache_dir("mkdockyard"))
        repos = self.config.repos

        plugins = config.plugins
        if "mkdocstrings" not in plugins:
            defined_plugins = list(plugins.keys())
            raise ConfigurationError(
                "Failed to find the key 'mkdocstrings' in your plugin config."
                " Are you sure you have 'mkdocstrings' defined under `plugins` in"
                " your `mkdocs.yml`?\n"
                f"Found plugins: {defined_plugins}"
            )

        handlers = plugins["mkdocstrings"].config.setdefault("handlers", {})
        python = handlers.setdefault("python", {})
        paths = python.setdefault("paths", ["."])

        clone_information = []
        for repo in repos:
            url = repo.url
            ref = repo.ref

            hashed_name = hashlib.sha256((url + ref).encode()).hexdigest()
            output_path = cache_dir.joinpath(hashed_name)

            paths.append(str(output_path))

            clone_information.append(
                {"url": url, "ref": ref, "output_path": output_path}
            )

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_to_clone = {
                executor.submit(
                    self.clone_git_repo,
                    url=info["url"],
                    ref=info["ref"],
                    output_path=info["output_path"],
                ): info
                for info in clone_information
            }
            for i, future in enumerate(
                concurrent.futures.as_completed(future_to_clone)
            ):
                try:
                    future.result()
                except subprocess.CalledProcessError as e:
                    info = future_to_clone[future]
                    raise ConfigurationError(
                        f"Failed to fetch git URL '{info['url']}' for ref '{info['ref']}'."
                        " See Git output below:\n"
                        f"{e.stderr}"
                    )

    def clone_git_repo(self, url: str, ref: str, output_path: Path) -> None:
        if not output_path.exists():
            log.info(f"Fetching '{url}' at ref '{ref}' into '{output_path}'")
            subprocess.run(
                [
                    "git",
                    "clone",
                    url,
                    output_path,
                    "--depth=1",
                    f"--revision={ref}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            log.info(f"Reusing repo {url}, which already exists at {output_path}")
