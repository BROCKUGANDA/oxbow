"""MLflow 2.x lineage on a local file store, degrading loudly instead of quietly.

Plan §10 / 02 §B seam 4: every scored row carries the model URI and version that
produced it, so a number on the UI can be traced to the fit that made it. Without
that, "the model says 0.31" is an opinion; with it, the row names the run, the fold,
the seed and the feature hash behind it, and a reviewer can re-fit and disagree on
evidence.

Two constraints shape this module, and they pull against each other:

* **The offline demo must work.** Compose is not up, nothing is listening on port
  5000, and 02 §B makes the null adapter the default. So the tracking URI in
  ``config/model.yaml`` is *attempted*, and the local file store under
  ``mlflow.local_store_dir`` is the fallback. Which one was actually used is written
  into the payload -- silently pretending the server was reached is the failure this
  module exists to prevent.
* **A missing registry must not destroy a score.** Losing lineage is a degraded run,
  not a wrong number: the score keeps its calibration and its SHAP, and the UI shows
  its degraded banner. So every mlflow call is guarded, the reason is named, and a
  lineage record is still written to disk. The refusal to *pretend* is the point: a
  swallowed exception would be indistinguishable from a logged run, which is exactly
  the fiction the plan forbids.

The reachability probe is a TCP ``socket.connect`` with a short timeout, not an HTTP
request. Two reasons, both load-bearing: mlflow's own client retries a dead server for
about 70 seconds (measured on this box), which would make the offline demo hang; and
import-linter contract 3 forbids this layer importing an HTTP client. The probe
answers the only question that matters -- is anything listening?
"""

from __future__ import annotations

import json
import socket
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from oxbow.models.config import MlflowConfig
from oxbow.models.errors import TrackingUnavailableError

LOCAL_STORE_SUBDIR: Final = "mlruns"
LINEAGE_FILENAME: Final = "lineage.jsonl"
MODEL_ARTIFACT_PATH: Final = "gbm"
PROBE_TIMEOUT_SECONDS: Final = 1.0

STORE_SERVER: Final = "mlflow_server"
STORE_FILE: Final = "mlflow_file_store"
STORE_NONE: Final = "unavailable_local_lineage_only"


def file_store_uri(root: Path, local_store_dir: str) -> str:
    """The ``file://`` URI of the declared local store, created if absent."""
    directory = Path(root) / local_store_dir / LOCAL_STORE_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory.resolve().as_uri()


def parse_host_port(tracking_uri: str) -> tuple[str, int]:
    """Host and port of an HTTP tracking URI, defaulting the way a URL does."""
    remainder = tracking_uri.split("://", 1)[-1]
    host, _, port_text = remainder.partition(":")
    host = host.split("/", 1)[0] or "localhost"
    if not port_text:
        return host, 80
    digits = port_text.split("/", 1)[0]
    return host, int(digits) if digits.isdigit() else 80


def server_listening(host: str, port: int, timeout: float) -> tuple[bool, str]:
    """True only when a TCP connection to the tracking server actually succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} accepted a connection within {timeout:.1f}s"
    except OSError as exc:
        return False, f"{host}:{port} refused ({type(exc).__name__}: {exc})"


def resolve_tracking_uri(
    cfg: MlflowConfig, root: Path, probe_timeout: float = PROBE_TIMEOUT_SECONDS
) -> tuple[str, str, bool, str]:
    """Decide which tracking URI to use, and say why in the returned reason.

    Returns ``(uri_used, store_kind, degraded, reason)``. The file store is the
    offline path the plan names; the server is used only when something is
    demonstrably listening on its host and port.
    """
    if not cfg.tracking_uri.startswith("http"):
        # A sqlite or file URI in config is already a store the client opens directly.
        return (
            cfg.tracking_uri,
            STORE_FILE,
            False,
            f"config tracking_uri is not HTTP: {cfg.tracking_uri!r} used as given",
        )
    host, port = parse_host_port(cfg.tracking_uri)
    listening, detail = server_listening(host, port, probe_timeout)
    if listening:
        return cfg.tracking_uri, STORE_SERVER, False, f"tracking server reachable: {detail}"
    if not cfg.offline_fallback_allowed:
        raise TrackingUnavailableError(
            f"mlflow.tracking_uri={cfg.tracking_uri!r} is unreachable ({detail}) and "
            "mlflow.offline_fallback_allowed is false, so lineage cannot be recorded. "
            "Refusing to run as if it were."
        )
    uri = file_store_uri(root, cfg.local_store_dir)
    return (
        uri,
        STORE_FILE,
        True,
        f"tracking server unreachable: {detail}. Fell back to the local file store at "
        f"{uri} because mlflow.offline_fallback_allowed is true. The run is DEGRADED on "
        "lineage only; the scores themselves are unaffected.",
    )


@dataclass(frozen=True, slots=True)
class TrackingStatus:
    """What the registry resolved to, including the reason it is degraded."""

    tracking_uri_requested: str
    tracking_uri_used: str
    store_kind: str
    degraded: bool
    reason: str
    local_store_dir: Path
    module: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "tracking_uri_requested": self.tracking_uri_requested,
            "tracking_uri_used": self.tracking_uri_used,
            "store_kind": self.store_kind,
            "degraded": self.degraded,
            "reason": self.reason,
            "local_store_dir": str(self.local_store_dir),
            "mlflow_available": self.module is not None,
        }


@dataclass(frozen=True, slots=True)
class ModelLineage:
    """The trace a scored row carries: which model version, in which run, where."""

    run_id: str | None
    model_uri: str | None
    model_version: str | None
    registered_model_name: str | None
    experiment: str
    tracking_uri_used: str
    store_kind: str
    degraded: bool
    reason: str
    model_fingerprint: str | None

    def to_row(self) -> dict[str, object]:
        """The columns written onto every scored row (02 §B seam 4).

        Nulls are explicit rather than absent: a row whose ``model_version`` is null
        says "lineage was lost and the run knows it", which is a different fact from a
        row from a build that never had the column at all.
        """
        return {
            "mlflow_run_id": self.run_id,
            "model_uri": self.model_uri,
            "model_version": self.model_version,
            "registered_model_name": self.registered_model_name,
            "model_fingerprint": self.model_fingerprint,
            "tracking_uri_used": self.tracking_uri_used,
            "tracking_degraded": self.degraded,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.to_row(),
            "experiment": self.experiment,
            "store_kind": self.store_kind,
            "reason": self.reason,
        }


@dataclass(slots=True)
class TrackedRun:
    """One MLflow run for one fold, with every failure surfaced rather than swallowed.

    Used as a context manager. If mlflow cannot be reached the object still writes the
    fold's record to ``lineage.jsonl`` on disk, so the artefacts exist and the run is
    labelled degraded instead of silently untracked.
    """

    cfg: MlflowConfig
    status: TrackingStatus
    run_name: str
    tags: Mapping[str, object]
    _mlflow: Any = field(default=None, repr=False, compare=False)
    _active: bool = field(default=False, repr=False, compare=False)
    _run_id: str | None = field(default=None, repr=False, compare=False)
    _failures: list[str] = field(default_factory=list, repr=False, compare=False)
    _metrics: dict[str, float] = field(default_factory=dict, repr=False, compare=False)
    _params: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    _artifacts: list[str] = field(default_factory=list, repr=False, compare=False)

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(self._failures)

    @property
    def registered_name(self) -> str:
        return f"{self.cfg.registered_model_prefix}-{self.run_name}"

    def log_metrics(self, metrics: Mapping[str, float]) -> None:
        clean = {name: float(value) for name, value in metrics.items() if value == value}
        self._metrics.update(clean)
        self._guarded(lambda module: module.log_metrics(clean), "log_metrics")

    def log_params(self, params: Mapping[str, object]) -> None:
        strings = {name: str(value)[:480] for name, value in params.items()}
        self._params.update(strings)
        self._guarded(lambda module: module.log_params(strings), "log_params")

    def log_artifact(self, path: Path, artifact_name: str | None = None) -> None:
        self._artifacts.append(str(path))
        self._guarded(lambda module: module.log_artifact(str(path), artifact_name), "log_artifact")

    def log_reliability_curve(self, bins: Sequence[Mapping[str, object]]) -> None:
        """The curve as a JSON artefact *and* as per-bin metrics.

        Both, because they answer different questions: the artefact is what the UI
        plots, the metrics make each bin searchable and diffable between runs, which is
        how a reviewer notices that band 9's observed rate moved between folds.
        """
        payload = {"reliability_bins": list(bins), "run_name": self.run_name}
        directory = Path(tempfile.mkdtemp(prefix="oxbow-reliability-"))
        curve_path = directory / "reliability_curve.json"
        curve_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self.log_artifact(curve_path, "reliability_curve.json")
        self.log_metrics(
            {
                **{
                    f"reliability/bin{entry['bin_index']}_observed_rate": float(
                        entry["observed_rate"]
                    )
                    for entry in bins
                },
                **{
                    f"reliability/bin{entry['bin_index']}_n": float(entry["sample_size"])
                    for entry in bins
                },
            }
        )

    def log_model(self, booster: object, fingerprint: str) -> ModelLineage:
        """Log and register the booster; return the lineage every scored row needs."""
        if not self.cfg.log_models:
            return self._lineage(None, None, fingerprint, "mlflow.log_models is false in config")
        state: dict[str, object] = {}

        def action(module: Any) -> None:
            info = module.lightgbm.log_model(booster, artifact_path=MODEL_ARTIFACT_PATH)
            uri = str(info.model_uri)
            state["model_uri"] = uri
            try:
                # ``name=`` is not forwarded to the flavour's save_model in mlflow 2.19
                # (measured here: TypeError), so registration is an explicit second call.
                registered = module.register_model(uri, self.registered_name)
                state["model_version"] = getattr(registered, "version", None)
            except Exception as exc:
                state["register_error"] = f"register_model failed: {type(exc).__name__}: {exc}"

        self._guarded(action, "log_model")
        error = state.get("register_error")
        return self._lineage(
            state.get("model_uri"),
            state.get("model_version"),
            fingerprint,
            None if error is None else str(error),
        )

    def _lineage(
        self, model_uri: object, model_version: object, fingerprint: str, reason: str | None
    ) -> ModelLineage:
        parts = [part for part in (self.status.reason, reason) if part]
        return ModelLineage(
            run_id=self._run_id,
            model_uri=None if model_uri is None else str(model_uri),
            model_version=None if model_version is None else str(model_version),
            registered_model_name=self.registered_name,
            experiment=self.cfg.experiment,
            tracking_uri_used=self.status.tracking_uri_used,
            store_kind=self.status.store_kind,
            degraded=self.status.degraded or model_uri is None,
            reason="; ".join(parts),
            model_fingerprint=fingerprint,
        )

    def _guarded(self, action: Any, label: str) -> None:
        """Run an mlflow call, or record exactly why it did not happen."""
        module = self._mlflow
        if module is None:
            self._failures.append(
                f"{label} skipped: tracking unavailable "
                f"({self.status.reason or self.status.store_kind})"
            )
            return
        try:
            action(module)
        except Exception as exc:
            self._failures.append(f"{label} failed: {type(exc).__name__}: {str(exc)[:200]}")

    def __enter__(self) -> TrackedRun:
        """Open the run imperatively and leave it active for the caller's body.

        ``mlflow.start_run()`` rather than its context-manager form: a context manager
        entered and exited inside ``__enter__`` would close the run before the caller
        logged anything, and the fold's metrics would land nowhere while every call
        still reported success.
        """
        module = self.status.module
        if module is None:
            return self
        try:
            module.set_tracking_uri(self.status.tracking_uri_used)
            module.set_experiment(self.cfg.experiment)
            run = module.start_run(run_name=self.run_name)
            self._run_id = run.info.run_id
            self._mlflow = module
            self._active = True
            module.set_tags({key: str(value) for key, value in self.tags.items()})
        except Exception as exc:
            self._failures.append(f"start_run failed: {type(exc).__name__}: {str(exc)[:200]}")
            self._mlflow = None
            self._active = False
            self._run_id = None
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._active and self._mlflow is not None:
            try:
                self._mlflow.end_run()
            except Exception as exc:
                self._failures.append(f"end_run failed: {type(exc).__name__}: {str(exc)[:200]}")
            self._active = False
            self._mlflow = None
        error = None if exc_info[0] is None else f"{type(exc_info[0]).__name__}: {exc_info[1]}"
        self.write_lineage_record(error=error)

    def write_lineage_record(self, error: str | None = None) -> Path:
        """Append this run's record to the local lineage file, tracked or not.

        This is the artefact that makes degradation *visible*: a judge opening the file
        store finds one line per fold saying which store was used, which metrics were
        meant to be logged, and which calls did not happen.
        """
        record = {
            "run_name": self.run_name,
            "run_id": self._run_id,
            "experiment": self.cfg.experiment,
            "tracking_uri_requested": self.status.tracking_uri_requested,
            "tracking_uri_used": self.status.tracking_uri_used,
            "store_kind": self.status.store_kind,
            "degraded": self.status.degraded or self._run_id is None,
            "tags": dict(self.tags),
            "metrics": self._metrics,
            "params": self._params,
            "artifacts": list(self._artifacts),
            "failures": list(self._failures),
            "error": error,
        }
        path = Path(self.status.local_store_dir) / LINEAGE_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        return path


class ModelRegistry:
    """The tracking seam P4b and P6 both use, resolved once per process.

    Deliberately not a module-level singleton, and resolved in the constructor rather
    than per fold: the resolution is a network probe with a timeout, and a
    walk-forward over five folds must not pay it five times for the same answer.
    """

    def __init__(
        self,
        cfg: MlflowConfig,
        *,
        root: Path,
        probe_timeout: float = PROBE_TIMEOUT_SECONDS,
        force_store_uri: str | None = None,
    ) -> None:
        self._cfg = cfg
        local_dir = (Path(root) / cfg.local_store_dir).resolve()
        local_dir.mkdir(parents=True, exist_ok=True)
        if force_store_uri is not None:
            uri, kind, degraded, reason = (
                force_store_uri,
                STORE_FILE,
                False,
                f"explicit store override (caller-supplied): {force_store_uri}",
            )
        else:
            uri, kind, degraded, reason = resolve_tracking_uri(cfg, Path(root), probe_timeout)
        module, module_error = _load_mlflow()
        if module is None:
            kind = STORE_NONE
            degraded = True
            reason = (
                f"mlflow import failed: {module_error}; lineage falls back to "
                f"{LINEAGE_FILENAME} on disk and no model is registered"
            )
            uri = ""
        self._status = TrackingStatus(
            tracking_uri_requested=cfg.tracking_uri,
            tracking_uri_used=uri,
            store_kind=kind,
            degraded=degraded,
            reason=reason,
            local_store_dir=local_dir,
            module=module,
        )

    @property
    def cfg(self) -> MlflowConfig:
        return self._cfg

    @property
    def status(self) -> TrackingStatus:
        return self._status

    def open_run(self, *, run_name: str, tags: Mapping[str, object]) -> TrackedRun:
        """A run handle usable as a context manager; never raises for a tracking fault."""
        return TrackedRun(cfg=self._cfg, status=self._status, run_name=run_name, tags=dict(tags))

    def to_dict(self) -> dict[str, object]:
        return self._status.to_dict()


def _load_mlflow() -> tuple[Any, str | None]:
    """Import mlflow lazily.

    Lazy on purpose: importing mlflow costs seconds, and the read path must not pay
    that for a fold it only wants to display. A missing mlflow is a degraded run with
    a named reason, not an ImportError at module load.
    """
    try:
        import mlflow

        return mlflow, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


__all__ = [
    "LINEAGE_FILENAME",
    "LOCAL_STORE_SUBDIR",
    "MODEL_ARTIFACT_PATH",
    "PROBE_TIMEOUT_SECONDS",
    "STORE_FILE",
    "STORE_NONE",
    "STORE_SERVER",
    "ModelLineage",
    "ModelRegistry",
    "TrackedRun",
    "TrackingStatus",
    "file_store_uri",
    "parse_host_port",
    "resolve_tracking_uri",
    "server_listening",
]
