/* =============================================================================
   Timestamp rendering — the zone is part of the value, never an assumption.

   Plan §14 names the failure: "a UTC timestamp shown to a UTC+3 analyst is an
   hour-hunting bug during a demo". The rule engine reads a separate `local_hour`
   column for exactly this reason (plan §7), and an ODD_HOUR_SHIFT rule written
   against UTC would flag an entire East African morning as suspicious.

   The zone name is NOT a literal here. `deployment_timezone` arrives from the API
   (config/pipeline.yaml's value, surfaced through `GET /api/meta/run`), the
   abbreviation is derived by `Intl` from that same zone, and the offset is printed
   alongside it because "EAT" alone does not tell a reader how far from UTC the
   clock is. A component that hardcodes "+3" would be inventing a number, which is
   the other rejection trigger.
   ============================================================================= */

export type ZonedInstant = {
  /** Wall-clock text in the deployment zone: "14 Sep 2026 09:42". */
  local: string;
  /** The zone name `Intl` derives for that instant: `EAT`, or `East Africa Time` on an
   * ICU build with no short form for the zone. Never typed here, always asked for. */
  abbrev: string;
  /** Numeric offset from UTC in hours, positive east: 3. */
  offsetHours: number;
  /** The zone identifier the whole thing was computed in. */
  timeZone: string;
  /** Whether the underlying instant is the epoch sentinel the API uses for "unknown". */
  missing: boolean;
};

const DATE_CACHE = new Map<string, Intl.DateTimeFormat>();

function formatter(timeZone: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${timeZone}|${JSON.stringify(options)}`;
  const existing = DATE_CACHE.get(key);
  if (existing !== undefined) return existing;
  const created = new Intl.DateTimeFormat('en-GB', { timeZone, ...options });
  DATE_CACHE.set(key, created);
  return created;
}

/**
 * Parts-based conversion: `formatToParts` gives the wall-clock fields *in the zone*,
 * and the short zone name comes from the same call, so there is exactly one place
 * the abbreviation can come from. No arithmetic on the ISO string, which is how a
 * DST-free zone and a DST-observing zone end up looking the same to the code.
 */
export function zoned(iso: string | null, timeZone: string): ZonedInstant {
  if (iso === null || iso.length === 0) {
    return { local: '—', abbrev: timeZone, offsetHours: 0, timeZone, missing: true };
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return { local: iso, abbrev: 'unparsed', offsetHours: 0, timeZone, missing: true };
  }

  const parts = formatter(timeZone, {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZoneName: 'short',
  }).formatToParts(date);

  const pick = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((part) => part.type === type)?.value ?? '';

  const datePart = `${pick('day')} ${pick('month')} ${pick('year')}`;
  const timePart = normaliseHours(pick('hour'), pick('minute'));
  const zone = zoneLabel(date, timeZone, pick('timeZoneName'));

  return {
    local: `${datePart} ${timePart}`,
    abbrev: zone,
    offsetHours: offsetInHours(date, timeZone),
    timeZone,
    missing: false,
  };
}

/**
 * The zone's own name, or the closest thing this `Intl` build actually has.
 *
 * `timeZoneName: "short"` is the abbreviation §14 asks for, but ICU ships no short
 * form for some zones and renders `GMT+3` instead -- an offset wearing a name, which
 * is the hour-hunting bug in its quieter form: nothing tells the reader which zone the
 * wall clock was taken in. When the short form is only an offset, the long form is
 * asked for ("East Africa Time"), and the offset is still printed beside it.
 */
function zoneLabel(
  date: Date,
  timeZone: string,
  shortName: string,
): string {
  if (!/^GMT([+-]|$)/.test(shortName)) return shortName;
  const parts = formatter(timeZone, {
    year: 'numeric',
    timeZoneName: 'long',
  }).formatToParts(date);
  const longName = parts.find((part) => part.type === 'timeZoneName')?.value ?? '';
  return longName === '' ? shortName : longName;
}

/** `Intl` renders midnight in en-GB as "24" with hour12: false; "00" is what reads. */
function normaliseHours(hour: string, minute: string): string {
  const normalised = hour === '24' ? '00' : hour.padStart(2, '0');
  return `${normalised}:${minute === '' ? '00' : minute}`;
}

/**
 * Offset computed rather than declared: the difference between the zone's wall
 * clock and UTC's, in hours. It is what makes "+03" a derived fact and not a
 * string someone remembered to type.
 */
export function offsetInHours(date: Date, timeZone: string): number {
  const asUtc = date.getTime();
  const zonedParts = formatter(timeZone, {
    hour12: false,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(date);
  const value = (type: Intl.DateTimeFormatPartTypes): number =>
    Number(zonedParts.find((part) => part.type === type)?.value ?? '0');
  const wallAsUtc = Date.UTC(
    value('year'),
    value('month') - 1,
    value('day'),
    value('hour') === 24 ? 0 : value('hour'),
    value('minute'),
    value('second'),
  );
  return Math.round(((wallAsUtc - asUtc) / 3_600_000) * 100) / 100;
}

/** The full label a screen prints: `14 Sep 2026 09:42 EAT (UTC+3)`. */
export function formatInstant(iso: string | null, timeZone: string): string {
  const instant = zoned(iso, timeZone);
  if (instant.missing) return instant.local;
  return `${instant.local} ${instant.abbrev} (${formatOffset(instant.offsetHours)})`;
}

export function formatOffset(hours: number): string {
  const sign = hours < 0 ? '-' : '+';
  const absolute = Math.abs(hours);
  const whole = Math.floor(absolute);
  const minutes = Math.round((absolute - whole) * 60);
  return minutes === 0
    ? `UTC${sign}${String(whole)}`
    : `UTC${sign}${String(whole)}:${String(minutes).padStart(2, '0')}`;
}

/** Date only, for an axis tick or a period label. Zone is still stated once, nearby. */
export function formatDate(iso: string | null, timeZone: string): string {
  const instant = zoned(iso, timeZone);
  if (instant.missing) return instant.local;
  return instant.local.slice(0, instant.local.indexOf(' '));
}

/** Hours and minutes of a duration in ms, for a stage ledger or a review estimate. */
export function formatDuration(minutes: number): string {
  const whole = Math.trunc(minutes);
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  if (hours === 0) return `${String(rest)} min`;
  return rest === 0 ? `${String(hours)} h` : `${String(hours)} h ${String(rest)} min`;
}

/** The sentinel the API uses for "this row has no recorded instant". */
export const NO_TIMESTAMP = new Date(0).toISOString();
