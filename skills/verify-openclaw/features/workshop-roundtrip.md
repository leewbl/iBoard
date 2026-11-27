# Archive and restore a learned skill

Reach the Workshop from a disposable OpenClaw CLI profile. Run `python3 -m workbench.verify_openclaw workshop-roundtrip` with a pinned local image, a matching checkout and a new report path.

The driver seeds one learned skill. The CLI lists it as current, archives it, and lists it in the archive. The driver inspects the archived version through `show`, restores the skill, then lists and reads it again. The restored content must match the original fixture. Each CLI invocation reads the same disposable state and all state is removed after the final read.

The fixture runs with plugins disabled and no running Gateway. Workshop history filtering and the Gateway-owned mutation path require separate journeys.
