"""Synthesize the demo narration from the script that is already pinned in `SUBMISSION.md`.

Component 03 of the submission is a 2-5 minute video with narration. The narration text and
its slot budget live in one place — the beat table under `## 03 · Demo video` in
`SUBMISSION.md` — because a second copy in a script is a second copy that goes stale: the
beats get reworded, the video is re-shot against the old words, and the submission ships a
voiceover that contradicts its own written script.

So this reads the table, synthesizes one WAV per beat with a neural voice through `edge-tts`
(fetched with `uvx --from edge-tts`, so nothing is added to the project's dependencies), and
then reports each beat's *measured* audio length against the slot the table allocates for it.
That last part is the reason the script exists as a program rather than as a comment: TTS
length is not what a prose reading time suggests, and a 3:30 plan whose beats actually total
4:50 fails the Devpost limit only after the video has been cut.

    uv run python scripts/make_narration.py            # synthesize all beats
    uv run python scripts/make_narration.py --check    # measure only, say what does not fit

The beats are joined for `ffmpeg -f concat` by the `beats.txt` this writes beside the WAVs.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import wave
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SOURCE = REPO_ROOT / "SUBMISSION.md"
OUT_DIR = REPO_ROOT / "out" / "narr"

#: Microsoft's Kenyan English neural voice. `en-KE-AsiliaNeural`/`ChilembaNeural` are the two
#: `en-KE` voices on the service; Asilia is the clearer of the pair on technical prose.
VOICE: str = "en-KE-AsiliaNeural"
#: The voice's own pace. A slowed read overruns the script's beat slots (see `synthesize`).
RATE: str = "+0%"

#: Matches `| 1 | 0:00-0:20 | Command strip | "narration" |`. The slot column is written with
#: an en dash (U+2013) in `SUBMISSION.md` and the header row uses an ordinary hyphen, so both
#: are accepted; the narration is quoted with typographic quotes, which `clean` below removes.
EN_DASH = chr(0x2013)
#: Built from `chr` rather than a literal because the code must match what the document
#: actually contains, and an ambiguous Unicode character written down as itself is invisible
#: to the next reader and a lint finding to the next run.
BEAT_ROW = re.compile(
    rf"^\|\s*(\d+)\s*\|\s*([\d:]+)\s*[{EN_DASH}-]\s*([\d:]+)\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$"
)
SECTION_HEAD = "## 03 · Demo video"
NEXT_SECTION = "\n## "


def beats() -> list[dict[str, object]]:
    """The beat table, in order, with its slot parsed into seconds."""
    text = SCRIPT_SOURCE.read_text(encoding="utf-8")
    start = text.index(SECTION_HEAD)
    body = text[start + len(SECTION_HEAD) : start + len(SECTION_HEAD) + 20000]
    cut = body.find(NEXT_SECTION, 10)
    if cut != -1:
        body = body[:cut]
    found: list[dict[str, object]] = []
    for line in body.splitlines():
        match = BEAT_ROW.match(line.strip())
        if match is None:
            continue
        number, slot_from, slot_to, surface, narration = match.groups()
        if number == "#":  # the table header row
            continue
        found.append(
            {
                "number": int(number),
                "slot": (to_seconds(slot_from), to_seconds(slot_to)),
                "surface": surface,
                "narration": clean(narration),
            }
        )
    return found


def to_seconds(stamp: str) -> int:
    minutes, _, seconds = stamp.partition(":")
    return int(minutes) * 60 + int(seconds)


def clean(quoted: str) -> str:
    """The narration with its surrounding quotes and markdown emphasis removed."""
    text = quoted.strip().strip('"').strip()
    for fancy, plain in (
        (chr(0x2019), "'"),
        (chr(0x2018), "'"),
        (chr(0x201C), '"'),
        (chr(0x201D), '"'),
    ):
        text = text.replace(fancy, plain)
    text = text.replace("*", "")
    # A colon inside a spoken line makes `System.Speech` read the clause as a heading and the
    # prosody drops; a comma says the same thing at the pace the video needs.
    return re.sub(r"\s+", " ", text).strip()


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def build_timeline(rows: list[dict[str, object]], out_dir: Path) -> Path:
    """One WAV with each beat starting at its own slot, silence between them.

    The seven beats synthesize to about 2:32 while the on-screen plan runs to 3:30, so a plain
    concatenation would leave the last minute of the video silent and the voice drifting ahead
    of the route it is describing. Padding each beat out to its slot on a single timeline is
    what lets the video be cut from the script's own clock instead of re-timed by hand — and a
    hand-timed voiceover is how a submission ends up with narration describing the wrong screen.
    """
    timeline = out_dir / "timeline.wav"
    wavs = paths_to_wav(rows, out_dir)
    with wave.open(str(wavs[0]), "rb") as first:
        rate, width, channels = first.getframerate(), first.getsampwidth(), first.getnchannels()

    chunks: list[bytes] = []
    cursor = 0.0
    for row, wav in zip(rows, wavs, strict=True):
        slot_from, _slot_to = row["slot"]  # type: ignore[misc]
        with wave.open(str(wav), "rb") as handle:
            if (handle.getframerate(), handle.getsampwidth(), handle.getnchannels()) != (
                rate,
                width,
                channels,
            ):
                raise SystemExit(f"{wav.name}: a different sample format, refusing to splice")
            frames = handle.readframes(handle.getnframes())
        pad = int(max(0.0, float(slot_from) - cursor) * rate) * width * channels
        chunks.append(b"\x00" * pad)
        chunks.append(frames)
        cursor = max(cursor + pad / (rate * width * channels), float(slot_from)) + len(frames) / (
            rate * width * channels
        )

    with wave.open(str(timeline), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(b"".join(chunks))
    return timeline


def paths_to_wav(rows: list[dict[str, object]], out_dir: Path) -> list[Path]:
    return [out_dir / f"beat{row['number']}.wav" for row in rows]


def _tool_env() -> dict[str, str]:
    """This process's environment with the Python locators stripped.

    `uv run python scripts/make_narration.py` exports `PYTHONHOME`, `PYTHONPATH`,
    `VIRTUAL_ENV` and `UV_PROJECT_ENVIRONMENT` for *this* interpreter, and `uvx` honours them
    when it resolves the tool's own interpreter -- which is a different Python. The failure is
    `Could not import runpy._run_module_as_main`, and it looks like a broken install rather
    than an inherited variable, so the strip is named here rather than left to the next reader.
    """
    drop = {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONEXECUTABLE"}
    return {key: value for key, value in os.environ.items() if key not in drop}


def synthesize(beat: dict[str, object], out: Path, *, voice: str = VOICE) -> None:
    """One beat, spoken by a neural voice, landed as the canonical WAV the timeline splices.

    `edge-tts` rather than `scripts/speak.ps1`: the local `System.Speech` synthesis is the
    desktop SAPI voice, which is intelligible and unmistakably a 2010 screen reader, and a
    submission's only audio is the one thing a judge cannot skim. The voice is Kenyan English
    for the same reason the interface prints East Africa Time and UGX — this is a mobile-money
    network in that region, and a generic American voice would be the first thing that made
    the framing look like set dressing.

    Rate is `+0%`. The earlier plan called for a slightly slowed read; measured against the
    beat slots, a slowed voice overruns the 3:30 script and the last beat lands on a frozen
    frame, so the pace is the voice's own and the *slots* absorb the difference.

    The MP3 is transcoded to 24 kHz mono 16-bit because `build_timeline` refuses to splice two
    beats whose sample formats differ, and it would rather refuse than concatenate silence into
    the wrong place.
    """
    mp3 = out.with_suffix(".mp3")
    if mp3.is_file():
        mp3.unlink()
    result = subprocess.run(
        [
            "uvx",
            "--from",
            "edge-tts",
            "edge-tts",
            "--voice",
            voice,
            "--rate",
            RATE,
            "--text",
            str(beat["narration"]),
            "--write-media",
            str(mp3),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_tool_env(),
    )
    if not mp3.is_file() or mp3.stat().st_size < 1_024:
        raise SystemExit(
            f"beat {beat['number']}: {voice} produced no audio. "
            f"{(result.stderr or result.stdout)[:400]}"
        )
    converted = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-ar", "24000", "-ac", "1", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    if not out.is_file():
        raise SystemExit(f"beat {beat['number']}: ffmpeg could not write {out.name}: {converted.stderr[:400]}")


def write_concat_list(paths: list[Path]) -> Path:
    beats_txt = OUT_DIR / "beats.txt"
    # ffmpeg's concat demuxer reads `file '<path>'` lines. `PurePosixPath` emits forward
    # slashes, which both ffmpeg and this shell agree on; a native backslashed path has to be
    # quoted twice over to survive both readers, and the audio would silently skip beats.
    lines = [f"file '{PurePosixPath(p.as_posix())}'" for p in paths]
    beats_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return beats_txt


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="measure existing waves; do not synthesize"
    )
    parser.add_argument(
        "--budget", type=int, default=300, help="hard Devpost ceiling in seconds (2-5 min)"
    )
    args = parser.parse_args(argv)

    rows = beats()
    if not rows:
        print(f"FAIL  no beat rows parsed from {SCRIPT_SOURCE.relative_to(REPO_ROOT)}")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    total_audio = 0.0
    over: list[str] = []

    print(f"{'beat':>4}  {'slot':>11}  {'audio':>7}  {'fit':>6}  surface")
    for beat in rows:
        out = OUT_DIR / f"beat{beat['number']}.wav"
        if not args.check:
            synthesize(beat, out)
        paths.append(out)
        slot_from, slot_to = beat["slot"]  # type: ignore[misc]
        slot = slot_to - slot_from
        audio = wav_seconds(out)
        total_audio += audio
        margin = slot - audio
        flag = "ok" if margin >= -0.5 else "OVER"
        if flag == "OVER":
            over.append(f"beat {beat['number']}: {audio:.1f}s of audio in a {slot}s slot")
        print(
            f"{beat['number']:>4}  {fmt(slot_from)}-{fmt(slot_to)} ({slot:>3}s)"
            f"  {audio:>6.1f}s  {flag:>6}  {beat['surface']}"
        )

    if not args.check:
        list_path = write_concat_list(paths)
        timeline = build_timeline(rows, OUT_DIR)
        print(f"concat list: {list_path.relative_to(REPO_ROOT)}")
        print(
            f"timeline:    {timeline.relative_to(REPO_ROOT)} "
            f"({wav_seconds(timeline):.1f}s, every beat at its own slot)"
        )

    planned = int(rows[-1]["slot"][1])  # type: ignore[arg-type]
    print(f"\ntotal audio {fmt(int(total_audio))} against a planned {fmt(planned)}")
    if over:
        print("FAIL  beats that do not fit their slots:")
        for line in over:
            print(f"      - {line}")
    if total_audio > args.budget:
        print(f"FAIL  {fmt(int(total_audio))} exceeds the {fmt(args.budget)} submission ceiling")
        return 1
    if over:
        return 1
    print("OK    every beat fits, and the whole is inside the ceiling")
    return 0


def fmt(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
