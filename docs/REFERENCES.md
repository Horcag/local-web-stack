# Workflow reference evidence

Public GitHub API inspection on 2026-10-06; links pin the exact inspected revision.
These are design references, not claims that this project inherited their acceptance.

| Repository | Revision and inspected surface | Applied observation |
| --- | --- | --- |
| agent-machine-control | [9dc63deb](https://github.com/Horcag/agent-machine-control/blob/9dc63deba0af753c361b0155fc8ead632a8487ae/.github/workflows/ci.yml) | Shared size/coverage commands, immutable actions, concurrency and least permissions. |
| agent-control-plane | [615b854a](https://github.com/Horcag/agent-control-plane/blob/615b854ae9c8e2489c0708b8572aaa15dbd4bfa2/pyproject.toml) | Ruff, mypy and pytest configured beside Python dependencies. |
| sto-report-generator | [16dec352](https://github.com/Horcag/sto-report-generator/blob/16dec35203d3afe92cd08c2020378a7579f51764/.github/workflows/ci.yml) | Shared quality commands; operational acceptance is a separate workflow. |
| english-file | [fce6f5d3](https://github.com/Horcag/english-file/tree/fce6f5d3c436d05b086a1c302fe3404768e47075) | No workflow/tool configuration found in bounded tree inspection; no gate pattern borrowed. |
| Crawl4AI | [8afd0a68](https://github.com/unclecode/crawl4ai/blob/8afd0a68064ff7049303c9f9d037ab6228aac43c/.github/workflows/docker-release.yml) | Container release is a distinct publication step. |
| Firecrawl | [8c84d8b6](https://github.com/firecrawl/firecrawl/blob/8c84d8b6a155495b8088c286d878b2f875de3918/.github/workflows/deploy-image-staging.yml) | Explicit image permissions and revision-derived tags. |
| browser-use | [7be96ed8](https://github.com/browser-use/browser-use/tree/7be96ed8bafa8dfe1eef228b59cf5c884b8b2431/.github/workflows) | Separate browser installation/operational workflows from local code tooling. |

[uv project synchronization documentation](https://docs.astral.sh/uv/concepts/projects/sync/)
was checked through Context7: `uv lock --check` validates freshness, `--frozen` skips
that check, and `--no-dev` excludes the development group. Action tags were resolved
to official repository commit SHAs through GitHub API on the same date. Dependabot
maintains those immutable workflow pins and Python/Docker dependency updates.
