# Build map: blitzproof

Derived from the repository. Not a guessed architecture.

## Homepage
`app/page.tsx`

## Routes
- `app/about/page.tsx`
- `app/contact/page.tsx`
- `app/dashboard/page.tsx`
- `app/demo/page.tsx`
- `app/docs/page.tsx`
- `app/how-it-works/page.tsx`
- `app/i/[id]/page.tsx`
- `app/i/page.tsx`
- `app/intake/page.tsx`
- `app/investor/page.tsx`
- `app/page.tsx`
- `app/pricing/page.tsx`
- `app/privacy/page.tsx`
- `app/product/page.tsx`
- `app/support/page.tsx`
- `app/terms/page.tsx`
- `app/trust/page.tsx`

## Commands
- `dev`: `next dev --turbopack`
- `build`: `next build`
- `start`: `next start`
- `lint`: `eslint`
- `typecheck`: `tsc --noEmit`

## Direct dependencies
next, react, react-dom

```mermaid
flowchart LR
  Visitor --> Homepage
  Homepage --> PublicSurface
```
