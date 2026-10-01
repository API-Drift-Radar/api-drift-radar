\# API Drift Radar



API Drift Radar watches live public APIs, infers their schemas, and flags breaking changes that the docs never announced. AI agents then explain each change and draft the fix.



> \*\*Status:\*\* Early development, in the Foundations phase (weeks 1–2).



\## Why this exists



Tools like \[oasdiff](https://github.com/oasdiff/oasdiff) and \[Optic](https://github.com/opticdev/optic) compare two OpenAPI spec files. But many APIs have no spec, or a spec that no longer matches what the server returns. API Drift Radar watches the real responses instead, so it catches changes nobody documented.



\## What it produces



\- \*\*Public changelog and RSS feed\*\* of detected breaking changes

\- \*\*Alerts\*\* by webhook, Slack or GitHub issue

\- \*\*MCP server\*\* so coding agents can check an API's health before writing integration code

\- \*\*CLI and GitHub Action\*\* that fail a build when a dependency's API drifts



\## How it works



1\. \*\*Poll:\*\* Temporal workers fetch each API on a schedule, with retries and rate limits.

2\. \*\*Infer:\*\* A Rust engine turns each response into a schema.

3\. \*\*Diff:\*\* The engine compares the new schema with the previous one.

4\. \*\*Classify:\*\* Deterministic rules decide whether each change is breaking.

5\. \*\*Explain:\*\* An LLM agent summarizes the change and drafts a fix.

6\. \*\*Serve:\*\* Every change is stored in Postgres and served by one REST API.



\## Tech stack



| Layer | Technology |

|---|---|

| Schema inference and diff | Rust, with PyO3 bindings to Python |

| Orchestration | Temporal (Python SDK) |

| AI layer | Python, Claude API, structured outputs |

| API | FastAPI with a generated OpenAPI spec |

| Storage | PostgreSQL (JSONB) |

| Dashboard | Next.js (TypeScript) |

| Deploy | Docker, Terraform, AWS |



\## Repository layout



| Folder | Contents |

|---|---|

| `engine/` | Rust schema inference and diff |

| `worker/` | Temporal workflows and API fetchers |

| `agents/` | Change-explainer and patch-drafting agents |

| `evals/` | Eval datasets, graders and runner |

| `api/` | REST API core |

| `mcp/` | MCP server |

| `cli/` | Command-line tool |

| `skill/` | Agent Skill |

| `web/` | Dashboard and RSS feed |

| `infra/` | Docker and Terraform |

| `docs/` | Design decisions, user research and case studies |



\## Roadmap



\- \[ ] \*\*Weeks 1–2:\*\* Schema inference and diff engine

\- \[ ] \*\*Weeks 3–4:\*\* Poll 10 public APIs, run locally with Docker

\- \[ ] \*\*Weeks 5–6:\*\* AI change explainer, evals, CLI

\- \[ ] \*\*Weeks 7–8:\*\* Patch agent, MCP server, alerts

\- \[ ] \*\*Weeks 9–10:\*\* Public dashboard and launch

