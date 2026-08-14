# code-reviews

External and self-run architecture reviews of this repo, with an accuracy audit and a
remediation plan for each.

Layout, one directory per review:

```
code-reviews/<date>-<topic>/
  source-review.html        the review as received, unedited
  claude-verification.md    claim-by-claim audit against the code
  claude-plan.md            what to actually do, sequenced
```

Files written by an agent carry that agent's name as a prefix, so it stays obvious which
document is the incoming review and which is the response to it.

| Review | Subject | Verdict |
|---|---|---|
| [2026-08-13-architecture](2026-08-13-architecture/) | sidequestor @ `309aab8`, nine architecture candidates | Accurate. 3 live defects confirmed, 1 since fixed locally. |
