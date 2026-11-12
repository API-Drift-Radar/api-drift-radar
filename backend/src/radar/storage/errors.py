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


class CheckInProgress(ConflictError):
    """A check of the source is already running, so another one was not started.

    ``check_id`` is the running check, so a caller can report or follow it.
    """

    def __init__(self, source_id, check_id):
        super().__init__(f"Source {source_id} already has a running check: {check_id}.")
        self.source_id = source_id
        self.check_id = check_id
