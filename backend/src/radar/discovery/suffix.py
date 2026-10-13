"""Public-suffix handling: which part of a host an organisation can own.

`co.uk` and `github.io` are shared by unrelated parties, so a host that merely ends in one says nothing about who
runs it. The list (including its private section, which covers hosting domains such as `github.io`) comes from the
`publicsuffixlist` package, which bundles it and needs no network access.
"""

from functools import lru_cache

from publicsuffixlist import PublicSuffixList

_LIST = PublicSuffixList()
_ICANN = PublicSuffixList(only_icann=True)


@lru_cache(maxsize=4096)
def registrable_domain(host: str) -> str | None:
    """The domain an organisation can register under a public suffix, or None if `host` is itself a suffix.

    >>> registrable_domain('api.acme.co.uk')
    'acme.co.uk'
    >>> registrable_domain('co.uk') is None and registrable_domain('github.io') is None
    True
    >>> registrable_domain('someone.github.io')
    'someone.github.io'
    """
    return _LIST.privatesuffix((host or '').rstrip('.').lower()) or None


def is_public_suffix(host: str) -> bool:
    return registrable_domain(host) is None


def is_icann_suffix(host: str) -> bool:
    """True for `com` or `co.uk`: suffixes run by registries, as opposed to private ones such as `github.io`.

    >>> is_icann_suffix('co.uk'), is_icann_suffix('github.io'), is_icann_suffix('acme.co.uk')
    (True, False, False)
    """
    return _ICANN.privatesuffix((host or '').rstrip('.').lower()) is None
