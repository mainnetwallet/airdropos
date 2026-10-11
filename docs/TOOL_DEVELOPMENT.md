# Tool Development

## Adding a Tool

Subclass `BaseTool`:

```python
from backend.app.tools.base import BaseTool, SideEffect, ToolResult

class MyTool(BaseTool):
    name = "my_tool"
    version = "1.0.0"
    description = "What it does, precisely."
    required_permissions = ["my.permission"]
    side_effect = SideEffect.EXTERNAL_READ
    timeout_seconds = 30
    input_schema = {
        "type": "object",
        "properties": {"target": {"type": "string", "maxLength": 200}},
        "required": ["target"],
    }

    async def execute(self, params):
        return ToolResult.success(data={"result": "..."})
```

Register it in `bootstrap_tools()` in `main.py` and grant its permission in the
`AuthorizationPolicy`.

## Side Effects

`SideEffect` drives the emergency-stop guard. `WALLET_SIGNING` and `TRANSACTION_SUBMISSION`
tools are blocked when the stop is active. `WEB_INTERACTION` is blocked when task execution is
halted.

## Parameter Strictness

`validate_params()` **rejects unknown parameters**. This is deliberate: an unexpected argument
often indicates a prompt injection trying to smuggle instructions into a tool call. Supported
types: string, integer, number, boolean, array, object, plus `enum`, `minimum`, `maximum`, and
`maxLength`.

## Result Envelope

Always return `ToolResult`, never raise. Use `ToolResult.success()`, `.failure(...)`, or
`.needs_user(reason)`.

For actions whose success needs proof, set `evidence={"authoritative": True, ...}`. Without it,
the execution loop records the action as **unverified** — which is correct behaviour, not a bug.

## Dynamic Adapters

When a new platform is discovered, the adapter workflow is: identify official documentation,
determine available APIs, identify authentication, define operations, write an adapter
specification, generate code with the fixed LLM, define schemas, record dependencies and
permissions, and specify required tests.

**Generated adapters stay `QUARANTINED`.** They are not executed and not granted unrestricted
permissions until validation completes. Arbitrary downloaded code is never executed, and
untrusted dependencies are never installed into production.

## Rules

- No unrestricted shell, SQL, filesystem, or raw network access to the LLM.
- Validate every invocation in the backend.
- Keep new tools within granted permissions.
- Never disable a control to make a tool succeed.
