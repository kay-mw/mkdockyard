# TODO:
# - Add some functionality that gets the size of the cache dir, and if it exceeds a
# certain size, prunes unused entries by comparing the contents against the current
# mkdocs.yml repos configuration.
# - Add a check for the git version. If it's not at least 2.49, don't even let the
# plugin start doing anything.
import concurrent.futures
import hashlib
import logging
import os
import shutil
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
    cache_limit_multiplier = c.Type(int, default=2)


class MkdockyardPlugin(BasePlugin[MkdockyardConfig]):
    def on_config(self, config: MkDocsConfig):
        cache_dir = Path(user_cache_dir("mkdockyard"))
        repos = self.config.repos
        cache_limit_multiplier = self.config.cache_limit_multiplier

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

        output_paths = [info.get("output_path") for info in clone_information]
        cached_repos = os.listdir(cache_dir)
        len_output = len(output_paths)
        n_unused_in_cache = len(cached_repos) - len_output
        cache_limit = len_output * cache_limit_multiplier
        if n_unused_in_cache > cache_limit:
            log.info(
                f"Detected {n_unused_in_cache} unused repo(s) in the cache,"
                f" which exceeds the cache limit of {cache_limit}. Pruning..."
            )
            cached_repos = [Path(cache_dir.joinpath(x)) for x in cached_repos]
            self.prune_cache(
                output_paths=output_paths,
                cached_repos=cached_repos,
                cache_dir=cache_dir,
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

    def prune_cache(
        self, output_paths: list[Path], cached_repos: list[Path], cache_dir: Path
    ) -> None:
        for repo in cached_repos:
            if repo in output_paths:
                continue

            if not repo.is_relative_to(cache_dir):
                raise ValueError(
                    f"Almost repo dir {repo}, but detected that it's path is not"
                    f" relative to {cache_dir}."
                )

            shutil.rmtree(repo)
