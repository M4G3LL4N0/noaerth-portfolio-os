# Build map: bioyield-labs

Derived from the repository. Not a guessed architecture.

## Canonical entrypoint
`app/page.tsx`

Next prefers ./app over ./src/app.



## Dead or superseded paths
- none detected

These are not deleted. They are marked so the next pass does not treat them as the product.

## Homepage evidence
`app/page.tsx`

## Routes
- `app/about/page.tsx`
- `app/contact/page.tsx`
- `app/dashboard/page.tsx`
- `app/dashboard/plans/[id]/page.tsx`
- `app/demo/page.tsx`
- `app/docs/page.tsx`
- `app/how-it-works/page.tsx`
- `app/intake/page.tsx`
- `app/investor/page.tsx`
- `app/page.tsx`
- `app/planner/page.tsx`
- `app/pricing/page.tsx`
- `app/privacy/page.tsx`
- `app/product/page.tsx`
- `app/support/page.tsx`
- `app/terms/page.tsx`
- `app/trials/page.tsx`
- `app/trust/page.tsx`

## Commands
- `dev`: `next dev`
- `build`: `next build`
- `start`: `next start`
- `lint`: `eslint`

## Direct dependencies
@prisma/client, next, react, react-dom, zod

```mermaid
flowchart LR
  Visitor --> Homepage
  Homepage --> PublicSurface
```
