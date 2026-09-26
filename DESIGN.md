# DESIGN

The contract for how OXBOW looks and behaves. `apps/web/src/design/tokens.css` is
the source of truth for values; this file is the source of truth for *rules*.
`tokens.ts` is generated from `tokens.css` and never hand-edited — CI fails on
drift, because a token file that can silently disagree with the code that reads
it is a design system with no design system.

This is an investigation tool for a risk analyst working a queue under a budget.
It is not a dashboard template. Every rule below exists because the alternative
has a specific failure: a number nobody can trace, a false positive manufactured
by a modelling choice, or a claim of rigour the interface cannot support.

---

## 1. Tokens

Defined in `design/tokens.css` as Tailwind v4 `@theme`, in **OKLCH** (perceptually
uniform, so a lightness step means the same thing everywhere in the ramp).

| Token | Value | Why |
| --- | --- | --- |
| `--canvas` | `oklch(0.17 0.012 250)` | dark, low-chroma, blue-leaning; the surface evidence sits on |
| `--paper` | `oklch(0.98 0.004 90)` | warm near-white for read-only document surfaces and the print stylesheet |
| `--band-a … --band-e` | ramp | risk bands move in **lightness *and* hue along one arc**, never hue alone |
| `--evidence` | one reserved accent | spent only on provenance: a hash chip, a run id, a citation link |
| hairlines | `oklch(… / 0.08)` | structure must be felt, not drawn |
| radii | `3 / 6 / 0` | two rounded steps and a square one; the square is for data, not decoration |

The band ramp carries rank information, so it must survive greyscale, colour
blindness and a laser-printed packet. Hence rule 6.

## 2. The twelve glyphs

`design/icons/` — hand-drawn, on a 24 px grid, 1.75 px stroke, square caps, 2 px
joins: `cycle · fan-in · fan-out · pass-through · structuring · velocity-spike ·
dormant-wake · fast-cash-out · chain · band-meter · hash-link · embargo`.

These are not downloaded. They encode typologies that no icon library has, and an
approximate icon for *structuring* would quietly teach the wrong thing.
Pipeline: SVG source → SVGO → SVGR → one concatenated `sprite.svg` of `<symbol>`
ids → `<Icon name="cycle" size={16} />`. **Never on the cut list.**

## 3. Banned

Verbatim, non-negotiable, and checked in review as well as by eye:

1. No Inter or Geist.
2. No purple-blue gradients.
3. No glassmorphism.
4. No raw Lucide for domain concepts.
5. No emoji as iconography.
6. No default indeterminate spinner.
7. **No risk encoded by colour alone.**
8. **No "No data available" empty state.**
9. No stock Lottie.
10. No default shadcn radius.

## 4. Motion

Motion exists to **explain causality**, nothing else.

- Enters ease *out*; exits ease *in*, and exits are always faster than enters.
- Position changes use the spring.
- `ease-in-out` never appears on a user-triggered transition — it reads as
  hesitation on an action the user already took.
- Reduced motion is a **tested path, not an assertion**:
  `/dev/states?motion=reduced` renders the whole gallery in that mode.
  `<MotionConfig reducedMotion="user">` globally, shimmer loops paused,
  transforms degraded to opacity.
- The one licensed spinner is the OXBOW mark as a **determinate arc**, reserved for
  user-triggered work with no known output shape — the CP-SAT solve.

## 5. State craft

- **No spinners for content.** Skeleton geometry matches the resolved layout
  *exactly* — same row heights, column widths, and row count as the page size — so
  resolution produces zero layout shift. CLS is asserted at zero on the queue and
  case routes.
- Shimmer is a pseudo-element gradient swept with `transform: translateX()` on the
  compositor. Never animated `background-position`, which repaints every frame.
  1.4 s loop, `will-change: transform`.
- Suspense **per pane**, so the case workspace paints the score header while SHAP
  and the subgraph resolve. Boundaries ordered so the most persuasive content lands
  first.
- `useOptimistic` on the decision write: the hash chip and timeline entry appear
  instantly, reconcile on the server response, roll back with an inline error.
- **Long jobs get a ledger, not a spinner.** Pipeline and backtest stream real
  stage events over SSE with rows and elapsed time. Watching real work happen for
  forty seconds is more convincing than any animation.
- Four empty states, none of which say "no data": filters excluded everything
  (names the *narrowest* predicate, one-click removal, the count that would return)
  · fresh install (the `make pipeline` command with a copy button and expected
  runtime) · no counterparties in the window (explains it is the window's fault,
  offers hop/date widening inline) · zero model disagreement (**framed as a
  finding**, with the threshold that would surface near-misses).
- Four error tiers: inline field · pane-level (that pane retries in place while
  every other pane keeps working) · route-level `error.tsx` · `global-error.tsx`.
  TanStack Query retries 5xx with exponential backoff and **never retries 4xx**.
- **Every error surface prints the `run_id` with a copy button.** A tool whose
  failures cannot be reported is not an investigation tool.
- **Degraded, not broken.** If MLflow, the summarizer or the solver is unavailable,
  the pane renders with a labelled degraded banner and the deterministic fallback —
  the template narrative, the greedy allocation — rather than failing.
- Timestamps show the deployment timezone **with the zone abbreviation**. A UTC
  timestamp shown to a UTC+3 analyst is an hour-hunting bug during a demo.

## 6. Risk legibility

Band letter **and** the five-segment `band-meter` glyph always render alongside any
band colour, and a print stylesheet is tested. A colour-only band fails on greyscale
output, for colour-blind analysts, and in the PDF packet.

## 7. Numbers

The design rule that constrains the whole stack:

- **No route may render a value that is not in an API response.** A number invented
  in JSX is a lie a judge will ask the source of.
- Every currency figure carries its **assumption line and its recovery-rate band**,
  naming the keys in `config/economics.yaml` it depends on. A money figure without
  its assumptions is a rejection trigger, not a nit.
- Every tunable value lives in `config/` or `tokens.css`, never in a component.
- Density is not evidence: a sparse page gets cut, not decorated.

## 8. Standing copy

The disclaimer appears in the README, in the **app footer of every page**, and on
page one of every exported packet — asserted by `test_disclaimer_present_everywhere`.
Any East-African mobile-money framing in the UI is illustrative scenario dressing
over permitted public data, not a claim about any real institution, market or
regulator.
