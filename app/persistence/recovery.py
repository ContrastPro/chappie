from app.market.models import MarketStreamKey
from app.persistence.exceptions import (
    PersistenceCheckpointError,
    PersistenceError,
    PersistenceIntegrityError,
    PersistenceReconciliationError,
)
from app.persistence.models import RecoveryResult, RecoveryStatus
from app.persistence.ports import RecoveryView


class RecoveryCoordinator:
    """Adapter-independent, read-only Portfolio chain reconciliation."""

    def recover(
        self,
        persistence: RecoveryView,
        portfolio_id: str,
        *,
        expected_stream_key: MarketStreamKey | None = None,
        expected_config_fingerprint: str | None = None,
    ) -> RecoveryResult:
        if not isinstance(persistence, RecoveryView):
            raise PersistenceReconciliationError("persistence does not implement RecoveryView")
        if not isinstance(portfolio_id, str) or not portfolio_id.strip():
            raise PersistenceReconciliationError("portfolio_id must be a non-empty string")
        try:
            integrity = persistence.integrity_check()
            if integrity != ("ok",):
                return self._required("database integrity check failed")
            initial = persistence.load_initial(portfolio_id)
            current = persistence.load_current(portfolio_id)
            transitions = persistence.load_transitions(portfolio_id)
            checkpoints = persistence.load_checkpoints(portfolio_id)
            checkpoint_diagnostics = tuple(
                getattr(persistence, "checkpoint_diagnostics", lambda _portfolio_id: ())(
                    portfolio_id
                )
            )
            lineage_issues = tuple(getattr(persistence, "reconciliation_issues", lambda: ())())
        except (PersistenceIntegrityError, PersistenceCheckpointError) as error:
            return self._required(str(error))
        except PersistenceError:
            raise
        except Exception as error:
            raise PersistenceReconciliationError("recovery view failed") from error

        if initial is None:
            if current is not None or transitions or checkpoints:
                return self._required("financial state exists without authoritative Portfolio root")
            return RecoveryResult(RecoveryStatus.CLEAN, None, None, 0, 0)
        if expected_stream_key is not None and initial.state.stream_key != expected_stream_key:
            return self._required("persisted Portfolio stream differs from expected identity")
        if (
            expected_config_fingerprint is not None
            and initial.config_fingerprint != expected_config_fingerprint
        ):
            return self._required("persisted Portfolio configuration fingerprint mismatch")
        if lineage_issues:
            return self._required(lineage_issues[0])
        if not transitions:
            if current != initial.state:
                return self._required("initial portfolio_current differs from Portfolio root")
            return RecoveryResult(RecoveryStatus.CLEAN, initial.state, None, 0, 0)

        current_version = current.version if current is not None else transitions[-1].version_after
        checkpoint = next(
            (
                item
                for item in checkpoints
                if item.portfolio_version <= current_version
                and item.state.version == item.portfolio_version
            ),
            None,
        )
        previous_digest = initial.state.digest
        last_state = initial.state
        expected_version = 0
        for transition in transitions:
            if transition.version_before != expected_version:
                return self._required("portfolio transition versions are not contiguous")
            if previous_digest is not None and transition.before_digest != previous_digest:
                return self._required("portfolio transition digest chain is broken")
            previous_digest = transition.after_state.digest
            expected_version = transition.version_after
            last_state = transition.after_state
        if current is not None and (last_state != current or last_state.digest != current.digest):
            return self._required("portfolio_current differs from journal reconstruction")
        if checkpoint is not None:
            checkpoint_state = next(
                (
                    transition.after_state
                    for transition in transitions
                    if transition.version_after == checkpoint.portfolio_version
                ),
                initial.state if checkpoint.portfolio_version == 0 else None,
            )
            if checkpoint_state != checkpoint.state:
                return self._required("checkpoint differs from authoritative journal history")
        base_version = checkpoint.portfolio_version if checkpoint else 0
        replayed = sum(transition.version_after > base_version for transition in transitions)
        last_journal_seq = getattr(
            getattr(persistence, "status", None), "last_journal_seq", len(transitions)
        )
        status = RecoveryStatus.RECOVERED if replayed else RecoveryStatus.CLEAN
        return RecoveryResult(
            status=status,
            portfolio_state=last_state,
            checkpoint_version=(checkpoint.portfolio_version if checkpoint else None),
            last_journal_seq=last_journal_seq,
            replayed_transitions=replayed,
            diagnostics=checkpoint_diagnostics,
        )

    @staticmethod
    def _required(message: str) -> RecoveryResult:
        return RecoveryResult(
            status=RecoveryStatus.RECOVERY_REQUIRED,
            portfolio_state=None,
            checkpoint_version=None,
            last_journal_seq=0,
            replayed_transitions=0,
            unresolved_conflicts=1,
            diagnostics=(message,),
        )
