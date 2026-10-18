from __future__ import annotations

import os
import subprocess

GIT_LOCAL_ENV_VARS_FALLBACK = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
)


def _git_local_env_vars() -> set[str]:
    names = set(GIT_LOCAL_ENV_VARS_FALLBACK)
    env_without_git = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        listed = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"],
            env=env_without_git, capture_output=True, text=True, check=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return names
    return names | set(listed)


def scrub_inherited_git_environment() -> None:
    redirecting = _git_local_env_vars()
    for key in list(os.environ):
        if key in redirecting or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            del os.environ[key]
