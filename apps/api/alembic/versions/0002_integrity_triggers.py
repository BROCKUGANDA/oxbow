"""integrity: run immutability, append-only chains, and schema comments

Revision ID: 0002_integrity
Revises: 7907d04c69bc
Create Date: 2026-09-26

Hand-written, deliberately. Autogenerate can produce the columns and the
constraints it can see in the metadata, but three rules in this schema exist only
in the database and cannot be expressed as a type annotation:

* **a run is immutable once complete** (plan §13). "Rescoring creates a new run" is
  the invariant the whole audit story rests on: a packet that pins a run has to be
  able to say what that run said. A trigger makes that a fact about the database
  rather than a habit of the code that writes to it.
* **decisions and audit events are append-only** (plan §15). A reversal is a new row
  referencing the original, so UPDATE of the substantive columns is refused, and
  DELETE is refused outright. The four-eyes confirmation columns are the one
  permitted update, because second review genuinely does arrive later.
* **money and identity types are decisions, not defaults** (DEV-003, DEV-004,
  DEV-005). ``COMMENT ON`` puts the reasoning where someone inspecting the schema
  will find it, instead of in a README nobody opens before a bad ALTER TABLE.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_integrity"
down_revision: str | None = "7907d04c69bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables whose rows belong to a run and are written once by the pipeline. A new
# read-model table added later belongs in this list; ``test_run_immutability_trigger_covers_
# every_pipeline_table`` fails if it does not, so the list cannot silently fall behind
# the metadata.
RUN_SCOPED_TABLES: tuple[str, ...] = (
    "account",
    "ablation_row",
    "backtest_fold",
    "band_definition",
    "batch",
    "community",
    "confusion_cell",
    "curve_point",
    "drift_period",
    "economics",
    "fairness_row",
    "graph_edge",
    "model_disagreement",
    "account_membership",
    "perturbation_row",
    "policy_allocation",
    "policy_simulation",
    "policy_summary",
    "quarantine_row",
    "rating_migration",
    "rule_hit",
    "score",
    "scorecard_attribute",
    "scorecard_bin",
    "scorecard_point",
    "scorecard_spec",
    "shap_contribution",
    "stage_event",
    "transaction",
    "validation_metric",
)

# Substantive decision columns: changing any of them after the fact would rewrite
# history, which is precisely what the chain exists to detect.
IMMUTABLE_DECISION_COLUMNS: tuple[str, ...] = (
    "chain_seq",
    "case_id",
    "run_id",
    "account_key",
    "decision_seq",
    "action",
    "reason",
    "actor_id",
    "actor_roles",
    "exposure_minor",
    "currency",
    "four_eyes_required",
    "reversal_of_decision_id",
    "decided_on_superseded_run",
    "case_version_after",
    "idempotency_key",
    "trace_id",
    "occurred_at",
    "prev_hash",
    "row_hash",
    "payload",
)


def _fn_guard_run_mutable() -> None:
    op.execute(
        """
        CREATE FUNCTION guard_run_mutable() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            IF NEW.run_id IS NULL THEN
                RETURN NEW;
            END IF;
            IF EXISTS (
                SELECT 1 FROM run
                WHERE run.run_id = NEW.run_id
                  AND run.state IN ('complete', 'superseded')
            ) THEN
                RAISE EXCEPTION
                    'run % is %: rows in % may not be added to a completed run. '
                    'Rescoring creates a new run (plan §13).',
                    NEW.run_id,
                    (SELECT state FROM run WHERE run.run_id = NEW.run_id),
                    TG_TABLE_NAME
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $fn$;
        """
    )


def _fn_guard_run_immutable() -> None:
    op.execute(
        """
        CREATE FUNCTION guard_run_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            IF OLD.state IN ('complete', 'superseded') AND NEW.state IS DISTINCT FROM OLD.state THEN
                RAISE EXCEPTION
                    'run % is %: its lifecycle state is frozen (plan §13)', OLD.run_id, OLD.state
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.state IN ('complete', 'superseded') AND (
                OLD.created_at IS DISTINCT FROM NEW.created_at
                OR OLD.seed IS DISTINCT FROM NEW.seed
                OR OLD.config_hash IS DISTINCT FROM NEW.config_hash
                OR OLD.model_version IS DISTINCT FROM NEW.model_version
                OR OLD.provenance IS DISTINCT FROM NEW.provenance
                OR OLD.timezone IS DISTINCT FROM NEW.timezone
            ) THEN
                RAISE EXCEPTION
                    'run % is %: its identity columns are frozen', OLD.run_id, OLD.state
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $fn$;
        """
    )


def _fn_guard_decision_append_only() -> None:
    op.execute(
        """
        CREATE FUNCTION guard_decision_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'decision % is append-only: a reversal is a NEW row referencing the '
                    'original (plan §15)', OLD.decision_id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF (NEW.four_eyes_state IS DISTINCT FROM OLD.four_eyes_state)
               AND OLD.four_eyes_state = 'confirmed' THEN
                RAISE EXCEPTION
                    'decision % is already confirmed by a second reviewer; the four-eyes '
                    'state does not change again', OLD.decision_id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.confirmed_by IS DISTINCT FROM OLD.confirmed_by
               AND OLD.confirmed_by IS NOT NULL THEN
                RAISE EXCEPTION
                    'decision % already records a second reviewer', OLD.decision_id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.case_id IS DISTINCT FROM OLD.case_id
               OR NEW.reason IS DISTINCT FROM OLD.reason THEN
                RAISE EXCEPTION
                    'decision %: reason and case linkage are frozen at write time',
                    OLD.decision_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $fn$;
        """
    )


def _fn_guard_audit_append_only() -> None:
    op.execute(
        """
        CREATE FUNCTION guard_audit_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            RAISE EXCEPTION
                'audit_event % is append-only: no update and no delete (02 §F)', OLD.audit_id
                USING ERRCODE = 'check_violation';
        END
        $fn$;
        """
    )


def _fn_guard_pseudonym_delete() -> None:
    op.execute(
        """
        CREATE FUNCTION guard_pseudonym_delete() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            IF OLD.salt_version IS NULL THEN
                RAISE EXCEPTION 'pseudonym_map row for % has no salt version', OLD.account_key;
            END IF;
            RETURN OLD;
        END
        $fn$;
        """
    )


def upgrade() -> None:
    _fn_guard_run_mutable()
    _fn_guard_run_immutable()
    _fn_guard_decision_append_only()
    _fn_guard_audit_append_only()
    _fn_guard_pseudonym_delete()

    for table in RUN_SCOPED_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_run_mutable
            BEFORE INSERT OR UPDATE OF run_id ON {table}
            FOR EACH ROW EXECUTE FUNCTION guard_run_mutable()
            """
        )
    op.execute(
        """
        CREATE TRIGGER trg_run_immutable
        BEFORE UPDATE ON run
        FOR EACH ROW EXECUTE FUNCTION guard_run_immutable()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_run_no_delete
        BEFORE DELETE ON run
        FOR EACH ROW EXECUTE FUNCTION guard_run_immutable()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_decision_append_only
        BEFORE UPDATE OR DELETE ON decision
        FOR EACH ROW EXECUTE FUNCTION guard_decision_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_append_only
        BEFORE UPDATE OR DELETE ON audit_event
        FOR EACH ROW EXECUTE FUNCTION guard_audit_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pseudonym_delete
        BEFORE DELETE ON pseudonym_map
        FOR EACH ROW EXECUTE FUNCTION guard_pseudonym_delete()
        """
    )

    # --- the decisions, next to the columns they constrain ------------------
    op.execute(
        """
        COMMENT ON COLUMN "transaction".amount_minor IS
        'Integer minor units (DEV-005 / C5). Never numeric, never float: 0.10 has no '
        'exact binary representation, and money that cannot be represented cannot be summed.'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN "transaction".txn_id IS
        'Corpus-namespaced text, e.g. paysim:12345 (DEV-004 / C4). A bigint primary key '
        'would merge two corpora''s row 41 into one transaction belonging to neither.'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN run.run_id IS
        'ULID stored as char(26) text, never native uuid (DEV-003 / C3): the SSE cursor and '
        '"rescoring is a new run" both need a key that sorts by creation time on its own.'
        """
    )
    op.execute(
        """
        COMMENT ON TABLE review_case IS
        'One account pinned to one run. A case pins its run so a packet shows the evidence as '
        'it was at decision time, even after a later run supersedes it (plan §15).'
        """
    )
    op.execute(
        """
        COMMENT ON TABLE outbox IS
        'The outbox: written in the same Postgres transaction as the decision it carries. '
        'The database is the only source of truth about what has been sent (02 §E).'
        """
    )
    op.execute(
        """
        COMMENT ON TABLE pseudonym_map IS
        'The only table holding a salt. Erasure deletes from here: every downstream reference '
        'becomes unrecoverable while every digest still verifies (02 §F).'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN economics.assumptions IS
        'The recovery rate, analyst cost and friction cost this row''s money figures are a '
        'function of. Stored with the figures on purpose: a later edit of economics.yaml must '
        'not reinterpret an older run (plan §13).'
        """
    )


def downgrade() -> None:
    for statement in (
        "DROP TRIGGER IF EXISTS trg_pseudonym_delete ON pseudonym_map",
        "DROP TRIGGER IF EXISTS trg_audit_append_only ON audit_event",
        "DROP TRIGGER IF EXISTS trg_decision_append_only ON decision",
        "DROP TRIGGER IF EXISTS trg_run_no_delete ON run",
        "DROP TRIGGER IF EXISTS trg_run_immutable ON run",
    ):
        op.execute(statement)
    for table in RUN_SCOPED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_run_mutable ON {table}")
    for function in (
        "guard_pseudonym_delete",
        "guard_audit_append_only",
        "guard_decision_append_only",
        "guard_run_immutable",
        "guard_run_mutable",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")


__all__ = ["IMMUTABLE_DECISION_COLUMNS", "RUN_SCOPED_TABLES", "downgrade", "revision", "upgrade"]
