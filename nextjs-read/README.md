# Next.js 15 read layer for the cattle graph

Drop-in TypeScript for reading the Neo4j cattle graph from a Next.js 15 (App
Router) app. It's **read-only** and completely independent of the crawler —
build and run this whenever you're ready; the data just has to be in Neo4j.

## Files

```
lib/neo4j.ts                     driver singleton (HMR-safe) + read() helper
lib/animals.ts                   typed query functions — the API your app calls
app/api/animals/[uid]/route.ts   example REST route handler
app/animals/[uid]/page.tsx       example Server Component page
.env.example                     connection env vars
```

Copy `lib/` and the example `app/` files into your project (adjust the `@/lib`
import alias to match your `tsconfig.json` paths).

## Setup

```bash
npm install neo4j-driver
# server-only is used to keep DB code off the client bundle:
npm install server-only
cp .env.example .env.local        # then fill in your Neo4j credentials
```

## Use it

In any Server Component, Server Action, or route handler:

```ts
import { getAnimal, carriersOfDefect, animalsByBreed } from "@/lib/animals";

const ace = await getAnimal("USAM20180211Z001");   // full profile, all associations
const carriers = await carriersOfDefect("PHA");     // every PHA carrier
const maine = await animalsByBreed("MA", 50);        // >= 50% Maine-Anjou
```

Available functions (all typed):

- `getAnimal(uid)` — full profile: identity, color, horn, every registration
  (with parsed EPDs + the free-form `attributes` bag), breed composition,
  defect results, sire and dam.
- `listMultiAssociationAnimals(limit?)` — animals registered in >1 association.
- `carriersOfDefect(code, limit?)` — carriers (status `C`) of a defect locus.
- `animalsByBreed(code, minPercent?, limit?)` — composition filter.
- `getPedigree(uid, generations?)` — ancestors up the sire/dam tree.

## Notes

- **Keep DB access server-side.** The `import "server-only"` guard makes the
  build fail if these modules are ever pulled into a Client Component. Never
  prefix the Neo4j env vars with `NEXT_PUBLIC_`.
- **One driver, many sessions.** The driver is cached on `globalThis` so Next's
  hot reload doesn't leak connection pools. Each query opens and closes a short
  READ session.
- **Numbers.** The driver is configured with `disableLosslessIntegers` so counts
  and percentages come back as plain JS numbers.
- **Scaling later.** READ sessions let Neo4j Aura / a cluster route queries to
  read replicas with no code change. If you'd rather the app not embed a driver
  at all, the same functions can sit behind the route handler and the app can
  call `/api/animals/...` over HTTP instead.
```
