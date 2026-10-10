"""Try discovery by hand:  python -m radar.discovery TARGET [options]

    python -m radar.discovery api.stripe.com
    python -m radar.discovery https://api.github.com/user --method GET --api-version 2022-11-28
    python -m radar.discovery api.github.com --json
    python -m radar.discovery https://example.com --llm          # needs MERGE_API_KEY; spends from the ledger
    python -m radar.discovery api.example.com --docs-url https://docs.example.com/api --llm
    python -m radar.discovery api.example.com --deep --trail      # a larger budget, and every navigation step
    python -m radar.discovery.llm_input https://docs.example.com/api   # what the model step would see; free

Exit status: 0 validated, 1 any other outcome, 2 unusable input.
"""

import argparse
import json
import os
import sys

from radar.discovery.input import DiscoveryInputError
from radar.discovery.limits import FetchLimits
from radar.discovery.orchestrator import discover, select_candidate
from radar.discovery.serialization import outcome_to_dict
from radar.domain.discovery import DiscoveryRequest, DiscoveryStatus

MARK = {'match': '+', 'mismatch': 'x', 'indeterminate': '?', 'not_requested': '-', None: ' '}


def _print(outcome, show_trail=False):
    print(f'STATUS: {outcome.status.value.upper()}')
    if outcome.package:
        p = outcome.package
        contract = p.parsed_contract
        print(f'  contract: {contract["info"].get("title")!r}, OpenAPI {p.openapi_version}, '
              f'{len(p.root_document.content):,} bytes, {len(p.referenced_documents)} referenced file(s)')
        print(f'  found at: {p.candidate.source_url}  (via {p.candidate.discovery_method})')
        print('  checks:   + match   x mismatch   ? could not tell   - not requested')
        for e in p.candidate.evidence:
            if e.outcome:
                print(f'    {MARK[e.outcome]} {e.criterion}: {e.description}')
    for package in outcome.packages:
        c = package.candidate
        print(f'  alternative: {package.parsed_contract["info"].get("title")!r} at {c.source_url}  (via {c.discovery_method})')
    rejected = [c for c in outcome.candidates if c.rejection_reasons]
    if rejected:
        print('  rejected:')
        for c in rejected:
            print(f'    {c.source_url}  (via {c.discovery_method})')
            for reason in c.rejection_reasons:
                print(f'      - {reason}')
    if outcome.artifacts:
        print('  found besides a contract:')
        for f in outcome.artifacts:
            where = f'  (via {f.discovery_method}' + (f' from {f.parent_url}' if f.parent_url else '') + ')' if f.discovery_method else ''
            print(f'    [{f.category}] {f.kind}: {f.url}{where}')
            print(f'      {f.detail}')
    print('  attempts:')
    for a in outcome.attempts:
        print(f'    {a.stage:34} {a.outcome:16} {a.url}' + (f'   [{a.reason}]' if a.reason else ''))
    if show_trail and outcome.trail:
        print('  navigation trail (url <- parent, mechanism, outcome):')
        for step in outcome.trail:
            print(f'    {"  " * step.depth}{step.url}  <- {step.parent_url or "start"}  [{step.mechanism}, {step.kind}, {step.outcome}]')
    c = outcome.coverage
    if c is not None:
        state = 'complete within its limits' if c.complete else 'CUT SHORT by ' + ', '.join(c.limits_reached or ['limits'])
        print(f'  coverage: {state}; {c.requests_used}/{c.requests_limit} requests ({c.reserved_for_references} reserved for references), '
              f'{len(c.hosts_contacted)} host(s), {c.leads_unexamined} lead(s) unexamined')
    print('  notes:')
    for note in outcome.limitations:
        print(f'    * {note}')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m radar.discovery', description='Discover and validate a published OpenAPI contract.')
    parser.add_argument('target', help='a domain, an API URL, or a URL of a specification file')
    parser.add_argument('--method', help='HTTP method, to check an operation (with a URL path)')
    parser.add_argument('--api-version', help='provider API version to match')
    parser.add_argument('--product', help='product name to look for in the contract')
    parser.add_argument('--select', metavar='URL', help='if the result is ambiguous, choose the alternative at this URL')
    parser.add_argument('--docs-url', action='append', metavar='URL',
                        help='a documentation page to read (repeatable); default: /docs and /documentation on the target host')
    parser.add_argument('--llm', action='store_true', help='enable the language-model fallback (needs MERGE_API_KEY)')
    parser.add_argument('--ledger', default=os.environ.get('RADAR_DATA_DIR', '.radar') + '/llm_cost.jsonl')
    parser.add_argument('--budget', type=float, default=5.0, help='total USD budget for the language-model ledger')
    parser.add_argument('--max-requests', type=int, default=None)
    parser.add_argument('--max-document-mb', type=float, default=None, help='per-document size limit (default 5)')
    parser.add_argument('--allow-loopback', action='store_true', help='allow localhost targets (for local testing)')
    parser.add_argument('--deep', action='store_true', help='a larger budget for a deliberate deeper search (60 requests, 120 s)')
    parser.add_argument('--trail', action='store_true', help='also show every navigation step with its parent and mechanism')
    parser.add_argument('--json', action='store_true', help='print the machine-readable result instead')
    args = parser.parse_args(argv)

    kwargs = {}
    limits = {}
    if args.deep:
        limits.update(max_requests=60, discovery_timeout=120.0)
    if args.max_requests:
        limits['max_requests'] = args.max_requests
    if args.max_document_mb:
        limits['max_document_bytes'] = int(args.max_document_mb * 1024 * 1024)
        limits['max_total_bytes'] = max(FetchLimits().max_total_bytes, limits['max_document_bytes'] * 2)
    if limits.get('max_requests') and limits.get('max_requests') < 4:
        limits['reference_reserve'] = 0
    try:
        if limits:
            kwargs['limits'] = FetchLimits(**limits)
        if args.llm:
            from radar.discovery.llm_cost import CostLedger, LlmLimits
            from radar.discovery.llm_suggestions import SuggesterError
            from radar.discovery.merge_gateway import MergeSuggester
            try:
                kwargs['llm_suggester'] = MergeSuggester.from_env()
            except SuggesterError as error:
                print(f'error: {error}', file=sys.stderr)
                return 2
            kwargs['llm_ledger'] = CostLedger(args.ledger, LlmLimits(max_total_usd=args.budget))
        outcome = discover(DiscoveryRequest(args.target, method=args.method, api_version=args.api_version,
                                            product=args.product), allow_loopback=args.allow_loopback,
                           documentation_urls=args.docs_url, **kwargs)
        if args.select and outcome.status is DiscoveryStatus.AMBIGUOUS:
            outcome = select_candidate(outcome, args.select)
    except (DiscoveryInputError, ValueError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(outcome_to_dict(outcome), indent=2))
    else:
        _print(outcome, args.trail)
    return 0 if outcome.status is DiscoveryStatus.VALIDATED else 1


if __name__ == '__main__':
    raise SystemExit(main())
