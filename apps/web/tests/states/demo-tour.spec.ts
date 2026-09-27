/**
 * The demo tour the submission video is captured from — component 03 of SUBMISSION.md.
 *
 * Three things make this a test rather than a screen recording done by hand:
 *
 * **It runs on the clock the script already fixes.** `SUBMISSION.md` §03 allocates a slot to
 * each beat (0:00-0:20, 0:20-0:50, …), and `scripts/make_narration.py` pads the synthesized
 * voice onto that same schedule. The tour holds each screen until its slot ends, so picture and
 * `out/narr/timeline.wav` line up on their own instead of being nudged together afterwards —
 * which is how a voiceover ends up describing the screen behind it rather than the one in front.
 *
 * **It produces frames, not a Playwright video.** `video: 'on'` needs Playwright's own ffmpeg
 * build under `~/.cache/ms-playwright/ffmpeg-*`; this project installs no browsers and downloads
 * nothing, so the machine has a cached Chromium and a system ffmpeg, and that is the whole
 * toolchain. The tour therefore screenshots on a timer and `scripts/make_demo_video.py`
 * assembles the frames with the ffmpeg that is actually here. A recording path that only works
 * after a download is a path that fails silently on the day, in front of a deadline.
 *
 * **It refuses to film a blank app.** Each stop waits out its skeletons and fails if they do not
 * clear, so a take cannot be made against loading states and then narrated as numbers. That is
 * the defect the earlier `docs/screens/` set carried: honest captures, but of a read-only
 * warehouse, so the panes showed skeleton rows and one correctly said the table does not exist in
 * this deployment.
 *
 * Live mode only, deliberately: against the bundled fixture every figure on screen is a sample
 * the repository ships as a sample, and the video would demonstrate the fixture rather than the
 * pipeline. So this skips with a named reason when the live origin is not configured.
 */
import { existsSync, mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';

import { expect, test } from '@playwright/test';

// Playwright's own video and trace are not used: video wants the ffmpeg bundle this project
// does not install, and a trace of a 3:30 take is a larger artifact than the film. These have to
// sit at module scope — `test.use()` inside a `describe` forces a new worker and Playwright
// refuses it with a hard error at collection, which is a way to discover that on filming day.
test.use({ trace: 'off', video: 'off', screenshot: 'off' });

const LIVE_BASE = process.env.OXBOW_LIVE_WEB_BASE_URL;
const CASE_ID = process.env.OXBOW_DEMO_CASE_ID;
// The account the graph beat walks. It comes from the environment for the same reason the case
// id does: `ACC-00DORM` is a fixture key, and asking the live warehouse for it returns a pane
// that correctly says the account is not here — so a tour filmed from the sample URL would
// narrate a network over an empty state, which is the exact defect this file exists to refuse.
const ACCOUNT_KEY = process.env.OXBOW_DEMO_ACCOUNT_KEY;
const FRAMES_DIR = process.env.OXBOW_DEMO_FRAMES_DIR ?? 'out/video/frames';

/** How often a frame is taken, in milliseconds. At 500 ms a 3:30 tour is roughly 420 frames. */
const FRAME_EVERY_MS = 500;

/** Beat slots, ending on the second `SUBMISSION.md` §03 and the narration timeline agree on. */
const BEATS: readonly { number: number; label: string; path: string; to: number }[] = [
  { number: 1, label: 'command', path: '/dashboard', to: 20 },
  { number: 2, label: 'scorecard', path: '/scorecard', to: 50 },
  { number: 3, label: 'network', path: '/network?account=DEMO_ACCOUNT&hops=2', to: 85 },
  { number: 4, label: 'queue', path: '/alerts', to: 120 },
  { number: 5, label: 'case', path: '/cases/DEMO_CASE', to: 155 },
  { number: 6, label: 'validation', path: '/model', to: 180 },
  { number: 7, label: 'closing', path: '/dashboard', to: 210 },
];

/** Why the tour cannot be filmed yet, or null when it can. Computed at collection so a missing
 * prerequisite skips without launching a browser to find out. */
function blocked(): string | null {
  if (LIVE_BASE === undefined || LIVE_BASE === '') {
    return (
      'OXBOW_LIVE_WEB_BASE_URL is not set: the tour records the live app only, because a video ' +
      'of the bundled fixture demonstrates the fixture. Start the app profile (`make up-full`) ' +
      'and point the variable at it.'
    );
  }
  if (CASE_ID === undefined || CASE_ID === '') {
    return (
      'OXBOW_DEMO_CASE_ID is not set: beat 5 opens a real case carrying a landed decision, so ' +
      'the demo snapshot has to exist and name the case it holds.'
    );
  }
  if (ACCOUNT_KEY === undefined || ACCOUNT_KEY === '') {
    return (
      'OXBOW_DEMO_ACCOUNT_KEY is not set: beat 3 walks a real account two hops out, and a ' +
      'fixture key asked of the live warehouse answers "not here", which would film an empty ' +
      'state under a sentence about cycles.'
    );
  }
  return null;
}

const reason = blocked();

test.describe('the demo tour', () => {
  test.skip(reason !== null, reason ?? '');
  // One continuous take: a separate file per beat would have to be stitched, and the stitch
  // is where audio and picture drift.
  test('walks the seven beats on the script clock', async ({ page }) => {
    test.setTimeout(420_000); // 3:30 of screen time, plus navigation and frame latency

    // The frames directory is the tour's only output and it is regenerated wholesale: a stale
    // frame from an earlier take sorts into the middle of this one, and the assembled video
    // shows numbers from a run nobody re-ran.
    rmSync(FRAMES_DIR, { recursive: true, force: true });
    mkdirSync(FRAMES_DIR, { recursive: true });

    const started = Date.now();
    let frame = 0;
    const shoot = async (): Promise<void> => {
      frame += 1;
      await page.screenshot({
        path: join(FRAMES_DIR, `frame${String(frame).padStart(6, '0')}.jpg`),
        type: 'jpeg',
        quality: 82,
      });
    };

    for (const beat of BEATS) {
      const path = beat.path.replace('DEMO_CASE', CASE_ID ?? '').replace('DEMO_ACCOUNT', ACCOUNT_KEY ?? '');
      await page.goto(`${LIVE_BASE}${path}`, { waitUntil: 'networkidle' });

      // The footer disclaimer is on every route and is what beat 7 says out loud; if it is
      // missing, the recording is non-compliant with the plan's own §8 rule.
      await expect(page.getByText(/research prototype/i).first()).toBeVisible({ timeout: 15_000 });

      // A pane still skeletoned has not answered. Dwelling on a skeleton for 35 seconds is a
      // video of a loading screen, so the take fails rather than filming it.
      const skeletons = page.locator('[data-slot="skeleton"], [data-skeleton="true"]');
      await expect(skeletons, `beat ${beat.number} (${beat.label}) never finished loading`).toHaveCount(0, {
        timeout: 20_000,
      });

      // Hold until the slot ends, framing as it goes.
      while (Date.now() - started < beat.to * 1000) {
        await shoot();
        const remaining = beat.to * 1000 - (Date.now() - started);
        if (remaining <= FRAME_EVERY_MS) break;
        await page.waitForTimeout(Math.min(FRAME_EVERY_MS, remaining));
      }
      // The only motion in the take: a slow pan that shows the pane is complete rather than
      // cropped, because a dead-still 35 seconds reads as a paused video.
      if (beat.number !== BEATS.length) {
        await page.mouse.wheel(0, 200);
        await shoot();
      }
    }

    console.log(`[tour] ${frame} frames into ${FRAMES_DIR}`);
    expect(existsSync(join(FRAMES_DIR, 'frame000001.jpg')), 'the first frame is missing').toBe(true);
    // Fewer than ~200 frames means the clock was not held: the loop exited early and the video
    // would run out of picture while the narration is still speaking.
    expect(frame, 'a 3:30 tour at one frame per 500 ms').toBeGreaterThan(200);
  });
});
