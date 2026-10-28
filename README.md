# AI agent workloads

Verified public GitHub repository: `AI-SRE-Oberveability/ai-agent-workloads`. Reviewed source is published under the organization; local GitHub remotes are configured. The delivery plan is approved; this first publication remains a development baseline.

This repository owns the application code, contracts, images, tests and application deployments for three distinct POCs:

| POC | Current location | State |
| --- | --- | --- |
| Policy assistant | `src/ai_lab`, `contracts`, `evaluations`, `tests`; scenario overview in `pocs/policy-assistant` | Draft with historical local test evidence |
| Service-status investigator | `pocs/service-status` | Planned; no implementation |
| Read-only SRE copilot | `pocs/sre-copilot` | Planned; no implementation |

The first policy slice uses one inspectable Python package. After plan acceptance, separate additional POC modules/services according to their contracts; extract shared model/telemetry/auth code only where it is actually reused. Keep POC-specific datasets, failure scenarios and assertions distinct. Do not present placeholders as implemented agents.

Repository layout:

```text
contracts/                  Versioned API contracts
src/ai_lab/                 Current draft policy agent, retrieval and mock adapter
tests/                      Contract, HTTP, bounds and telemetry checks
evaluations/                Versioned synthetic expected answers
pocs/
  policy-assistant/         POC A scenario and acceptance notes
  service-status/           POC B planned scenario and acceptance notes
  sre-copilot/              POC C planned scenario and acceptance notes
.github/workflows/          CI tests and container build
.github/workflow-drafts/    Historical pre-activation draft
Dockerfile                  Draft application image
compose.yaml                Draft application services
pyproject.toml / uv.lock    Dependency declarations and resolved versions
requirements.lock.txt      Hash-pinned application dependencies
```

The active CI workflow installs requirements-dev.lock.txt with hash verification, checks lint/format, runs HTTP/contract/telemetry tests and builds the synthetic container. Its historical draft is retained separately. GitHub run evidence is recorded in the lab; the complete P2.3 end-to-end/release exit remains separate. Run/lifecycle instructions will be completed at P1.5; no backend delivery is implied by these files.

Azure resources belong to infra; Collector/OneAgent/backend/dashboard configuration belongs to observability-config; coordination, HLD and evidence belong to lab. Never commit credentials, virtual environments, downloaded binaries or raw customer data.
