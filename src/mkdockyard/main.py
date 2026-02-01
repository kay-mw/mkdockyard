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
from mkdocs.exceptions import ConfigurationError, PluginError
from mkdocs.plugins import BasePlugin
from platformdirs import user_cache_dir

log = logging.getLogger(f"mkdocs.plugins.{__name__}")


class _Repos(base.Config):
    name = c.Type(str)
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

        git_supports_revision = False
        git_major_version, git_minor_version = self.get_git_version()
        if (
            git_major_version == 2 and git_minor_version >= 49
        ) or git_major_version > 2:
            git_supports_revision = True

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
            name = repo.name
            url = repo.url
            ref = repo.ref

            hashed_name = hashlib.sha256((url + ref).encode()).hexdigest()
            hashed_dir = cache_dir.joinpath(hashed_name)

            paths.append(str(hashed_dir))

            clone_information.append(
                {"name": name, "url": url, "ref": ref, "hashed_dir": hashed_dir}
            )

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_to_clone = {
                executor.submit(
                    self.clone_git_repo,
                    name=info["name"],
                    url=info["url"],
                    ref=info["ref"],
                    hashed_dir=info["hashed_dir"],
                    git_supports_revision=git_supports_revision,
                ): info
                for info in clone_information
            }
            for future in concurrent.futures.as_completed(future_to_clone):
                try:
                    future.result()
                except subprocess.CalledProcessError as e:
                    info = future_to_clone[future]
                    raise ConfigurationError(
                        f"Failed to fetch git URL '{info['url']}' for ref '{info['ref']}'."
                        " See Git output below:\n"
                        f"{e.stderr}"
                    )

        configured_repos = [info.get("hashed_dir") for info in clone_information]
        cached_repos = os.listdir(cache_dir)
        len_configured = len(configured_repos)
        n_unused_in_cache = len(cached_repos) - len_configured
        cache_limit = len_configured * cache_limit_multiplier
        if n_unused_in_cache > cache_limit:
            log.info(
                f"Detected {n_unused_in_cache} unused repo(s) in the cache,"
                f" which exceeds the cache limit of {cache_limit}. Pruning..."
            )
            cached_repos = [Path(cache_dir.joinpath(x)) for x in cached_repos]
            self.prune_cache(
                output_paths=configured_repos,
                cached_repos=cached_repos,
                cache_dir=cache_dir,
            )

    def get_git_version(self) -> tuple[int, int]:
        version_string = subprocess.run(
            ["git", "--version"], check=True, capture_output=True, text=True
        )
        version_number = version_string.stdout.split(" ")[2]
        version_components = version_number.split(".")
        major_version = int(version_components[0])
        minor_version = int(version_components[1])

        return major_version, minor_version

    def subprocess_run_wrapper(self, args: list[str], output_path: Path):
        subprocess.run(
            args,
            cwd=output_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def clone_git_repo(
        self,
        url: str,
        ref: str,
        hashed_dir: Path,
        name: str,
        git_supports_revision: bool,
    ) -> None:
        output_path = hashed_dir.joinpath(name)
        if not hashed_dir.exists():
            log.info(f"Fetching '{url}' at ref '{ref}' into '{output_path}'")
            if git_supports_revision:
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
                return

            os.makedirs(output_path)
            self.subprocess_run_wrapper(args=["git", "init"], output_path=output_path)
            self.subprocess_run_wrapper(
                args=["git", "remote", "add", "origin", url], output_path=output_path
            )
            self.subprocess_run_wrapper(
                args=["git", "fetch", "--depth", "1", "origin", ref],
                output_path=output_path,
            )
            self.subprocess_run_wrapper(
                args=["git", "checkout", "FETCH_HEAD"], output_path=output_path
            )
        elif not output_path.exists():
            old_name_path = hashed_dir.joinpath(os.listdir(hashed_dir)[0])
            log.info(
                f"Name change detected. Renaming '{old_name_path}' to '{output_path}'"
            )
            os.rename(old_name_path, output_path)
        else:
            log.info(f"Reusing repo {url}")

    def prune_cache(
        self, output_paths: list[Path], cached_repos: list[Path], cache_dir: Path
    ) -> None:
        for repo in cached_repos:
            if repo in output_paths:
                continue

            if not repo.is_relative_to(cache_dir):
                raise PluginError(
                    f"Almost pruned repo dir {repo}, but it's path is not relative to"
                    f" {cache_dir}."
                )

            shutil.rmtree(repo)
