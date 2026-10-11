# Self-Improvement

## Outcome-Based Learning

`LearningEngine` analyzes task outcomes, repeated errors, and tool performance to propose
improvements. Insights carry a sample size and confidence; **one successful execution is never
treated as proof a workflow is universally correct**.

Learning records live in their own tables, separate from authoritative task records.

**Security controls are never disabled to improve a success rate.** Recommendations containing
"disable", "bypass", "lower the limit", or "skip verification" are rejected by policy and
recorded with confidence 0.

## Controlled Self-Coding

The same fixed LLM powers code generation. There is no second coding model.

`SelfImprovementEngine.propose()`:

1. Identifies a concrete problem and analyses relevant logs.
2. Generates a proposal with root cause, change, affected files, required tests, and rollback plan.
3. Records it as `PROPOSED`.

## Deployment Gating

```
PROPOSED → BRANCH_CREATED → VALIDATION_PENDING → VALIDATED → DEPLOYED
                                                      ↓
                                                 ROLLED_BACK
```

`mark_deployed()` raises unless the proposal is in `VALIDATED` state. Unattended deployment of
unvalidated self-generated code is structurally prohibited.

## Protected Files

These paths can never be modified by a proposal, regardless of what the model returns:

```
app/security/
app/wallets/vault.py
app/llm/client.py
app/tools/registry.py
app/core/config.py
```

A proposal touching them is recorded but flagged for mandatory manual security review, and can
never be auto-deployed.

## During This Phase

Validation commands are **recorded but not run**. The workflow definitions exist; the pipeline
does not execute during source creation.
