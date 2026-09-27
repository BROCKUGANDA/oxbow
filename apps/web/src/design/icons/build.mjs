#!/usr/bin/env node
/* =============================================================================
   OXBOW glyph + token build.
   Spec 12.6 PIPELINE: draw -> export SVG -> SVGO -> SVGR -> concatenate into
   sprite.svg as <symbol> ids. Sixty glyphs cost one request and zero per-icon
   JavaScript, and because every glyph uses currentColor, band colour drives glyph
   colour for free.

   This script is the tail of that pipeline. It is deterministic, idempotent and
   offline: same inputs, byte-identical outputs, no network, no dependencies.

   Run:
     node apps/web/src/design/icons/build.mjs

   Emits:
     sprite.svg   <symbol id="i-NAME"> per glyph, referenced as /sprite.svg#i-NAME
     ../tokens.ts typed TS mirror of tokens.css for Cytoscape, canvas and
                  lightweight-charts, none of which can read CSS custom properties

   tokens.ts is GENERATED and committed. Edit tokens.css, re-run, never hand-edit.
   ============================================================================= */

import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC_DIR = join(HERE, 'src');
const SPRITE_OUT = join(HERE, 'sprite.svg');
const TOKENS_CSS = join(HERE, '..', 'tokens.css');
const TOKENS_TS = join(HERE, '..', 'tokens.ts');

/* Next.js serves static assets only from apps/web/public/. Icon.tsx references
   <use href="/sprite.svg">, so the sprite has to exist at the web public root or
   every glyph renders blank. Emitting it next to the sources alone is not enough:
   the served copy is the one the browser actually fetches. Both are written from
   the same generated string, so they cannot drift. */
const WEB_ROOT = join(HERE, '..', '..', '..');
const PUBLIC_DIR = join(WEB_ROOT, 'public');
const SPRITE_PUBLIC_OUT = join(PUBLIC_DIR, 'sprite.svg');
const MARK_PUBLIC_OUT = join(PUBLIC_DIR, 'mark.svg');

const BANNER = '<!-- GENERATED FILE. DO NOT EDIT. Run: node apps/web/src/design/icons/build.mjs -->';
/* TypeScript cannot start with an HTML comment, so tokens.ts gets a line-comment banner. */
const TS_BANNER = '// GENERATED FILE. DO NOT EDIT. Run: node apps/web/src/design/icons/build.mjs';

/* ------------------------------------------------------------------ SVGO ----
   A local, dependency-free optimiser covering the transforms that actually move
   the byte count on hand-authored 24px glyphs. It is deliberately conservative:
   it never touches path data, because rewriting numbers on a 1.75px stroke is
   where an over-eager optimiser silently shifts a vertex by a pixel. */

const ROOT_ATTRS = new Set(['xmlns', 'viewBox', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin']);

/** Strip comments, collapse inter-tag whitespace, drop the root's default
 *  attributes from children that repeat them. Byte-safe and reversible. */
function optimiseSvg(svg) {
  let out = svg
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<\?xml[\s\S]*?\?>/g, '')
    .replace(/<!DOCTYPE[^>]*>/gi, '');

  out = out.replace(/>\s+</g, '><').trim();

  // The root carries the paint defaults for the whole symbol; children that
  // repeat them verbatim are pure bytes. Children that DIFFER (a filled node,
  // a decaying stroke-width, a dasharray) are untouched.
  const rootOpen = out.match(/^<svg\b([^>]*)>/);
  if (rootOpen) {
    const defaults = new Map();
    for (const m of rootOpen[1].matchAll(/([\w-]+)="([^"]*)"/g)) {
      if (ROOT_ATTRS.has(m[1])) defaults.set(m[1], m[2]);
    }
    for (const [name, value] of defaults) {
      const re = new RegExp(
        `(<(?:path|circle|rect|line|polyline|polygon|ellipse)\\b[^>]*?)\\s${name}="${value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"`,
        'g',
      );
      out = out.replace(re, '$1');
    }
  }
  return out;
}

/** Pull the inner markup of an SVG document, keeping the root attributes. */
function toSymbolBody(svg) {
  const inner = svg
    .replace(/^[\s\S]*?<svg\b[^>]*>/, '')
    .replace(/<\/svg>\s*$/, '')
    .trim();
  const attrs = svg.match(/^[\s\S]*?<svg\b([^>]*)>/);
  const viewBox = attrs && /viewBox="([^"]+)"/.exec(attrs[1]);
  return { viewBox: viewBox ? viewBox[1] : '0 0 24 24', body: inner };
}

/* ---------------------------------------------------------------- sprite ---- */

async function buildSprite() {
  const files = (await readdir(SRC_DIR)).filter((f) => f.endsWith('.svg')).sort();
  if (files.length === 0) throw new Error('no glyphs found in ' + SRC_DIR);

  const symbols = [];
  for (const file of files) {
    const raw = await readFile(join(SRC_DIR, file), 'utf8');
    const name = file.replace(/\.svg$/, '');
    const { viewBox, body } = toSymbolBody(optimiseSvg(raw));
    symbols.push(
      `  <symbol id="i-${name}" viewBox="${viewBox}" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="square" stroke-linejoin="round">\n` +
        `    ${body}\n` +
        '  </symbol>',
    );
  }

  const sprite =
    `${BANNER}\n` +
    '<!-- OXBOW glyph sprite. One request, zero per-icon JavaScript.\n' +
    '     Referenced as <use href="/sprite.svg#i-cycle" />; colour follows currentColor. -->\n' +
    '<svg xmlns="http://www.w3.org/2000/svg" style="display:none">\n' +
    `${symbols.join('\n')}\n` +
    '</svg>\n';

  await writeIfChanged(SPRITE_OUT, sprite);
  // Publish the same bytes to the web public root so Next.js serves them. The
  // browser fetches /sprite.svg, not the file next to the sources.
  await mkdir(PUBLIC_DIR, { recursive: true });
  await writeIfChanged(SPRITE_PUBLIC_OUT, sprite);
  const names = files.map((f) => f.replace(/\.svg$/, ''));
  return { count: files.length, names };
}

/* --------------------------------------------------------------- tokens ---- */

/** Parse a CSS declaration block into a plain object, skipping at-rules. */
function parseDeclarations(block) {
  const out = {};
  const body = block.replace(/@theme\s*\{/, '').replace(/\}\s*$/, '');
  for (const line of body.split('\n')) {
    const m = /^\s*(--[\w-]+)\s*:\s*([^;]+);/.exec(line);
    if (m) out[m[1]] = m[2].trim();
  }
  return out;
}

function toTsLiteral(value) {
  return value.includes(',') && !value.startsWith('cubic-bezier') ? `[${value}]` : `'${value}'`;
}

async function buildTokens() {
  const css = await readFile(TOKENS_CSS, 'utf8');

  const themeMatch = /@theme\s*\{([\s\S]*?)\n\}/.exec(css);
  if (!themeMatch) throw new Error('no @theme block found in tokens.css');
  const theme = parseDeclarations(themeMatch[1]);

  const paperMatch = /\[data-theme='paper'\]\s*\{([\s\S]*?)\n\}/.exec(css);
  const paperRaw = paperMatch ? parseDeclarations(paperMatch[1]) : {};

  /* Paper mode re-points some tokens at other tokens. A canvas renderer cannot follow
   * `var(--color-paper)`, so every reference is resolved against the theme here, to
   * the literal colour. The cascade still does the work in CSS; the mirror carries
   * finished values so Cytoscape and lightweight-charts get what they can read. */
  const resolve = (value, depth = 0) => {
    if (depth > 8) return value;
    return value.replace(/var\((--[\w-]+)\)/g, (_, ref) => {
      const target = theme[ref] ?? paperRaw[ref];
      return target === undefined ? value : resolve(target, depth + 1);
    });
  };
  const paper = Object.fromEntries(Object.entries(paperRaw).map(([k, v]) => [k, resolve(v)]));

  const pick = (prefix) =>
    Object.fromEntries(
      Object.entries(theme)
        .filter(([k]) => k.startsWith(`--${prefix}`))
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([k, v]) => [k.replace(`--${prefix}`, '').replace(/-/g, '_'), v]),
    );

  const colours = pick('color-');
  const durations = pick('duration-');
  const easings = pick('ease-');

  const bands = Object.fromEntries(
    ['a', 'b', 'c', 'd', 'e'].map((band) => [band.toUpperCase(), theme['--color-band-' + band]]),
  );

  const typologies = Object.fromEntries(
    Object.entries(colours)
      .filter(([k]) => k.startsWith('typo_'))
      .map(([k, v]) => [k.toUpperCase(), v]),
  );

  const emit = (name, value) => `  ${name}: ${toTsLiteral(value)},`;

  const ts =
    `${TS_BANNER}\n` +
    '// OXBOW design tokens, mirrored for Cytoscape, canvas and lightweight-charts.\n' +
    '// These renderers cannot read CSS custom properties, so the values are compiled\n' +
    '// here from tokens.css, with every paper-mode var() reference resolved to a\n' +
    '// literal colour. Risk is never encoded by colour alone: every band ships with\n' +
    '// bandMeterSegments() as well as its hue.\n' +
    '\n' +
    `export const tokens = {\n` +
    `  colours: {\n` +
    Object.entries(colours)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => emit(k, v))
      .join('\n') +
    `\n  },\n` +
    `  paper: {\n` +
    Object.entries(paper)
      .filter(([k]) => k.startsWith('--color-'))
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => emit(k.replace('--color-', '').replace(/-/g, '_'), v))
      .join('\n') +
    `\n  },\n` +
    `  durations: {\n` +
    Object.entries(durations)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => emit(k, v))
      .join('\n') +
    `\n  },\n` +
    `  easings: {\n` +
    Object.entries(easings)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => emit(k, v))
      .join('\n') +
    `\n  },\n` +
    `} as const;\n` +
    `\n` +
    `export type TokenColour = keyof typeof tokens.colours;\n` +
    `export type Band = 'A' | 'B' | 'C' | 'D' | 'E';\n` +
    `export type TypologyId =\n` +
    `  | 'R1_RAPID_PASS_THROUGH'\n` +
    `  | 'R2_FAN_IN'\n` +
    `  | 'R3_FAN_OUT'\n` +
    `  | 'R4_CYCLE_MEMBER'\n` +
    `  | 'R5_STRUCTURING'\n` +
    `  | 'R6_VELOCITY_SPIKE'\n` +
    `  | 'R7_DORMANT_REACTIVATION'\n` +
    `  | 'R8_ODD_HOUR_SHIFT'\n` +
    `  | 'R9_AMOUNT_REGIME_SHIFT'\n` +
    `  | 'R10_FAST_CASH_OUT'\n` +
    `  | 'R11_NEW_COUNTERPARTY_SURGE'\n` +
    `  | 'R12_CHAIN_MEMBER';\n` +
    `\n` +
    `/** The five band colours, named. Each sits at equal lightness; chroma carries severity. */\n` +
    `export const BAND_COLOURS = {\n` +
    Object.entries(bands)
      .map(([k, v]) => emit(k, v))
      .join('\n') +
    `\n} as const satisfies Record<Band, string>;\n` +
    `\n` +
    `/** Semantic typology colours, keyed by rule id. Never a risk signal on its own. */\n` +
    `export const TYPOGRAPHY_COLOURS = {\n` +
    Object.entries(typologies)
      .map(([k, v]) => emit(k, v))
      .join('\n') +
    `\n} as const;\n` +
    `\n` +
    `/** The reserved accent. Its only job is marking what the thing you clicked is made of. */\n` +
    `export const EVIDENCE = '${theme['--color-evidence']}' as const;\n` +
    `\n` +
    `export const CANVAS = '${theme['--color-canvas']}' as const;\n` +
    `export const CANVAS_RAISED = '${theme['--color-canvas-raised']}' as const;\n` +
    `export const HAIRLINE = '${theme['--color-hairline']}' as const;\n` +
    `export const INK = '${theme['--color-ink']}' as const;\n` +
    `export const INK_MUTED = '${theme['--color-ink-muted']}' as const;\n` +
    `export const INK_FAINT = '${theme['--color-ink-faint']}' as const;\n` +
    `\n` +
    `export const DURATIONS = {\n` +
    Object.entries(durations)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => emit(k.toUpperCase(), v))
      .join('\n') +
    `\n} as const;\n` +
    `\n` +
    `export const EASINGS = {\n` +
    Object.entries(easings)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => emit(k.toUpperCase(), v))
      .join('\n') +
    `\n} as const;\n` +
    `\n` +
    `export type DurationToken = keyof typeof DURATIONS;\n` +
    `export type EasingToken = keyof typeof EASINGS;\n` +
    `\n` +
    `const SEGMENTS_PER_BAND: Readonly<Record<Band, number>> = { A: 1, B: 2, C: 3, D: 4, E: 5 };\n` +
    `\n` +
    `/**\n` +
    ` * The redundant channel that makes risk survivable without colour: how many of the\n` +
    ` * five meter segments are lit. A greyscale export packet keeps the band.\n` +
    ` */\n` +
    `export function bandMeterSegments(band: Band): number {\n` +
    `  return SEGMENTS_PER_BAND[band];\n` +
    `}\n` +
    `\n` +
    `/** CSS custom property value for the band-meter glyph, which fills to the band. */\n` +
    `export function bandMeterVar(band: Band): string {\n` +
    `  return String(SEGMENTS_PER_BAND[band]);\n` +
    `}\n` +
    `\n` +
    `export function bandColour(band: Band): string {\n` +
    `  return BAND_COLOURS[band];\n` +
    `}\n`;

  await writeIfChanged(TOKENS_TS, ts);
  return {
    colourTokens: Object.keys(colours).length,
    bandTokens: Object.keys(bands).length,
  };
}

/* ------------------------------------------------------------ idempotence --- */

async function writeIfChanged(path, content) {
  let existing = null;
  try {
    existing = await readFile(path, 'utf8');
  } catch {
    /* first run */
  }
  if (existing === content) return false;
  await writeFile(path, content, 'utf8');
  return true;
}

/* ------------------------------------------------------------------- main --- */

async function main() {
  const sprite = await buildSprite();
  const tok = await buildTokens();
  // The mark is the favicon, the app mark and the export-packet cover, so it
  // belongs in the public root alongside the sprite.
  await mkdir(PUBLIC_DIR, { recursive: true });
  await writeIfChanged(MARK_PUBLIC_OUT, await readFile(join(HERE, 'mark.svg'), 'utf8'));
  process.stdout.write(
    `sprite.svg  ${sprite.count} symbols (${sprite.names.join(', ')})\n` +
      `tokens.ts  ${tok.colourTokens} colours, ${tok.bandTokens} bands\n` +
      `public/    sprite.svg, mark.svg\n`,
  );
}

main().catch((err) => {
  process.stderr.write('build failed: ' + (err instanceof Error ? err.message : String(err)) + '\n');
  process.exitCode = 1;
});
