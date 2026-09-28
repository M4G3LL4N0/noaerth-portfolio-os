# Build plan: autoerp

## Execution
FIX_NOW

## Why
portfolio/autoerp/228 adds components/SiteNav.tsx, which the published homepage does not import. Merging that file alone does not change the page. The rest of the app is an uncommitted index and was not committed.

## Now
- Leave portfolio/autoerp/228 unmerged until the published homepage imports SiteNav.

## Acceptance
The change matches code that is actually published, and public copy does not claim a route or metric the repository does not contain.

## Next
- Show a worked planner result only after the merge is safe.
