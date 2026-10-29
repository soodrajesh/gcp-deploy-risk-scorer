# Threat model

| Threat | Mitigation | Test |
|---|---|---|
| Code or file names reach a model vendor | the model receives structured metadata only; a test asserts that paths, codenames and message text are absent from the payload | `test_the_model_never_receives_code_or_paths` |
| A model argues the risk down | band is the max of the model band and the flags; notes that argue the band or gates down are rejected; the note cannot change the band | `test_a_flag_sets_a_floor_the_model_cannot_lower`, `test_note_validation` |
| The score is gamed through the commit message or by splitting a change | documented limit; content and path flags do not depend on the message; large changes are flagged (P006) | `test_path_flags` |
| A secret in the diff leaks through the service | the diff is parsed in memory and never stored or sent anywhere; credentials in the added lines raise flag C004 | `test_dependency_major_bumps_and_credentials` |
| Over-trust in a modest model | the README, the UI and the API state the measured skill; the score is a rank-based band with gates, not a verdict | evaluation docs |
| Unauthorised use | Cloud Run IAM, one invoker, no `allUsers` | live: 403 |
| Oversized input | 2 MB limit (413) | `test_api_body_limit` |

The service has no access to your repositories or cloud resources; it scores what it is sent.
