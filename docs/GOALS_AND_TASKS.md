# Goals & Tasks

## Goals

A goal is a persistent long-term objective, for example:

> "Continuously discover legitimate airdrops, research eligibility, complete permitted
> activities, monitor claims, and report important changes."

Each goal stores: ID, description, status, progress, success criteria, scheduling policy,
resource limits, current blockers, next planned action, and last review timestamp. Goals can be
paused, resumed, and cancelled.

The agent checks for existing goals and near-duplicate tasks before creating new work, so it does
not spawn redundant tasks for the same objective.

## Tasks

Tasks are the executable unit. Every task gets a persistent `AIR-YYYY-MM-DD-NNNNNN` ID and stores:

- Goal, project, and parent task references
- Category, network, priority, status
- Dependencies on other tasks
- Assigned wallet references
- Planned, completed, and failed actions
- Execution history and retry count
- Timestamps: created, updated, next execution, completed, last verification
- Evidence, transaction references, cost estimates
- Checkpoints and next planned action

Lookups work by task ID, project, wallet, network, status, and date.

## Dependency Handling

A task is only claimable when its dependencies are satisfied. The planner stores dependencies
explicitly so partial completion is visible rather than implied.

## Progress

Progress is a float from 0.0 to 1.0 and is only reported as complete when completion conditions
are verified against evidence. Uncertainty is represented as `UNVERIFIED`.

## Duplicate Prevention

`TaskManager.find_duplicates()` matches on title fragment within a goal, excluding cancelled and
completed tasks. The agent consults it before creating work so the same objective does not
generate parallel task trees.
