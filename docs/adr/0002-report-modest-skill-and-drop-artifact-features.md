# ADR 0002: report modest skill honestly and drop features that are time artifacts

**Decision**: evaluate chronologically and across repos against a lines-changed baseline, publish the numbers (AUC about 0.65 to 0.72), and remove repository age, which improved headline lift (1.82x to 1.65x after removal) but is not a property of the change.
**Why**: a risk score invites over-trust; leaking time into the features inflates results that will not hold on a new repository.
**Consequence**: lower headline numbers, a score that transfers across repositories (leave-one-repo-out), and the README leads with the limits.
