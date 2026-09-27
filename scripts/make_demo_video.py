"""Assemble the demo video from the frames the tour captured and the narration it was paced to.

The submission's component 03 is a 2-5 minute MP4 with voiceover. The two halves are produced by
`apps/web/tests/states/demo-tour.spec.ts` (numbered JPEG frames, held to the script's own beat
slots) and `scripts/make_narration.py` (one WAV per beat, padded onto the same slots into
`out/narr/timeline.wav`). This joins them with the system ffmpeg.

Why frames instead of Playwright's `video: 'on'`: that option requires Playwright's private
ffmpeg build under the browser cache, and this project downloads no browsers and installs nothing
at run time. Turning it on fails with `Executable doesn't exist at
.../ms-playwright/ffmpeg-1010/ffmpeg-win64.exe` — a failure that would land on the day of filming
rather than now.

    uv run python scripts/make_demo_video.py                 # assemble out/oxbow-demo.mp4
    uv run python scripts/make_demo_video.py --check-only    # report what it would need

The frame rate is derived, not declared: the tour's screenshots take a variable amount of time,
so a fixed `-framerate` would stretch or shrink the picture against a narration whose length is
already known. Dividing the frame count by the narration's own duration keeps the last frame on
screen when the last word is spoken, which is the property that matters for a cut with no
timeline editing in it.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRAMES = REPO_ROOT / "out" / "video" / "frames"
TIMELINE = REPO_ROOT / "out" / "narr" / "timeline.wav"
OUT = REPO_ROOT / "out" / "oxbow-demo.mp4"

MIN_SECONDS = 120  # Devpost: 2-5 minutes
MAX_SECONDS = 300


def seconds_of(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def frames() -> list[Path]:
    if not FRAMES.is_dir():
        return []
    return sorted(p for p in FRAMES.glob("frame*.jpg"))


def run(argv: list[str]) -> int:
    print("$ " + " ".join(part.replace("\\", "/") for part in argv))
    return subprocess.call(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def probe_seconds(path: Path) -> float | None:
    """Read the container's own duration back. An output is not verified by the exit code."""
    probe = shutil.which("ffprobe")
    if probe is None:
        return None
    result = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check-only", action="store_true", help="verify inputs, do not encode")
    parser.add_argument("--fps-cap", type=float, default=12.0)
    args = parser.parse_args(argv)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("FAIL  no ffmpeg on PATH; the assembly cannot run without it")
        return 1

    shots = frames()
    problems: list[str] = []
    if len(shots) < 20:
        problems.append(
            f"only {len(shots)} frames in {FRAMES.relative_to(REPO_ROOT)} — run the tour first "
            "(`node node_modules/@playwright/test/cli.js test demo-tour.spec.ts`)"
        )
    if not TIMELINE.is_file():
        problems.append(f"{TIMELINE.relative_to(REPO_ROOT)} is missing — run make_narration.py")
        duration = 0.0
    else:
        duration = seconds_of(TIMELINE)
        if duration < MIN_SECONDS:
            problems.append(
                f"narration is {duration:.0f}s, under the {MIN_SECONDS}s submission floor"
            )
        if duration > MAX_SECONDS:
            problems.append(
                f"narration is {duration:.0f}s, over the {MAX_SECONDS}s submission ceiling"
            )

    if problems:
        for line in problems:
            print(f"FAIL  {line}")
        return 1

    # Derived, so picture and voice end together. Screenshot latency varies, which a declared
    # frame rate would silently turn into drift.
    fps = min(args.fps_cap, len(shots) / duration)
    print(f"inputs: {len(shots)} frames, {duration:.1f}s of narration -> {fps:.3f} fps")

    if args.check_only:
        print("OK    inputs are sufficient; nothing encoded")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-framerate",
        f"{fps:.6f}",
        "-i",
        str(FRAMES / "frame%06d.jpg"),
        "-i",
        str(TIMELINE),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(OUT),
    ]
    code = run(cmd)
    if code != 0:
        print(f"FAIL  ffmpeg exited {code}")
        return code

    size = OUT.stat().st_size
    print(f"wrote {OUT.relative_to(REPO_ROOT)} at {size / 1_048_576:.1f} MiB")
    encoded = probe_seconds(OUT)
    if encoded is None:
        print("FAIL  ffprobe could not read the file back; nothing is verified about the output")
        return 1
    print(f"        {encoded:.1f}s as read back from the container")
    if abs(encoded - duration) > 2.0:
        print(
            f"FAIL  the film runs {encoded:.1f}s against {duration:.1f}s of narration; the last "
            "words would land on a frozen frame or the picture would outlive the voice"
        )
        return 1
    if size < 1_048_576:
        print(f"FAIL  {size} bytes is not a 3-minute screen recording; suspect an empty frame set")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
