# Runbook 04: handle a flag

Each flag carries a gate. C001 destructive SQL: take a verified backup, ship the destructive step separately from the code change, and deploy it in a quiet window. C002 access widened: confirm it is intended. C003 safety setting disabled: re-enable or document.
C004 credentials in the diff: remove them from the change and history, rotate them. C005/P004 pipeline changes: review like production code. P001/P002: run `terraform plan` through a plan review before applying.
A flag is a floor, not a verdict: if the change is intended and reviewed, record why and proceed with the gates.
