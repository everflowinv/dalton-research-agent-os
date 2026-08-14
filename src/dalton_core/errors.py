"""Errors raised by the Dalton Core storage boundary."""


class DaltonStoreError(Exception):
    """Base class for deterministic storage and gate failures."""


class StoreError(DaltonStoreError):
    """Compatibility alias for callers that use the shorter base name."""


class ValidationError(DaltonStoreError):
    pass


class InvocationConflict(DaltonStoreError):
    """An invocation id was reused for a different immutable payload."""


class AuthorizationError(DaltonStoreError):
    pass


class ImmutableViolation(DaltonStoreError):
    pass


class GateRejected(DaltonStoreError):
    pass


class VerificationRequired(GateRejected):
    pass


class BadVerdict(GateRejected):
    pass


class IndependenceViolation(GateRejected):
    pass


class IdempotencyConflict(DaltonStoreError):
    pass


class DuplicateCommit(DaltonStoreError):
    """Optional signal for callers that prefer an exception for duplicates."""


class NotFound(DaltonStoreError):
    pass
