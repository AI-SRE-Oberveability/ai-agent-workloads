# POC B — service-status investigation agent

State: planned; no implementation. Plan steps P5.1–P5.2.

Flow: synthetic incident -> bounded agent -> allowlisted service-health, deployment-version and recent-failure tools -> evidence-based result. Contracts and synthetic fixtures precede code. Inject latency, timeout, 429/5xx, malformed responses and dependency failure. Demonstrate termination at tool/step/retry/time limits. Optional Node/Java service follows a concrete propagation requirement.
