# Build plan: access-layer

## Execution
FIX_NOW

## Why
A bounded change is identified in Now.

## Now
- Do not publish the untracked stub routes that only exist to avoid a 404.

## Acceptance
The change matches code that is actually published, and public copy does not claim a route or metric the repository does not contain.

## Next
- Do not restore the old 147k homepage. It claimed a production platform the pilot does not have.
