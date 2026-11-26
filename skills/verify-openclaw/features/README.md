| Journey | User-facing feature | Evidence |
| --- | --- | --- |
| [gateway-health](gateway-health.md) | Check whether a fresh Gateway can answer a real health RPC | Readiness and liveness HTTP responses, CLI health JSON, child exit, Docker cleanup |
| [skills-check](skills-check.md) | Discover workspace skills and see which prerequisites are missing | CLI JSON with and without an environment prerequisite, unchanged fixture files, Docker cleanup |
| [skills-inspect](skills-inspect.md) | List local skills and view the details of a named skill | CLI JSON for list, detail and an absent name, unchanged fixture files, Docker cleanup |
| [config-validate](config-validate.md) | Check the validity of a local configuration | Exit codes and JSON for valid and invalid port types, unchanged fixture inputs, Docker cleanup |
