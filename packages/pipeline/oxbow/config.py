"""Typed access to config/.

00 G: every tunable value lives in config/ or design/tokens.css, not in code.
This module is the only sanctioned way to read one, and it fails loud at the
boundary: a missing key or a bad type is a startup error, never a silent default.

There is deliberately no "return a sensible default if the key is absent"
behaviour. 03 A rule 1: fail loud at the boundary, degrade gracefully in the
middle, never fail silent anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

CONFIG_DIRNAME: Final = "config"
PIPELINE_FILENAME: Final = "pipeline.yaml"

_REQUIRED_TOP_LEVEL: Final = ("seed", "deployment_timezone")


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed, or internally inconsistent.

    This is a boundary failure and it is loud on purpose. 03 A rule 1.
    """


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """The subset of pipeline.yaml the CLI and early stages need.

    Frozen: configuration is read once at the boundary and never mutated, so a
    stage cannot quietly change the seed out from under a later stage.
    """

    root: Path
    seed: int
    deployment_timezone: str
    raw: dict[str, Any]

    @property
    def paysim(self) -> dict[str, Any]:
        """PaySim step-expansion settings: the synthetic timestamp rule."""
        return dict(self.raw["paysim"])

    @property
    def graph(self) -> dict[str, Any]:
        """Graph build settings, including the rail percentile and node cap."""
        return dict(self.raw["graph"])

    @property
    def determinism(self) -> dict[str, Any]:
        """Parquet and hashing settings that make two runs byte-identical."""
        return dict(self.raw["determinism"])

    @property
    def runtime(self) -> dict[str, Any]:
        """Checkpoint and resume settings."""
        return dict(self.raw["runtime"])

    @property
    def sampling(self) -> dict[str, Any]:
        """Subcorpus sampling policy for the interactive product."""
        return dict(self.raw["sampling"])


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the repository root by finding config/pipeline.yaml.

    Walks upward from ``start`` (default: cwd). Returns the first directory that
    holds the config file. Raises rather than guessing, because loading the wrong
    config is exactly the silent failure 03 A rule 1 forbids.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / CONFIG_DIRNAME / PIPELINE_FILENAME).is_file():
            return candidate
    raise ConfigError(
        f"No {CONFIG_DIRNAME}/{PIPELINE_FILENAME} found above {here}. "
        "Run from inside the OXBOW repository, or pass an explicit config_dir."
    )


def load_yaml(path: Path) -> dict[str, Any]:
    """Read one YAML config file, failing loud on anything unexpected."""
    if not path.is_file():
        raise ConfigError(f"Required config file is missing: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level, got {type(loaded)}")
    return loaded


def load_pipeline_config(root: Path | None = None) -> PipelineConfig:
    """Load and validate pipeline.yaml.

    Validates the keys the whole build depends on, so a typo in the seed or a
    missing timezone fails at the boundary instead of producing a run whose
    timestamps are subtly wrong.
    """
    repo_root = (root or find_repo_root()).resolve()
    raw = load_yaml(repo_root / CONFIG_DIRNAME / PIPELINE_FILENAME)

    missing = [key for key in _REQUIRED_TOP_LEVEL if key not in raw]
    if missing:
        raise ConfigError(
            f"{CONFIG_DIRNAME}/{PIPELINE_FILENAME} is missing required keys: {missing}"
        )

    seed = raw["seed"]
    if not isinstance(seed, int):
        raise ConfigError(f"seed must be an int, got {type(seed).__name__}")
    if seed != 1337:
        # 01 A rule 4: determinism before features. The global seed is 1337 and a
        # different value means two runs of `make pipeline` cannot be compared.
        raise ConfigError(
            f"seed is {seed}, but the project contract fixes the global seed at 1337 "
            "(01 A rule 4). Change it deliberately and record it in DECISIONS.md."
        )

    timezone = raw["deployment_timezone"]
    if not isinstance(timezone, str) or not timezone:
        raise ConfigError("deployment_timezone must be a non-empty IANA zone string")

    return PipelineConfig(
        root=repo_root,
        seed=seed,
        deployment_timezone=timezone,
        raw=raw,
    )


def require_run_salt() -> str:
    """Fetch RUN_SALT from the environment, failing loud when it is absent.

    01 A rule 8: the salt is per-run and lives in the environment, not the repo.
    02 F: the mapping table is excluded from exports; re-identification requires
    the salt, so losing it is what makes erasure possible (03 L).
    """
    salt = os.environ.get("RUN_SALT", "")
    if not salt:
        raise ConfigError(
            "RUN_SALT is not set. It must come from the environment and never from the "
            "repo (01 A rule 8). Export it, or source .env, before running a stage."
        )
    return salt


__all__ = [
    "ConfigError",
    "PipelineConfig",
    "find_repo_root",
    "load_pipeline_config",
    "load_yaml",
    "require_run_salt",
]
