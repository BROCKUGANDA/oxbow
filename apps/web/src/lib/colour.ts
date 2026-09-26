/* =============================================================================
   Canvas colour adapter.

   WHY THIS FILE EXISTS. `design/tokens.css` is the source of truth, and it is
   OKLCH by contract — DESIGN.md §1, because the band ramp has to move in
   perceptual lightness and survive greyscale. The generated mirror,
   `design/tokens.ts`, exists because Cytoscape, canvas2d and lightweight-charts
   cannot read CSS custom properties. That mirror carries the OKLCH strings
   verbatim, and it turns out that is not enough: **Cytoscape's own colour parser
   rejects `oklch()` outright.** Every band fill, every community border and every
   typologed edge stroke on the graph was silently dropped, verified in a browser
   as ~1,000 repeated console warnings of the form

       The style property `background-color: oklch(0.78 0.155 40)` is invalid

   and a graph drawn in Cytoscape's default light blue. No typecheck sees that: the
   string is a perfectly well-typed `string`, and the failure is a parser refusing
   it at paint time. The consequence is not cosmetic — risk legibility on the
   network screen was carrying no band information at all, which is exactly the
   thing DESIGN.md §6 rule 7 forbids.

   WHAT THIS FILE DOES. It converts an OKLCH colour to sRGB at the boundary where a
   renderer needs to parse it, so the tokens stay OKLCH everywhere else. It is a
   colour-space conversion, not a palette: the same numbers, expressed in the
   grammar the consumer accepts. Nothing here invents a hue.

   The math is the standard Oklab→linear-sRGB→sRGB pipeline (Ottosson). It is
   written out rather than pulled from a package because 01 A rule 6 forbids
   unapproved dependencies for ~40 lines of arithmetic, and because the gamut
   clamp has to be explicit: an out-of-gamut OKLCH colour is a real possibility in a
   ramp this wide, and the alternative is a NaN that paints nothing and says nothing.
   ============================================================================= */

/** One parsed OKLCH colour. `alpha` is a 0–1 fraction, absent meaning opaque. */
type Oklch = { l: number; c: number; h: number; alpha: number };

const OKLCH_PATTERN = /^oklch\(\s*([0-9.]+)(?:%?)\s+([0-9.]+)(?:%?)\s+([0-9.]+)(?:deg)?\s*(?:\/\s*([0-9.]+)%?)?\s*\)$/i;

function parse(value: string): Oklch | null {
  const match = OKLCH_PATTERN.exec(value.trim());
  if (match === null) return null;
  const l = Number(match[1]);
  const c = Number(match[2]);
  const h = Number(match[3]);
  const rawAlpha = match[4];
  if (!Number.isFinite(l) || !Number.isFinite(c) || !Number.isFinite(h)) return null;
  return { l, c, h, alpha: rawAlpha === undefined ? 1 : Number(rawAlpha) };
}

/** Oklab to linear sRGB. The matrix is the published Oklab→LMS→RGB composition. */
function oklabToLinearRgb(l: number, c: number, h: number): [number, number, number] {
  const a = c * Math.cos((h * Math.PI) / 180);
  const b = c * Math.sin((h * Math.PI) / 180);

  const lPrime = l + 0.3963377774 * a + 0.2158037573 * b;
  const mPrime = l - 0.1055613458 * a - 0.0638541728 * b;
  const sPrime = l - 0.0894841775 * a - 1.291485548 * b;

  const lc = lPrime ** 3;
  const mc = mPrime ** 3;
  const sc = sPrime ** 3;

  return [
    4.0767416621 * lc - 3.3077115913 * mc + 0.2309699292 * sc,
    -1.2684380046 * lc + 2.6097574011 * mc - 0.3413193965 * sc,
    -0.0041960863 * lc - 0.7034186147 * mc + 1.707614701 * sc,
  ];
}

/** Linear → gamma-encoded sRGB, with the transfer function applied per channel. */
function encode(channel: number): number {
  const clamped = channel <= 0.0031308 ? 12.92 * channel : 1.055 * channel ** (1 / 2.4) - 0.055;
  return Math.round(Math.min(1, Math.max(0, clamped)) * 255);
}

function hex(byte: number): string {
  return byte.toString(16).padStart(2, '0');
}

/**
 * Convert one CSS colour to the form canvas renderers accept.
 *
 * Anything this file does not recognise is returned unchanged: `rgb()`, hex and the
 * named colours Cytoscape already parses must not be mangled on the way through, and
 * a passthrough is strictly better than a guess. `null` and `undefined` become
 * Cytoscape's own "no colour" sentinel rather than an empty string, which parses as
 * black and would silently recolour the graph.
 */
export function canvasColour(value: string | null | undefined): string {
  if (value === null || value === undefined) return 'transparent';
  const parsed = parse(value);
  if (parsed === null) return value;

  const [red, green, blue] = oklabToLinearRgb(parsed.l, parsed.c, parsed.h);
  const r = encode(red);
  const g = encode(green);
  const b = encode(blue);
  const alpha = Number.isFinite(parsed.alpha) ? Math.min(1, Math.max(0, parsed.alpha)) : 1;

  if (alpha >= 1) return `#${hex(r)}${hex(g)}${hex(b)}`;
  return `rgba(${String(r)}, ${String(g)}, ${String(b)}, ${alpha.toFixed(3)})`;
}

/**
 * Resolve a CSS custom property to a literal, for a renderer that cannot take
 * `var()`. Reading the computed value rather than hard-coding a stack keeps the token
 * file as the single source: the canvas font is the same face the DOM is using, and
 * it follows a paper/canvas theme change without a second definition.
 */
export function resolveFontStack(host: HTMLElement, cssVariable: string): string {
  const fromToken = getComputedStyle(host).getPropertyValue(cssVariable).trim();
  return fromToken.length > 0 ? fromToken : 'monospace';
}
