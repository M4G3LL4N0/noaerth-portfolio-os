# access-layer

## What is this?
AccessXWorld is a coordination layer for physical access: policy, a signed pass, an edge check, and an audit trail.

## Who is it for?
Operators of venues, parking, property, campuses, and infrastructure who need access without shared gate codes.

## Problem
Physical access still depends on copied codes and improvised staff decisions.

## What exists
The published homepage is app/page.tsx. It shows four steps and an illustration of what a pass would carry. The page does not sign or check a credential. src/app is excluded by tsconfig and is not the served tree.

## Distinctive
The product story is the signed pass, not a generic access-control brochure.

## Maturity
Early Access

## Primary workflow
Define a policy, issue a signed pass, check it at the edge, keep the proof.

## Incomplete
Many secondary routes exist, including customers and metrics pages, that can overclaim a finished platform.

## Opportunity
Audit secondary pages so they match the pilot-brief honesty of the homepage.

Served homepage file: `app/page.tsx`.
Heading: Signed access for real doors, gates, and lots..
