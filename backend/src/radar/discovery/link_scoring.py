"""How promising a navigation link looks, from keywords in its URL path and label. Explicit rules, not learned.

Shared by navigation (which links to follow) and the language-model page reduction (which links to show).
A score is a reason to look, never evidence that a link leads to, or belongs to, a particular API.
"""

import re
from urllib.parse import urlsplit

ASSET = re.compile(r'(?i)\.(?:css|js|mjs|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|map|zip|gz|tgz|mp4|pdf)$')
KEYWORD_SCORES = (
    (re.compile(r'(?i)openapi|swagger|redoc|rapidoc'), 10), (re.compile(r'(?i)\bspec(?:s|ification)?\b|api[-_ ]?spec'), 8),
    (re.compile(r'(?i)api[-_ ]?(?:reference|docs?|documentation)|reference'), 6), (re.compile(r'(?i)\bapis?\b'), 5),
    (re.compile(r'(?i)developers?|dev[-_ ]portal'), 5), (re.compile(r'(?i)docs?\b|documentation'), 4),
    (re.compile(r'(?i)download|definition|schema'), 2),
    (re.compile(r'(?i)guide|integrat|getting[- ]started|resources?|sdks?|portal|machine[- ]readable'), 1),
)
NEGATIVE = re.compile(r'(?i)pricing|login|log-?in|sign-?(?:in|up)|careers?|jobs|blog|press|privacy|terms|cookie|status\.|support/|contact')
# Navigation follows links scoring at least MIN_SCORE; a model is shown links scoring at least MODEL_MIN_SCORE,
# so it can choose among promising links the rules would not follow on their own.
MIN_SCORE, MIN_CROSS_ORIGIN_SCORE, MODEL_MIN_SCORE = 4, 6, 1


def score_link(url: str, label: str = '') -> int:
    try:
        path = urlsplit(url).path
    except ValueError:
        return 0
    if ASSET.search(path):
        return 0
    text = f'{path} {label}'
    if NEGATIVE.search(text):
        return 0
    return min(12, sum(weight for pattern, weight in KEYWORD_SCORES if pattern.search(text)))
