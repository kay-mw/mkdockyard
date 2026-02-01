import concurrent.futures
import hashlib

# TODO:
# - Add validation to `name`, i.e. it cannot contain slashes or other path un-safe
# characters.
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mkdocs.config import base
from mkdocs.config import config_options as c
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.exceptions import ConfigurationError, PluginError
from mkdocs.plugins import BasePlugin
from platformdirs import user_cache_dir

log = logging.getLogger(f"mkdocs.plugins.{__name__}")


@dataclass
class CloneInformation:
    name: str
    url: str
    ref: str
    hashed_dir: Path


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
                "mkdockyard: Failed to find the key 'mkdocstrings' in your plugin config."
                " Are you sure you have 'mkdocstrings' defined under `plugins` in"
                " your `mkdocs.yml`?\n"
                f"Found plugins: {defined_plugins}"
            )

        handlers = plugins["mkdocstrings"].config.setdefault("handlers", {})
        python = handlers.setdefault("python", {})
        paths = python.setdefault("paths", ["."])

        clone_information = self.build_clone_information(
            repos=repos, cache_dir=cache_dir
        )
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_to_clone = {
                executor.submit(
                    self.make_dockyard,
                    name=info.name,
                    url=info.url,
                    ref=info.ref,
                    hashed_dir=info.hashed_dir,
                    git_supports_revision=git_supports_revision,
                ): info
                for info in clone_information
            }
            for future in concurrent.futures.as_completed(future_to_clone):
                info = future_to_clone[future]
                try:
                    cloned = future.result()
                    paths.append(str(future_to_clone[future].hashed_dir))

                    if cloned:
                        log.info(
                            f"mkdockyard: Fetching '{info.url}' at ref '{info.ref}'"
                        )
                except subprocess.CalledProcessError as e:
                    raise ConfigurationError(
                        f"mkdockyard: Failed to fetch git URL '{info.url}' for ref"
                        f" '{info.ref}'. See Git output below:\n"
                        f"{e.stderr}"
                    )

        configured_repos = [info.hashed_dir for info in clone_information]
        cached_repos = os.listdir(cache_dir)
        len_configured = len(configured_repos)
        n_unused_in_cache = len(cached_repos) - len_configured
        cache_limit = len_configured * self.config.cache_limit_multiplier
        if n_unused_in_cache > cache_limit:
            log.info(
                f"mkdockyard: Detected {n_unused_in_cache} unused repo(s) in the cache,"
                f" which exceeds the cache limit of {cache_limit}. Pruning..."
            )
            cached_repos = [Path(cache_dir.joinpath(repo)) for repo in cached_repos]
            self.prune_cache(
                output_paths=configured_repos,
                cached_repos=cached_repos,
                cache_dir=cache_dir,
            )

    @staticmethod
    def get_git_version() -> tuple[int, int]:
        version_string = subprocess.run(
            ["git", "--version"], check=True, capture_output=True, text=True
        )
        version_number = version_string.stdout.split(" ")[2]
        version_components = version_number.split(".")
        major_version = int(version_components[0])
        minor_version = int(version_components[1])

        return major_version, minor_version

    @staticmethod
    def build_clone_information(
        repos: list[_Repos], cache_dir: Path
    ) -> list[CloneInformation]:
        clone_information: list[CloneInformation] = []
        for repo in repos:
            name = repo.name
            url = repo.url
            ref = repo.ref

            hashed_name = hashlib.sha256((url + ref).encode()).hexdigest()
            hashed_dir = cache_dir.joinpath(hashed_name)

            clone_information.append(CloneInformation(name, url, ref, hashed_dir))

        return clone_information

    @staticmethod
    def subprocess_run_wrapper(args: list[str], output_path: Path) -> None:
        subprocess.run(
            args,
            cwd=output_path,
            check=True,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def clone_git_repo(
        git_supports_revision: bool, url: str, ref: str, output_path: Path
    ) -> None:
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
        MkdockyardPlugin.subprocess_run_wrapper(
            args=["git", "init"], output_path=output_path
        )
        MkdockyardPlugin.subprocess_run_wrapper(
            args=["git", "remote", "add", "origin", url], output_path=output_path
        )
        MkdockyardPlugin.subprocess_run_wrapper(
            args=["git", "fetch", "--depth", "1", "origin", ref],
            output_path=output_path,
        )
        MkdockyardPlugin.subprocess_run_wrapper(
            args=["git", "checkout", "FETCH_HEAD"], output_path=output_path
        )

    @staticmethod
    def make_dockyard(
        url: str,
        ref: str,
        hashed_dir: Path,
        name: str,
        git_supports_revision: bool,
    ) -> bool:
        output_path = hashed_dir.joinpath(name)
        if not hashed_dir.exists():
            MkdockyardPlugin.clone_git_repo(
                git_supports_revision=git_supports_revision,
                url=url,
                ref=ref,
                output_path=output_path,
            )
            return True
        elif not output_path.exists():
            old_name_path = hashed_dir.joinpath(os.listdir(hashed_dir)[0])
            log.info(
                f"mkdockyard: Name change detected. Renaming '{old_name_path}' to"
                f"'{output_path}'"
            )
            os.rename(old_name_path, output_path)
        else:
            log.info(f"mkdockyard: Reusing repo {url}")

        return False

    @staticmethod
    def prune_cache(
        output_paths: list[Path], cached_repos: list[Path], cache_dir: Path
    ) -> None:
        for repo in cached_repos:
            if repo in output_paths:
                continue

            if not repo.is_relative_to(cache_dir):
                raise PluginError(
                    f"mkdockyard: Almost pruned repo dir {repo}, but it's path is not"
                    f"relative to {cache_dir}."
                )

            shutil.rmtree(repo)
