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

This module stores bytes. It does not parse, validate or compare contracts;
SQLite records that refer to bundles are written elsewhere, and only after
``put`` has returned.
"""

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from typing import Mapping

from radar.storage.errors import StoreError

MANIFEST_FORMAT = 1
MANIFEST_NAME = "manifest.json"
ORIGINAL_DIRECTORY = "original"
NORMALIZED_NAME = "normalized"
INCOMING_PREFIX = ".incoming-"

# Characters Windows does not allow in file names, plus the separators.
_FORBIDDEN_CHARACTERS = frozenset('<>:"\\|?*')
_MAX_PATH_LENGTH = 512


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
        if os.path.isdir(final):
            self.verify(fingerprint)  # Reuse only content that is intact.
            return fingerprint

        try:
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
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
                _sync_directory(self.directory)
        except OSError as error:
            raise StoreError(f"Could not store contract content: {error}") from error
        finally:
            if incoming is not None:
                shutil.rmtree(incoming, ignore_errors=True)

        self.verify(fingerprint)
        return fingerprint

    def exists(self, fingerprint):
        """True if a bundle directory exists. Use ``verify`` to check it."""
        return os.path.isdir(self._path(fingerprint))

    def read(self, fingerprint):
        """Return the stored bundle. Raises StoreError if anything is off."""
        manifest = self.verify(fingerprint)
        base = self._path(fingerprint)
        documents = {
            entry["path"]: _read(_document_path(base, entry["path"]), fingerprint)
            for entry in manifest["documents"]
        }
        return StoredContract(
            fingerprint=fingerprint,
            entry_point=manifest["entry_point"],
            documents=documents,
            normalized=_read(os.path.join(base, NORMALIZED_NAME), fingerprint),
            normalization_version=manifest["normalization_version"],
        )

    def verify(self, fingerprint):
        """Check every file in a bundle against its manifest; return the manifest."""
        base = self._path(fingerprint)
        if not os.path.isdir(base):
            raise StoreError(f"Contract content {fingerprint} is missing.")
        raw = _read(os.path.join(base, MANIFEST_NAME), fingerprint)
        if _sha256(raw) != fingerprint:
            raise StoreError(f"Contract content {fingerprint} has a corrupted manifest.")
        manifest = json.loads(raw)
        if manifest.get("format") != MANIFEST_FORMAT:
            raise StoreError(
                f"Contract content {fingerprint} uses unsupported format "
                f"{manifest.get('format')!r}."
            )

        files = [
            (_document_path(base, entry["path"]), entry["sha256"])
            for entry in manifest["documents"]
        ]
        files.append((os.path.join(base, NORMALIZED_NAME), manifest["normalized"]["sha256"]))
        for file_path, digest in files:
            if _sha256(_read(file_path, fingerprint)) != digest:
                raise StoreError(f"Contract content {fingerprint} is corrupted on disk.")
        return manifest

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


def _sync_directory(directory):
    """Persist directory entries. Best effort; Windows cannot fsync directories."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass