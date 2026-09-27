/* =============================================================================
   Runtime contract validation.
   Plan §14 / §18: "no route renders a value that is not in the API response" is a
   rejection trigger, so it cannot rest on a TypeScript annotation. A type
   disappears at runtime; a decoder does not.

   Every response body is decoded here before a component sees it. A field the
   client expects but the server did not send is a loud, typed failure at the
   boundary — never `undefined` coerced into a blank cell, and never a `?? 0`
   that turns a schema drift into a fabricated zero on a money figure.

   Decoders are also how the fixture transport and the real transport stay
   interchangeable: both produce the same `unknown` JSON, and only the decoder
   turns it into a type a component is allowed to render.
   ============================================================================= */

export type DecodeError = {
  readonly path: string;
  readonly message: string;
};

export type DecodeResult<T> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly error: DecodeError };

/** Thrown at the fetch boundary when a body does not satisfy its declared shape. */
export class ContractViolation extends Error {
  readonly path: string;

  constructor(path: string, message: string) {
    super(`contract violation at ${path}: ${message}`);
    this.name = 'ContractViolation';
    this.path = path;
  }
}

export interface Decoder<T> {
  readonly kind: string;
  decode(value: unknown, path: string): DecodeResult<T>;
}

function fail(path: string, message: string): { ok: false; error: DecodeError } {
  return { ok: false, error: { path, message } };
}

function ok<T>(value: T): { ok: true; value: T } {
  return { ok: true, value };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function typeName(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

/** Lifts a predicate into a decoder, with the narrow type it proves. */
export function refine<T>(kind: string, test: (value: unknown) => value is T): Decoder<T> {
  return {
    kind,
    decode(value, path) {
      return test(value) ? ok(value) : fail(path, `expected ${kind}, got ${typeName(value)}`);
    },
  };
}

export const unknownDecoder: Decoder<unknown> = {
  kind: 'unknown',
  decode(value, path) {
    void path;
    return ok(value);
  },
};

export const string: Decoder<string> = refine('string', (v): v is string => typeof v === 'string');
export const boolean: Decoder<boolean> = refine('boolean', (v): v is boolean => typeof v === 'boolean');
export const number: Decoder<number> = refine(
  'number',
  (v): v is number => typeof v === 'number' && Number.isFinite(v),
);

/** Integer minor units. DEV-005: money is never a float, so the client asserts it. */
export const integer: Decoder<number> = refine(
  'integer',
  (v): v is number => typeof v === 'number' && Number.isSafeInteger(v),
);

export function literal<T extends string>(expected: T): Decoder<T> {
  return refine(`"${expected}"`, (v): v is T => v === expected);
}

type InferOne<D> = D extends Decoder<infer T> ? T : never;

/** First decoder that validates wins. Discriminated unions are built on this. */
export function union<T extends readonly Decoder<unknown>[]>(...decoders: T): Decoder<InferOne<T[number]>> {
  return {
    kind: decoders.map((d) => d.kind).join('|'),
    decode(value, path) {
      for (const decoder of decoders) {
        const result = decoder.decode(value, path);
        if (result.ok) return result as DecodeResult<InferOne<T[number]>>;
      }
      return fail(path, `no member of ${decoders.map((d) => d.kind).join('|')} matched ${typeName(value)}`);
    },
  };
}

/** One-of-a-literal-set: the discriminator form the error union needs. */
export function oneOf<K extends string>(kind: string, ...allowed: readonly K[]): Decoder<K> {
  const set = new Set<string>(allowed);
  return refine(kind, (v): v is K => typeof v === 'string' && set.has(v));
}

export function nullable<T>(inner: Decoder<T>): Decoder<T | null> {
  return {
    kind: `${inner.kind}?`,
    decode(value, path) {
      if (value === null || value === undefined) return ok(null);
      const result = inner.decode(value, path);
      return result.ok ? ok(result.value) : result;
    },
  };
}

export function optional<T>(inner: Decoder<T>): Decoder<T | undefined> {
  return {
    kind: `${inner.kind}??`,
    decode(value, path) {
      if (value === undefined) return ok(undefined);
      const result = inner.decode(value, path);
      return result.ok ? ok(result.value) : result;
    },
  };
}

export function array<T>(inner: Decoder<T>): Decoder<T[]> {
  return {
    kind: `${inner.kind}[]`,
    decode(value, path) {
      if (!Array.isArray(value)) return fail(path, `expected array, got ${typeName(value)}`);
      const out: T[] = [];
      for (let index = 0; index < value.length; index += 1) {
        const result = inner.decode(value[index], `${path}[${String(index)}]`);
        if (!result.ok) return result;
        out.push(result.value);
      }
      return ok(out);
    },
  };
}

export function record<T>(inner: Decoder<T>): Decoder<Record<string, T>> {
  return {
    kind: `Record<${inner.kind}>`,
    decode(value, path) {
      if (!isRecord(value)) return fail(path, `expected object, got ${typeName(value)}`);
      const out: Record<string, T> = {};
      for (const key of Object.keys(value)) {
        const result = inner.decode(value[key], `${path}.${key}`);
        if (!result.ok) return result;
        out[key] = result.value;
      }
      return ok(out);
    },
  };
}

type DecoderMap = Record<string, Decoder<unknown>>;

type Infer<M extends DecoderMap> = { [K in keyof M]: M[K] extends Decoder<infer T> ? T : never };

/**
 * Object decoder. Keys absent from the map are dropped rather than passed
 * through, so a server growing a field the client never agreed to cannot leak it
 * into a component's props.
 */
export function object<M extends DecoderMap>(kind: string, map: M): Decoder<Infer<M>> {
  return {
    kind,
    decode(value, path) {
      if (!isRecord(value)) return fail(path, `expected ${kind} object, got ${typeName(value)}`);
      const out: Record<string, unknown> = {};
      for (const key of Object.keys(map)) {
        const decoder = map[key];
        if (decoder === undefined) continue;
        const result = decoder.decode(value[key], path.length > 0 ? `${path}.${key}` : key);
        if (!result.ok) return result;
        out[key] = result.value;
      }
      return ok(out as Infer<M>);
    },
  };
}

/** Map a validated value into a derived shape. Used for the money/assumption pairings. */
export function mapDecode<T, U>(inner: Decoder<T>, fn: (value: T) => U, kind: string): Decoder<U> {
  return {
    kind,
    decode(value, path) {
      const result = inner.decode(value, path);
      return result.ok ? ok(fn(result.value)) : result;
    },
  };
}

/** Run a decoder, throwing at the boundary rather than returning a result union. */
export function decodeOrThrow<T>(decoder: Decoder<T>, value: unknown): T {
  const result = decoder.decode(value, '');
  if (!result.ok) throw new ContractViolation(result.error.path, result.error.message);
  return result.value;
}

/** Non-null narrowing for a field decoded as nullable at the boundary. */
export function expect<T>(value: T | null | undefined, what: string): T {
  if (value === null || value === undefined) {
    throw new ContractViolation(what, 'required by the renderer but absent from the response');
  }
  return value;
}
