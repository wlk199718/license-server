# Project Team And Refresh Plan

## Scope
- Add an optional `team` field to projects.
- Expose the field through create, update, and list project APIs.
- Add the field to the create-project form and project cards.
- Refresh the current data view immediately after successful mutations.
- Bump the application/static asset version.

## Data Safety
- Existing tables are never dropped or recreated.
- Startup migration only adds the nullable/defaulted `projects.team` column when missing.
- Existing projects receive an empty team value; licenses and device bindings are untouched.
- No NAS deployment or remote database operation is part of this change.

## Rollback
- Revert API and UI references to `team`.
- The added database column may remain safely unused; no destructive rollback is required.

## Acceptance
- Existing SQLite data counts and rows survive startup migration unchanged.
- New projects show their team immediately without a manual browser refresh.
- License/project mutations redraw the active list after the API succeeds.
- Backend tests and browser UI flow checks pass locally.
