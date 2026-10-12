"""Turn captured contract documents into a storable bundle.

Discovery identifies each captured document by the URL it came from. A
bundle on disk (see ``contracts.py``) names its files by relative path. This
module decides those paths and keeps the link back to each URL:

* The root definition is stored as ``root.json`` or ``root.yaml``.
* Each referenced document is stored as ``refs/<first 32 hex characters of
  the SHA-256 of its bytes>.json`` (or ``.yaml``).

The extension is chosen from the content itself (JSON starts with ``{`` or
``[``), only so stored files open sensibly in an editor. Paths depend only on
content, never on URLs, HTTP headers, or the order documents were captured
in. So identical content always produces the same bundle and is stored once,
even if a provider moves a file to a new URL. Where each file came from, and
when, is recorded per snapshot as an ``origin`` instead.

Inputs only need ``source_url``, ``content``, ``retrieved_at`` and
``media_type`` attributes, which matches discovery's ``CapturedDocument``.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional

ROOT_NAME = "root"
REFERENCES_DIRECTORY = "refs"
_HASH_CHARACTERS = 32  # 128 bits: collisions are not a practical concern.
_MAX_URL_LENGTH = 8192
_LEADING_NOISE = b"\xef\xbb\xbf \t\r\n"  # UTF-8 byte order mark and whitespace


@dataclass(frozen=True)
class DocumentOrigin:
    """Where one stored file of a snapshot came from."""

    path: str
    source_url: str
    retrieved_at: datetime
    media_type: Optional[str]
    is_root: bool


@dataclass(frozen=True)
class BundlePlan:
    """What to store (``entry_point`` and ``documents``) and where it came from."""

    entry_point: str
    documents: Mapping[str, bytes]
    origins: tuple  # of DocumentOrigin, root first, then references in input order


def plan_bundle(root_document, referenced_documents=()):
    """Decide bundle paths for a root document and the documents it references.

    Raises ValueError or TypeError for unusable input, such as a missing URL,
    non-bytes content, a naive timestamp, or the same URL captured twice.
    """
    root = _read_document(root_document, "root document")
    references = [
        _read_document(document, f"referenced document {index}")
        for index, document in enumerate(referenced_documents)
    ]

    seen_urls = {root[0]}
    for url, *_ in references:
        if url in seen_urls:
            raise ValueError(f"Document URL captured more than once: {url!r}.")
        seen_urls.add(url)

    entry_point = ROOT_NAME + _extension(root[1])
    documents = {entry_point: root[1]}
    origins = [DocumentOrigin(entry_point, root[0], root[2], root[3], True)]

    for url, content, retrieved_at, media_type in references:
        digest = hashlib.sha256(content).hexdigest()[:_HASH_CHARACTERS]
        path = f"{REFERENCES_DIRECTORY}/{digest}{_extension(content)}"
        # Identical bytes at two URLs are stored once; both origins point at them.
        documents.setdefault(path, content)
        origins.append(DocumentOrigin(path, url, retrieved_at, media_type, False))

    return BundlePlan(entry_point, documents, tuple(origins))


def _read_document(document, label):
    url = getattr(document, "source_url", None)
    content = getattr(document, "content", None)
    retrieved_at = getattr(document, "retrieved_at", None)
    media_type = getattr(document, "media_type", None)

    if not isinstance(url, str) or not url.strip() or len(url) > _MAX_URL_LENGTH:
        raise ValueError(f"The {label} needs a source_url.")
    if any(ord(c) < 32 or c == "\x7f" for c in url):
        raise ValueError(f"The {label} has a source_url with control characters.")
    if not isinstance(content, (bytes, bytearray)):
        raise TypeError(f"The {label}'s content must be bytes.")
    if not isinstance(retrieved_at, datetime) or retrieved_at.utcoffset() is None:
        raise ValueError(f"The {label}'s retrieved_at must be a timezone-aware datetime.")
    if media_type is not None and not isinstance(media_type, str):
        raise TypeError(f"The {label}'s media_type must be a string or None.")
    return url, bytes(content), retrieved_at, media_type


def _extension(content):
    """``.json`` if the content looks like JSON, otherwise ``.yaml``. Cosmetic only."""
    return ".json" if content.lstrip(_LEADING_NOISE)[:1] in (b"{", b"[") else ".yaml"
