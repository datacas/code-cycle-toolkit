# Issue-review fixtures

Conceptual fixtures for the optional pre-implementation issue review. Each one
pairs the declared task signals of a real toolkit work item with the result an
issue review is expected to return for it. They are written for the tests, not
captured from a live dispatch: they pin the routing decision, the closed result
shape, and the separation from the implementer's diagnosis.

| File | Work item | Case |
|---|---|---|
| `issue-92-trivial.json` | #92, a documentation convention with explicit scope | declared trivial and not security-sensitive: `auto` skips the review |
| `issue-90-moderate.json` | #90, documenting optional host tools (built on #82's diagnosis) | ordinary work: the standard reviewer confirms `READY` without repeating diagnosis |
| `issue-91-high-risk.json` | #91, verifying that read-only stages leave the checkout unchanged | high-risk and security-sensitive: the senior reviewer asks for refinement because the proposed detector does not cover the whole invariant |
