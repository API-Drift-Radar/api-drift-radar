"""Errors raised by the storage component."""


class StoreError(RuntimeError):
    """Stored data is missing, corrupted, or could not be written."""


class NotFoundError(LookupError):
    """No record exists with the requested identifier."""


class ConflictError(RuntimeError):
    """The request no longer matches stored state.

    For example: finishing a check that has already finished, or recording a
    comparison against a snapshot that is no longer the latest valid one.
    """