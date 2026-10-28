# POC C — read-only SRE copilot

State: planned; no implementation. Plan steps P5.3–P5.4.

Flow: synthetic incident -> profile-specific monitoring adapter -> bounded evidence -> timeline/likely causes with source IDs and uncertainty -> human review. Use verified supported Managed APIs; isolate any optional SaaS DQL/Grail configuration. Treat logs/documents as untrusted data. No shell, unrestricted query, Azure/Kubernetes write or automatic remediation tool belongs to this POC.
