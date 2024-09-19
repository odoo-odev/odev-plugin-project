from copier import run_copy, run_update
from git.exc import GitCommandError

from odev.common import bash, progress, string
from odev.common.commands import DatabaseOrRepositoryCommand, LocalDatabaseCommand
from odev.common.connectors import GitConnector, Stash
from odev.common.logging import LOG_LEVEL, logging
from odev.common.odoobin import OdoobinProcess


logger = logging.getLogger(__name__)


PRE_COMMIT_REPOSITORY = "odoo-ps/psbe-ps-tech-tools"
"""The repository to clone in order to install pre-commit hooks."""

COPIER_ANSWERS_FILE = ".copier-answers.yml"
"""The name of the file containing the answers to the Copier prompts."""


class PreCommit(DatabaseOrRepositoryCommand, LocalDatabaseCommand):
    """Install or update pre-commit hooks to be used with a database and its repository."""

    _name = "pre-commit"

    _exclusive_arguments = [("database", "repository")]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not self.args.repository and not self._database.repository:
            raise self.error(f"No repository linked to database {self._database.name!r}")

        self._repository = GitConnector(
            self._database.repository.full_name if self._database.repository else self.args.repository
        )

    def run(self):
        self._repository.clone()

        if self._database and self._database.version:
            self.version = self._database.version
        else:
            self.version = OdoobinProcess.version_from_addons(self._repository.path)

        if not self.version:
            raise self.error(f"Could not determine Odoo version from repository {self._repository.name!r}")

        with progress.spinner(f"Copying pre-commit config for Odoo {self.version}"):
            self._copy_config()
            self._install_hooks()
            self._commit_changes()

        logger.info(
            f"Pre-commit configuration successfully {'installed' if self._is_fresh_install() else 'updated'} "
            f"in repository {self._repository.name!r}"
        )

    def _is_fresh_install(self) -> bool:
        """Check whether currently installing the pre-commit configuration or updating it."""
        return not self._repository.exists or not (self._repository.path / COPIER_ANSWERS_FILE).is_file()

    def _copy_config(self) -> None:
        """Copy pre-commit configuration files to the repository, or update the existing configuration if any."""
        if not self._repository.repository:
            raise self.error(f"Ignoring non-existing repository {self._repository.name!r}")

        copier_params = {
            "dst_path": self._repository.path,
            "quiet": LOG_LEVEL != "DEBUG",
            "overwrite": True,
            "data": {"odoo_version": str(self.version)},
            "unsafe": True,
        }

        with Stash(self._repository.repository):
            if self._is_fresh_install():
                logger.info("Handing over to Copier to configure options:")
                self.console.pause_live()
                self.console.print()

                run_copy(
                    f"gh:{PRE_COMMIT_REPOSITORY}",
                    **copier_params,
                    defaults=False,
                )

                self.console.print()
                self.console.resume_live()
            else:
                run_update(
                    **copier_params,
                    answers_file=COPIER_ANSWERS_FILE,
                    defaults=True,
                )

    def _install_hooks(self) -> None:
        """Install pre-commit hooks in the repository."""
        with progress.spinner("Installing pre-commit hooks"):
            try:
                # The `GIT_CONFIG` environment variable is temporarily set to `/dev/null` to avoid
                # interfering with the user's global git configuration, as it could be setup as to use global hooks
                # in which case pre-commit will fail to install its own hook
                bash.execute("GIT_CONFIG=/dev/null pre-commit install")
            except bash.CalledProcessError as error:
                raise self.error(f"Failed to install pre-commit hooks:\n{error.stderr.decode()}") from error

    def _commit_message(self) -> str:
        """Return the commit message to use when committing changes made by Copier."""
        action = "[ADD] Install" if self._is_fresh_install() else "[IMP] Update"

        return string.normalize_indent(
            f"""{action} `pre-commit` configuration

            Added automatically with [odev](https://github.com/odoo-odev/odev) using configuration templates
            from [pre-commit-template](https://github.com/odoo-ps/psbe-ps-tech-tools/tree/pre-commit-template).

            Odoo version: {self.version}
            See: [pre-commit](https://github.com/odoo-ps/psbe-process/wiki/Development-common-practices#pre-commit)
            """
        )

    def _commit_changes(self) -> None:
        """Commit changes made by Copier."""
        with progress.spinner("Committing changes"):
            try:
                assert self._repository.repository
                self._repository.repository.git.add(".")
                self._repository.repository.git.commit("-m", self._commit_message())
            except GitCommandError as error:
                raise self.error(f"Failed to commit changes:\n{error.stderr.strip()}") from error
