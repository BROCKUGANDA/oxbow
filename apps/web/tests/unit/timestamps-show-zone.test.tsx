/**
 * §14 `test_timestamps_show_zone` — every timestamp carries its deployment zone
 * abbreviation and its offset, both derived, never typed: a UTC instant shown to a
 * UTC+3 analyst is the hour-hunting bug the plan names, and a hardcoded "+3" would
 * be an invented number in a product whose rule is that nothing is invented.
 */
import { afterEach, describe, expect, it } from 'vitest';

import { Timestamp } from '@/components/ui/provenance';
import { formatInstant, formatOffset, zoned } from '@/lib/format/time';
import { cleanupAll, mustFind, render, text } from '@/test/render';

afterEach(cleanupAll);

describe('timestamps in the deployment zone', () => {
  it('prints the zone abbreviation and the offset next to the wall clock', () => {
    const view = render(<Timestamp iso="2026-09-24T06:12:00Z" timeZone="Africa/Kampala" sense="scored" />);
    const body = text(view.container);
    expect(body).toContain('09:12'); // 06:12Z is 09:12 in EAT
    // §14 wants the zone NAMED, derived, never typed. This host's ICU ships no short
    // form for Africa/Kampala and renders the offset `GMT+3` in its place, so the
    // formatter falls through to the long name. What must never be printed is that
    // bare offset alone: an offset is not a zone, and it is the hour-hunting bug with
    // the serial numbers filed off.
    expect(body).toMatch(/EAT|East Africa Time/);
    expect(body).not.toMatch(/GMT\+3/);
    expect(body).toContain('(UTC+3)');
    const time = mustFind<HTMLElement>(view.container, 'time');
    expect(time.getAttribute('datetime')).toBe('2026-09-24T06:12:00Z');
    expect(time.getAttribute('title')).toBe('2026-09-24T06:12:00Z');
    view.cleanup();
  });

  it('shows the same instant in UTC honestly, without pretending to be elsewhere', () => {
    const view = render(<Timestamp iso="2026-09-24T06:12:00Z" timeZone="UTC" sense="the same instant in UTC" />);
    const body = text(view.container);
    expect(body).toContain('06:12');
    expect(body).toContain('UTC');
    expect(body).toContain('(UTC+0)');
    view.cleanup();
  });

  it('refuses to guess: with no zone reported, it says the zone is unreported', () => {
    const view = render(<Timestamp iso="2026-09-24T06:12:00Z" timeZone={null} sense="x" />);
    expect(text(view.container)).toContain('zone unreported');
    expect(text(view.container)).not.toContain('EAT');
    view.cleanup();
  });

  it('a half-hour zone states the minutes rather than rounding to a whole hour', () => {
    const instant = zoned('2026-09-24T06:12:00Z', 'Asia/Kolkata');
    expect(instant.offsetHours).toBeCloseTo(5.5, 5);
    expect(formatOffset(5.5)).toBe('UTC+5:30');
    expect(formatInstant('2026-09-24T06:12:00Z', 'Asia/Kolkata')).toContain('UTC+5:30');
  });

  it('midnight is midnight, not 24:00, in the en-GB formatter', () => {
    const instant = zoned('2026-09-24T21:30:00Z', 'Africa/Kampala'); // 00:30 next day, EAT
    expect(instant.local).toContain('00:30');
    expect(instant.local).not.toContain('24:');
  });

  it('null and unparseable instants are named, never drawn as time', () => {
    const view = render(<Timestamp iso={null} timeZone="Africa/Kampala" sense="nothing recorded" />);
    expect(text(view.container)).toContain('—');
    const unparsed = zoned('not-a-date', 'Africa/Kampala');
    expect(unparsed.missing).toBe(true);
    expect(unparsed.local).toBe('not-a-date');
    view.cleanup();
  });
});
