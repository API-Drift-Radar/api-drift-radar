"""Immutable, content-addressed contract bundles under ``.radar/contracts/``.

Each stored contract is one directory named by its fingerprint::

    contracts/<fingerprint>/
        manifest.json     what is stored, with a SHA-256 for every file
        original/<path>   the root definition and its referenced files,
                          byte for byte as captured
        normalized        the comparison representation

The fingerprint is the SHA-256 of the canonical manifest, so it identifies
the original files, the normalized form and the normalization version
together. Identical content maps to the same directory and is stored once.

A bundle is assembled under a temporary ``.incoming-*`` name and renamed into
place only after every file is written and synced, so a fingerprint
directory is never partial. Bundles are never modified or removed.

A crash part-way through a write leaves its ``.incoming-*`` directory
behind. Nothing refers to it, so it is harmless, and ``remove_abandoned``
deletes such directories once they are old enough that no live writer can
still be using them. A crash after the rename but before the database
records are written leaves a complete bundle that no snapshot refers to; it
is kept, and reused if the same content is captured again.

This module stores bytes. It does not parse, validate or compare contracts;
SQLite records that refer to bundles are written elsewhere, and only after
``put`` has returned.
"""

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from typing import Mapping

from radar.storage.errors import StoreError

MANIFEST_FORMAT = 1
MANIFEST_NAME = "manifest.json"
ORIGINAL_DIRECTORY = "original"
NORMALIZED_NAME = "normalized"
INCOMING_PREFIX = ".incoming-"
# A write takes seconds; anything this old was abandoned by a crashed writer.
ABANDONED_AFTER_SECONDS = 60 * 60
_INCOMING_NAME = re.compile(re.escape(INCOMING_PREFIX) + r"[0-9a-f]{32}")

# Characters Windows does not allow in file names, plus the separators.
_FORBIDDEN_CHARACTERS = frozenset('<>:"\\|?*')
_MAX_PATH_LENGTH = 512
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{device}{n}" for device in ("COM", "LPT") for n in "123456789¹²³"}
)


@dataclass(frozen=True)
class StoredContract:
    """A bundle read back from disk, already checked against its manifest."""

    fingerprint: str
    entry_point: str
    documents: Mapping[str, bytes]
    normalized: bytes
    normalization_version: str

    @property
    def original(self):
        """The root definition exactly as captured."""
        return self.documents[self.entry_point]


class ContractFiles:
    """The ``contracts/`` directory of a Radar data directory."""

    def __init__(self, directory):
        self.directory = os.fspath(directory)

    def put(self, entry_point, documents, normalized, normalization_version):
        """Store a bundle if it is not already present; return its fingerprint.

        ``documents`` maps relative ``/``-separated paths to bytes and must
        include ``entry_point``. Existing content is verified, never
        rewritten. When this returns, the bundle is complete on disk.
        """
        _check_bundle(entry_point, documents, normalized, normalization_version)
        documents = {path: bytes(data) for path, data in documents.items()}
        normalized = bytes(normalized)
        fingerprint, manifest = build_manifest(
            entry_point, documents, normalized, normalization_version
        )

        final = self._path(fingerprint)
        if not os.path.isdir(final):
            self._write(final, documents, normalized, manifest)
        self.verify(fingerprint)  # Reuse only content that is intact.

        # The bundle's name must be on disk before any record refers to it,
        # whichever process renamed it into place: a writer that crashed
        # right after its rename may not have synced it.
        try:
            _sync_directory(self.directory)
        except OSError as error:
            raise StoreError(f"Could not save contract content to disk: {error}") from error
        return fingerprint

    def _write(self, final, documents, normalized, manifest):
        """Write a new bundle under a temporary name, sync it, and rename it to ``final``."""
        try:
            _create_directory(self.directory)
        except OSError as error:
            raise StoreError(f"Could not create {self.directory}: {error}") from error

        incoming = os.path.join(self.directory, INCOMING_PREFIX + uuid.uuid4().hex)
        try:
            os.mkdir(incoming, 0o700)
            directories = [incoming, os.path.join(incoming, ORIGINAL_DIRECTORY)]
            os.mkdir(directories[1], 0o700)
            for path, data in documents.items():
                target = os.path.join(incoming, ORIGINAL_DIRECTORY, *path.split("/"))
                parent = os.path.dirname(target)
                if not os.path.isdir(parent):
                    os.makedirs(parent, 0o700)
                    directories.append(parent)
                _write_synced(target, data)
            _write_synced(os.path.join(incoming, NORMALIZED_NAME), normalized)
            # The manifest goes last: only a complete bundle has one.
            _write_synced(os.path.join(incoming, MANIFEST_NAME), _canonical(manifest))
            for directory in reversed(directories):
                _sync_directory(directory)

            try:
                os.rename(incoming, final)
            except OSError:
                if not os.path.isdir(final):
                    raise
                # Another writer stored the same content first; theirs is kept.
            else:
                incoming = None
        except OSError as error:
            raise StoreError(f"Could not store contract content: {error}") from error
        finally:
            if incoming is not None:
                shutil.rmtree(incoming, ignore_errors=True)

    def remove_abandoned(self, older_than=ABANDONED_AFTER_SECONDS, now=None):
        """Delete ``.incoming-*`` directories left by writes that crashed.

        Only directories named exactly as ``put`` names them and last modified
        more than ``older_than`` seconds ago are removed, so a write still in
        progress in another process is never touched. Best effort: anything
        that cannot be removed now is left for a later attempt, and errors
        are never raised. Returns the number of directories removed.
        """
        now = time.time() if now is None else now
        try:
            names = os.listdir(self.directory)
        except OSError:
            return 0  # Missing or unreadable: nothing to clean.
        removed = 0
        for name in names:
            if not _INCOMING_NAME.fullmatch(name):
                continue
            path = os.path.join(self.directory, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue  # Already gone, perhaps renamed into place.
            # lstat, so a symbolic link is never followed out of this directory.
            if not stat.S_ISDIR(info.st_mode) or now - info.st_mtime < older_than:
                continue
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.lexists(path):
                removed += 1
        return removed

    def exists(self, fingerprint):
        """True if a bundle directory exists. Use ``verify`` to check it."""
        return os.path.isdir(self._path(fingerprint))

    def read(self, fingerprint):
        """Return the stored bundle. Raises StoreError if anything is off.

        Each file is read once and checked against the manifest as it is read.
        """
        manifest, documents, normalized = self._load(fingerprint)
        return StoredContract(
            fingerprint=fingerprint,
            entry_point=manifest["entry_point"],
            documents=documents,
            normalized=normalized,
            normalization_version=manifest["normalization_version"],
        )

    def verify(self, fingerprint):
        """Check every file in a bundle against its manifest; return the manifest."""
        return self._load(fingerprint)[0]

    def _load(self, fingerprint):
        base = self._path(fingerprint)
        if not os.path.isdir(base):
            raise StoreError(f"Contract content {fingerprint} is missing.")
        raw = _read(os.path.join(base, MANIFEST_NAME), fingerprint)
        if _sha256(raw) != fingerprint:
            raise StoreError(f"Contract content {fingerprint} has a corrupted manifest.")
        manifest = _parse_manifest(raw, fingerprint)

        documents = {}
        for entry in manifest["documents"]:
            data = _read(_document_path(base, entry["path"]), fingerprint)
            if _sha256(data) != entry["sha256"]:
                raise StoreError(f"Contract content {fingerprint} is corrupted on disk.")
            documents[entry["path"]] = data
        normalized = _read(os.path.join(base, NORMALIZED_NAME), fingerprint)
        if _sha256(normalized) != manifest["normalized"]["sha256"]:
            raise StoreError(f"Contract content {fingerprint} is corrupted on disk.")
        return manifest, documents, normalized

    def _path(self, fingerprint):
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in fingerprint)
        ):
            raise ValueError(f"Invalid contract fingerprint: {fingerprint!r}.")
        return os.path.join(self.directory, fingerprint)


def build_manifest(entry_point, documents, normalized, normalization_version):
    """Return ``(fingerprint, manifest)`` for a bundle without writing it."""
    manifest = {
        "format": MANIFEST_FORMAT,
        "entry_point": entry_point,
        "normalization_version": normalization_version,
        "documents": [
            {"path": path, "sha256": _sha256(data), "size": len(data)}
            for path, data in sorted(documents.items())
        ],
        "normalized": {"sha256": _sha256(normalized), "size": len(normalized)},
    }
    return _sha256(_canonical(manifest)), manifest


def check_document_path(path):
    """Reject a document path that could escape or misbehave in the store.

    Paths are relative, use ``/`` as the separator, and must be valid file
    names on both POSIX and Windows.
    """
    if not isinstance(path, str) or not path or len(path) > _MAX_PATH_LENGTH:
        raise ValueError(f"Invalid document path: {path!r}.")
    if path.startswith("/") or any(
        c in _FORBIDDEN_CHARACTERS or ord(c) < 32 for c in path
    ):
        raise ValueError(f"Invalid document path: {path!r}.")
    for part in path.split("/"):
        # Windows silently drops trailing dots and spaces from names.
        if part in ("", ".", "..") or part != part.rstrip(". "):
            raise ValueError(f"Invalid document path: {path!r}.")
        # Windows treats these names as devices, with or without an extension.
        if part.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"Invalid document path: {path!r}.")


def check_document_set(paths):
    """Reject paths that cannot all exist side by side on every platform."""
    folded = {}
    for path in paths:
        key = path.casefold()
        if key in folded:
            raise ValueError(
                f"Document paths {folded[key]!r} and {path!r} differ only in case."
            )
        folded[key] = path
    for key, path in folded.items():
        parts = key.split("/")
        for depth in range(1, len(parts)):
            prefix = "/".join(parts[:depth])
            if prefix in folded:
                raise ValueError(
                    f"Document {folded[prefix]!r} is also used as a directory by {path!r}."
                )


def _parse_manifest(raw, fingerprint):
    """Decode a manifest and check its shape, so a crafted or damaged manifest
    can never point outside its bundle or crash the reader."""
    def broken(detail):
        return StoreError(f"Contract content {fingerprint} has an invalid manifest: {detail}.")

    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise broken("not JSON") from None
    if not isinstance(manifest, dict):
        raise broken("not an object")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise StoreError(
            f"Contract content {fingerprint} uses unsupported format "
            f"{manifest.get('format')!r}."
        )
    documents = manifest.get("documents")
    normalized = manifest.get("normalized")
    if not isinstance(documents, list) or not documents:
        raise broken("no documents")
    if not isinstance(normalized, dict) or not _is_digest(normalized.get("sha256")):
        raise broken("no normalized digest")
    if not isinstance(manifest.get("normalization_version"), str):
        raise broken("no normalization version")
    paths = []
    for entry in documents:
        if not isinstance(entry, dict) or not _is_digest(entry.get("sha256")):
            raise broken("a document entry is malformed")
        try:
            check_document_path(entry.get("path"))
        except ValueError:
            raise broken(f"unsafe document path {entry.get('path')!r}") from None
        paths.append(entry["path"])
    try:
        check_document_set(paths)
    except ValueError as error:
        raise broken(str(error)) from None
    if len(set(paths)) != len(paths) or manifest.get("entry_point") not in paths:
        raise broken("entry point or document list is inconsistent")
    return manifest


def _is_digest(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _check_bundle(entry_point, documents, normalized, normalization_version):
    if not isinstance(documents, Mapping) or not documents:
        raise ValueError("A contract needs at least one document.")
    for path, data in documents.items():
        check_document_path(path)
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"Document {path!r} must be bytes.")
    check_document_set(documents)
    if entry_point not in documents:
        raise ValueError(f"Entry point {entry_point!r} is not among the documents.")
    if not isinstance(normalized, (bytes, bytearray)):
        raise TypeError("The normalized representation must be bytes.")
    if not isinstance(normalization_version, str) or not normalization_version.strip():
        raise ValueError("normalization_version must be a nonempty string.")


def _document_path(base, path):
    return os.path.join(base, ORIGINAL_DIRECTORY, *path.split("/"))


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read(path, fingerprint):
    try:
        with open(path, "rb") as file:
            return file.read()
    except FileNotFoundError:
        raise StoreError(f"Contract content {fingerprint} is missing a file.") from None
    except OSError as error:
        raise StoreError(f"Could not read contract content {fingerprint}: {error}") from error


def _write_synced(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


# Errors meaning the file system cannot sync a directory at all, rather than
# that a sync failed. Some file systems (for example certain network or FUSE
# ones) report these; any other error is a real I/O failure.
_SYNC_NOT_SUPPORTED = frozenset(
    code
    for code in (getattr(errno, name, None) for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP"))
    if code is not None
)


def _sync_directory(directory):
    """Make a directory's entries durable: names created, renamed or removed in it.

    Skipped on Windows, which cannot open a directory to sync it, and
    tolerated where the file system reports that directory sync is not
    supported. Any other failure is a genuine I/O error and raises OSError.
    """
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in _SYNC_NOT_SUPPORTED:
            raise
    finally:
        os.close(descriptor)


def _create_directory(directory):
    """Create ``directory`` and any missing parents, durably.

    A new directory only survives a power failure once the directory that
    contains it is synced, so each newly created one is synced in its parent.
    """
    missing = []
    current = os.path.abspath(directory)
    while not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    if not missing:
        return
    os.makedirs(directory, mode=0o700, exist_ok=True)
    for path in missing:
        _sync_directory(os.path.dirname(path))