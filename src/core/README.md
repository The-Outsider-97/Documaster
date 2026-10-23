# src/core

The part of the client that has no user interface in it. Everything here can be imported and tested without a browser page
(`tests/*.test.ts`, run with `tsx --test`); `src/client/` builds the screens on top of it and never the other way round.

| File | What it is |
| --- | --- |
| `types.ts` | The shapes the Python service sends and receives (documents, jobs, reports, annotations, agent notes). One place, so the client and the API cannot drift apart silently. |
| `api.ts` | The HTTP client: the `X-Documaster` marker on every state-changing request (the session token travels in the cookie), error bodies turned into `ApiError`, job polling (`?wait=1`), upload with progress. `fetch` is injected, so tests use a mock. |
| `organizer.ts` | The page organiser engine behind the Merger: page items per source, select, move (also by keyboard), exclude, rotate, duplicate, blank pages, and the plan sent to the service. |
| `geometry.ts` | Normalised page rectangles ([left, top, right, bottom], top-left origin): conversion between pointer positions and page coordinates, hit tests, simplifying freehand strokes. |
| `format.ts` | Pure formatting and parsing: bytes, durations, plural forms, page-range strings (`1-3,5`) in both directions. |

Rule: nothing in this folder touches the DOM tree, `localStorage` or a global `window`.
