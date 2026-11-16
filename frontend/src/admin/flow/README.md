# Flow sync

The host screens must match the Flow Lab flow
(https://flow-lab-chi.vercel.app/moafunk/). This folder checks that in the
tests and tells every environment what its build matches.

## Files

| File | What it is |
|---|---|
| `flow.contract.json` | Pinned copy of the flow: per screen the items it shows, plus a hash of the items and one of the notes. Made by Flow Lab. Do not edit by hand. |
| `flow-gaps.json` | Known differences. `leftOut`: not built yet, with the release (a whole screen, or some `items`). `allowed`: extra items that a flow note describes. `drift`: known drift with a reason. It stays drift in the report. |
| `sync/<screen>.json` | Result of each screen's check. Written by the tests, committed. |
| `check.ts`, `expectFlow.ts` | The check and the test helper. |
| `report.ts` | Builds `/flow-sync.json` from the files above. |

The build writes `dist/admin/flow-sync.json`. The backend serves it at
`/flow-sync.json`, for example https://staging.live.moafunk.de/flow-sync.json.

## What is checked

Headings, pills, notices, buttons, links, form labels, checkboxes and rows,
by their text. Plain text, images and the flow notes (`>` lines) are not
checked. Behaviour from the notes needs its own test.

A test fails when:

- a flow item is missing and `flow-gaps.json` does not list it as left out;
- the page shows a heading, pill, notice, button, link, form label or row that
  the flow does not have;
- a flow screen has no check and is not left out as a whole;
- an entry in `flow-gaps.json` no longer applies.

The mock data in the checks uses the flow's own example data (Brunchtime,
lena, So 25.10.), so texts can match exactly.

## Pin a new flow rev

1. In Flow Lab:
   `npm run contract -- moafunk --out <this repo>/frontend/src/admin/flow/flow.contract.json`
2. Here: `npx vitest run -u`. This rewrites `sync/*.json`.
3. Read the diff of `sync/*.json`. Fix the code, or add the difference to
   `flow-gaps.json` with a release or a reason.
4. Run `npx vitest run` again. It must pass without `-u`.

## Check a new screen

Add a case to the screen's page test:

```ts
await expectFlow('screen-id', root);
```

For one more state of the same screen, for example a dropped connection:

```ts
await expectFlow('studio-onair', root, 'reconnecting');
```

This writes `sync/studio-onair@reconnecting.json`. Its items show in the
report with `[reconnecting]` in front.

## See what each environment runs

In Flow Lab: `npm run sync-status -- moafunk`. Per screen it shows `ok`,
`drift: n`, `left out: R3`, `changed` (built from an older flow), `new` (not
in that build) or `notes changed`.
