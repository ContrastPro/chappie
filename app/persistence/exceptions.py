class PersistenceError(Exception):
    """Base error for the persistence subsystem."""


class PersistenceConfigurationError(PersistenceError):
    pass


class PersistenceConnectionError(PersistenceError):
    pass


class PersistenceMigrationError(PersistenceError):
    pass


class PersistenceSchemaError(PersistenceError):
    pass


class PersistenceTransactionError(PersistenceError):
    pass


class PersistenceIntegrityError(PersistenceError):
    pass


class PersistenceConflictError(PersistenceIntegrityError):
    pass


class PersistenceSerializationError(PersistenceError):
    pass


class PersistenceRecoveryError(PersistenceError):
    pass


class PersistenceRecoveryRequiredError(PersistenceRecoveryError):
    pass


class PersistenceDecisionSlotConflictError(
    PersistenceRecoveryRequiredError, PersistenceConflictError
):
    pass


class PersistenceWriterConflictError(PersistenceTransactionError):
    pass


class PersistenceCheckpointError(PersistenceRecoveryError):
    pass


class PersistenceReconciliationError(PersistenceRecoveryError):
    pass
