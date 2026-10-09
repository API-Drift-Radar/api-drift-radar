import os
import tempfile


def write_file_atomically(path, text, replace=False):
    """Write ``text`` to ``path`` so readers never see a partial file.

    The text goes to a temporary file in the same directory first, and is
    only moved into place once it is fully written and synced. If anything
    fails, an existing file at ``path`` is left unchanged and the temporary
    file is removed.

    Without ``replace``, an existing file is never overwritten, even one
    created while this call was running. With ``replace``, an existing
    file (or symlink) at ``path`` is atomically swapped out; a symlink
    itself is replaced, never written through.
    """
    path = os.fspath(path)
    if not replace and os.path.lexists(path):
        raise _exists_error(path)

    directory = os.path.dirname(os.path.abspath(path))
    temp_path = None
    try:
        # The temporary file is created owner-read/write only (0600).
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        if replace:
            os.replace(temp_path, path)
        else:
            # Unlike os.replace, linking fails if the file appeared since
            # the check above, so a concurrent writer is never overwritten.
            os.link(temp_path, path)
            os.unlink(temp_path)
        temp_path = None
    except FileExistsError:
        raise _exists_error(path) from None
    except OSError as error:
        raise RuntimeError(f"Could not write {path}: {error}") from error
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    _sync_directory(directory)


def _exists_error(path):
    return FileExistsError(f"{path} already exists. Pass replace=True to overwrite it.")


def _sync_directory(directory):
    """Persist the rename itself. Best effort: the file is already in place."""
    if os.name == "nt":
        return  # Windows cannot open a directory for fsync.
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass
