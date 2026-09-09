#!/usr/bin/env python3
"""Interactive 3.5 mm phone-audio splitter diagnostic.

This intentionally uses the motherboard analog ALSA device directly.  It does
not touch the frozen Android SMS APK, install anything, or send phone data
anywhere.
"""
from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

RATE = 48_000
FREQUENCY = 1_000.0
CARD = "hw:0,0"
PLAYBACK_DEVICE = "pipewire"


def require(command: str) -> str:
    path = shutil.which(command)
    if not path:
        raise RuntimeError(f"required command not found: {command}")
    return path


def make_tone(path: Path, seconds: float, amplitude: float = 0.08) -> None:
    frames = bytearray()
    for n in range(round(RATE * seconds)):
        value = round(32767 * amplitude * math.sin(2 * math.pi * FREQUENCY * n / RATE))
        frames += int(value).to_bytes(2, "little", signed=True) * 2
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(frames)


def read_samples(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        if width != 2:
            raise RuntimeError(f"unexpected capture width: {width * 8}-bit")
        raw = source.readframes(source.getnframes())
    values = [int.from_bytes(raw[i : i + 2], "little", signed=True) for i in range(0, len(raw), 2)]
    return values[::channels]


def metrics(path: Path) -> tuple[float, float]:
    samples = read_samples(path)
    if not samples:
        return 0.0, 0.0
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768.0
    # Goertzel magnitude, normalized to a convenient relative amplitude.
    k = round(len(samples) * FREQUENCY / RATE)
    omega = 2.0 * math.pi * k / len(samples)
    coefficient = 2.0 * math.cos(omega)
    state1 = state2 = 0.0
    for sample in samples:
        state0 = sample / 32768.0 + coefficient * state1 - state2
        state2, state1 = state1, state0
    tone = math.sqrt(state1 * state1 + state2 * state2 - coefficient * state1 * state2) / len(samples) * 2
    return rms, tone


def input_sources() -> list[str]:
    result = subprocess.run(
        ["amixer", "-c", "0", "sget", "Input Source"],
        capture_output=True, text=True, check=False,
    )
    return [line.split("'", 2)[1] for line in result.stdout.splitlines() if "Item0:" in line and "'" in line]


def set_source(index: int, source: str) -> None:
    subprocess.run(
        ["amixer", "-c", "0", "sset", f"Input Source,{index}", source],
        capture_output=True, text=True, check=False,
    )


def record(path: Path, seconds: float) -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["arecord", "-D", CARD, "-f", "S16_LE", "-r", str(RATE), "-c", "2", "-d", str(round(seconds)), str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def play(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["aplay", "-D", PLAYBACK_DEVICE, str(path)], capture_output=True, text=True, check=False)


def capture_while_playing(capture: Path, tone: Path, seconds: float) -> tuple[float, float, str]:
    recorder = record(capture, seconds)
    time.sleep(0.35)
    playback = play(tone)
    stdout, stderr = recorder.communicate(timeout=seconds + 5)
    if recorder.returncode != 0:
        raise RuntimeError(f"arecord failed ({recorder.returncode}): {stderr.strip()}")
    if playback.returncode != 0:
        raise RuntimeError(f"aplay failed ({playback.returncode}): {playback.stderr.strip()}")
    rms, tone_level = metrics(capture)
    return rms, tone_level, playback.stdout.strip()


def verdict(rms: float, tone: float) -> str:
    # A tone must be clearly above both digital silence and a weak/noisy input.
    return "PASS" if rms >= 0.003 and tone >= 0.002 else "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=4.0, help="capture duration per direction")
    args = parser.parse_args()
    require("arecord")
    require("aplay")
    if args.seconds < 2:
        parser.error("--seconds must be at least 2")

    old_sources = input_sources()
    if not old_sources:
        raise RuntimeError("could not read ALSA Input Source state")
    tone_direction = "UNKNOWN"
    try:
        for index in range(len(old_sources)):
            set_source(index, "Line")
        with tempfile.TemporaryDirectory(prefix="hermes-phone-audio-") as directory:
            root = Path(directory)
            tone = root / "host-tone.wav"
            host_capture = root / "host-to-phone.wav"
            phone_capture = root / "phone-to-host.wav"
            make_tone(tone, args.seconds)

            print("HERMES PHONE 3.5 MM SPLITTER TEST")
            print(f"ALSA device: {CARD}; tone: {FREQUENCY:.0f} Hz; capture: {args.seconds:.1f}s")
            print("Input source set to Line for the duration of this test.")
            input("\nConnect the splitter, then press Enter to test computer -> phone -> computer... ")
            rms, tone_level, _ = capture_while_playing(host_capture, tone, args.seconds)
            print(f"computer -> phone -> computer: {verdict(rms, tone_level)}  RMS={rms:.5f}  1kHz={tone_level:.5f}")

            input("\nNow play a steady 1 kHz tone on the phone through the splitter, then press Enter; recording starts immediately... ")
            recorder = record(phone_capture, args.seconds)
            _, stderr = recorder.communicate(timeout=args.seconds + 5)
            if recorder.returncode != 0:
                raise RuntimeError(f"arecord failed ({recorder.returncode}): {stderr.strip()}")
            rms, tone_level = metrics(phone_capture)
            print(f"phone -> computer: {verdict(rms, tone_level)}  RMS={rms:.5f}  1kHz={tone_level:.5f}")
            print("\nInterpretation: PASS means the host captured a measurable 1 kHz signal, not merely that playback started.")
            print("A FAIL in either direction identifies the corresponding cable/splitter/port/input-routing path as unproven.")
    finally:
        for index, source in enumerate(old_sources):
            set_source(index, source)
        print("ALSA Input Source restored: " + ", ".join(old_sources), file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
