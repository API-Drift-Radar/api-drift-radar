"""A labeled benchmark of real targets: how often discovery finds the right contract, and how often it is wrong.

    python -m radar.discovery.benchmark                      # live network; no language model, no cost
    python -m radar.discovery.benchmark --no-directory       # to see what the directory adds
    python -m radar.discovery.benchmark --only stripe --json

Each target carries an expectation:
  found     a usable OpenAPI 3.x description is published; discovery should return one whose source URL contains one of
            `accept`. An ambiguous outcome counts if one alternative does (several legitimate contracts exist).
  optional  a description may exist only in a form the validator does not take; no result is fine, a wrong one is not.
  none      no public contract exists; any returned package is a false positive.
`reject` lists source URLs that are wrong for the target even though they validate (a partial or unrelated contract).

Recall is measured on `found` targets. Precision is measured on every returned package. Both are reported with
the labels' limits: they are the maintainer's reading of each provider's published material, not an audit, and the
live providers change.
"""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys

from radar.discovery.orchestrator import discover
from radar.domain.discovery import DiscoveryRequest, DiscoveryStatus

DEFAULT_TARGETS = Path(__file__).resolve().parents[3] / 'benchmark' / 'targets.json'
PACKAGE_STATUSES = (DiscoveryStatus.VALIDATED, DiscoveryStatus.UNCONFIRMED)


@dataclass(frozen=True)
class Verdict:
    target: str
    expect: str
    status: str
    sources: tuple[str, ...]
    outcome: str  # hit, miss, correct_empty, false_positive, ambiguous_hit, wrong_alternative

    @property
    def returned_package(self):
        return bool(self.sources)


def sources_of(outcome) -> tuple[str, ...]:
    packages = ([outcome.package] if outcome.package else []) + list(outcome.packages)
    return tuple(p.candidate.source_url for p in packages)


def judge(entry: dict, status: str, sources: tuple[str, ...]) -> str:
    """Pure: compare what discovery returned with what the label expects."""
    accept, reject = entry.get('accept', []), entry.get('reject', [])
    expect = entry['expect']
    good = [s for s in sources if any(a in s for a in accept) and not any(r in s for r in reject)]
    bad = [s for s in sources if s not in good]
    if expect == 'none':
        return 'false_positive' if sources else 'correct_empty'
    if not sources:
        return 'miss' if expect == 'found' else 'correct_empty'
    if bad and not good:
        return 'false_positive'
    if bad:
        return 'wrong_alternative'  # a correct contract is among the alternatives, next to a wrong one
    return 'hit' if status != DiscoveryStatus.AMBIGUOUS.value else 'ambiguous_hit'


def summarize(verdicts) -> dict:
    found = [v for v in verdicts if v.expect == 'found']
    hits = [v for v in found if v.outcome in ('hit', 'ambiguous_hit')]
    returned = [v for v in verdicts if v.returned_package]
    correct = [v for v in returned if v.outcome in ('hit', 'ambiguous_hit')]
    wrong = [v for v in verdicts if v.outcome in ('false_positive', 'wrong_alternative')]
    return {
        'targets': len(verdicts), 'found_targets': len(found), 'hits': len(hits),
        'recall': round(len(hits) / len(found), 3) if found else None,
        'packages_returned': len(returned), 'packages_correct': len(correct),
        'precision': round(len(correct) / len(returned), 3) if returned else None,
        'false_positives': [v.target for v in wrong],
        'misses': [v.target for v in found if v.outcome == 'miss'],
        'unconfirmed_hits': sum(1 for v in hits if v.status == DiscoveryStatus.UNCONFIRMED.value),
    }


def run(entries, *, use_directory=True, only=None, discover_fn=discover):
    verdicts = []
    for entry in entries:
        if only and only not in entry['target']:
            continue
        try:
            outcome = discover_fn(DiscoveryRequest(entry['target']), use_directory=use_directory)
            status, sources = outcome.status.value, sources_of(outcome)
        except Exception as error:  # a crash is a result, not a reason to stop the run
            status, sources = f'error: {type(error).__name__}', ()
        verdicts.append(Verdict(entry['target'], entry['expect'], status, sources, judge(entry, status, sources)))
    return verdicts


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the labeled discovery benchmark against the live network.')
    parser.add_argument('--targets', type=Path, default=DEFAULT_TARGETS)
    parser.add_argument('--no-directory', action='store_true')
    parser.add_argument('--only', help='run targets whose URL contains this text')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    document = json.loads(args.targets.read_text())
    verdicts = run(document['targets'], use_directory=not args.no_directory, only=args.only)
    summary = summarize(verdicts)
    if args.json:
        print(json.dumps({'summary': summary, 'results': [v.__dict__ for v in verdicts]}, indent=1))
    else:
        for v in verdicts:
            print(f'{v.outcome:15} {v.status:12} {v.expect:9} {v.target}')
            for source in v.sources:
                print(f'{"":39}-> {source}')
        print()
        for key, value in summary.items():
            print(f'{key}: {value}')
        print(f'\nlabels: {document.get("labels_by")}')
    return 0 if not summary['false_positives'] else 1


if __name__ == '__main__':
    sys.exit(main())
