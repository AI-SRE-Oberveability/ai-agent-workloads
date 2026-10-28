# POC A — synthetic policy assistant

State: local draft; plan approved and publication review in progress. Current implementation is in `../../src/ai_lab`, with contracts in `../../contracts` and expected answers in `../../evaluations`.

Flow: authenticated API -> bounded deterministic plan -> HTTP retrieval tool -> extractive answer with invented policy citations. Observe tool/model durations, steps, retries, status and cross-service trace context. Mock token usage remains unknown.

Exit evidence: supported/unsupported/empty answers; timeout/rate-limit/server-error/malformed tool responses; bounded execution; versioned expected-answer checks; exact backend trace graph and exported-content canary checks. The earlier in-memory telemetry run does not complete backend validation.
