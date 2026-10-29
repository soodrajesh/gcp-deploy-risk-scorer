# ADR 0001: a statistical model for ordinary risk, deterministic flags for catastrophic risk

**Decision**: the band is the maximum of a model band (rank among historical changes) and the highest deterministic flag.
**Why**: history predicts which ordinary changes need follow-up, but a two-line `DROP TABLE` is rare in any training set and looks harmless by size; conversely hand-written rules cannot rank thousands of ordinary changes.
**Consequence**: a model can never lower a flag's floor, and the screenshots show both layers disagreeing on purpose.
