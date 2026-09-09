#!/usr/bin/env python3
"""Half-duplex audio worker for a live cellular call.

The Android bridge owns dialing. This process owns the physical computer-side
analog boundary: capture the phone return channel, transcribe it, ask the
already-prepared Gemma endpoint, synthesize the answer, and play it into the
phone microphone through the wired splitter.
"""
from __future__ import annotations

import argparse
import fcntl
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
import selectors
import shutil
import signal
import sys
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger("hermes_phone_call_audio")

from physical_success_gate import write_live_success_marker

# The worker is normally launched from Hermes-Phone, while STT/TTS helpers
# live in the Hermes checkout.  Make that dependency explicit so the live-call
# path does not depend on the caller's current working directory or PYTHONPATH.
HERMES_ROOT = Path(
    os.environ.get("HERMES_ROOT", str(Path.home() / ".hermes" / "hermes-agent"))
)
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))
REX_VOICE_ROOT = Path(
    os.environ.get("REX_VOICE_ROOT", str(Path.home() / "hermes"))
)
if str(REX_VOICE_ROOT) not in sys.path:
    sys.path.insert(0, str(REX_VOICE_ROOT))


def audio_settings_path() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "phone-audio.json"


def load_audio_settings() -> dict[str, Any]:
    path = audio_settings_path()
    if not path.exists():
        return {"backend": "analog"}
    with path.open(encoding="utf-8") as handle:
        settings = json.load(handle)
    if not isinstance(settings, dict) or settings.get("backend", "analog") not in {"analog", "bluetooth"}:
        raise ValueError(f"invalid phone audio settings: {path}")
    return {"backend": settings.get("backend", "analog")}


def save_audio_settings(backend: str) -> Path:
    if backend not in {"analog", "bluetooth"}:
        raise ValueError("audio backend must be analog or bluetooth")
    path = audio_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"backend": backend}, indent=2) + "\n", encoding="utf-8")
    return path


@dataclass(frozen=True)
class CallAudioConfig:
    audio_backend: str = "analog"
    capture_device: str = "hw:0,0"
    # The analog reply must go to the physical Line-Out jack feeding the
    # splitter, never to PipeWire's default sink (which may be speakers).
    playback_device: str = "hw:CARD=PCH,DEV=0"
    sample_rate: int = 48000
    channels: int = 2
    turn_seconds: int = 4  # retained for analog compatibility
    silence_rms: float = 0.003
    pre_roll_seconds: float = 0.4
    trailing_silence_seconds: float = 0.9
    post_roll_seconds: float = 0.25
    max_utterance_seconds: float = 25.0
    gemma_url: str = "http://127.0.0.1:8082"
    max_tokens: int = 256
    poll_seconds: float = 0.5
    start_wait_seconds: float = 30.0
    manage_host_sinks: bool = True
    startup_message_path: str | None = None
    ready_message_path: str | None = None
    readiness_path: str | None = None
    readiness_wait_seconds: float = 900.0

    @classmethod
    def from_settings(cls) -> "CallAudioConfig":
        backend = load_audio_settings()["backend"]
        startup_message = None
        ready_message = None
        readiness_path = None
        if os.environ.get("HERMES_INBOUND_CALL") == "1":
            startup_message = os.environ.get(
                "HERMES_INBOUND_PREPARING_MESSAGE",
                str(Path(__file__).with_name("assets") / "inbound-preparing.wav"),
            )
            ready_message = os.environ.get(
                "HERMES_INBOUND_READY_MESSAGE",
                str(Path(__file__).with_name("assets") / "inbound-ready.wav"),
            )
            readiness_path = os.environ.get(
                "HERMES_INBOUND_READINESS_PATH",
                str(Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "inbound-call-readiness.json"),
            )
        if backend == "bluetooth":
            return cls(audio_backend=backend, sample_rate=16000, channels=1, startup_message_path=startup_message, ready_message_path=ready_message, readiness_path=readiness_path)
        return cls(audio_backend=backend, startup_message_path=startup_message, ready_message_path=ready_message, readiness_path=readiness_path)


class UtteranceEndpointDetector:
    """Accumulate one utterance from PCM frames without fixed turn chunks."""

    def __init__(
        self,
        *,
        sample_rate: int,
        pre_roll_seconds: float = 0.4,
        speech_start_threshold: float = 0.003,
        trailing_silence_seconds: float = 0.9,
        post_roll_seconds: float = 0.25,
        max_utterance_seconds: float = 25.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.bytes_per_second = sample_rate * 2
        self.pre_roll_bytes = int(pre_roll_seconds * self.bytes_per_second)
        self.speech_start_threshold = speech_start_threshold
        self.trailing_silence_bytes = int(trailing_silence_seconds * self.bytes_per_second)
        self.post_roll_bytes = int(post_roll_seconds * self.bytes_per_second)
        self.max_utterance_bytes = int(max_utterance_seconds * self.bytes_per_second)
        self._pre_roll: deque[bytes] = deque()
        self._pre_roll_size = 0
        self._utterance = bytearray()
        self._silence_bytes = 0
        self._post_roll_remaining = 0
        self._noise_floor = 0.0
        self._input_bytes = 0
        self.speech_started = False
        self.complete = False
        self.speech_start_offset: float | None = None
        self.segment_start_offset: float | None = None
        self.endpoint_offset: float | None = None
        self.endpoint_reason: str | None = None

    @staticmethod
    def _rms(data: bytes) -> float:
        if not data:
            return 0.0
        samples = memoryview(data).cast("h")
        return (sum(sample * sample for sample in samples) / len(samples)) ** 0.5 / 32768.0

    def _append_pre_roll(self, data: bytes) -> None:
        self._pre_roll.append(data)
        self._pre_roll_size += len(data)
        while self._pre_roll_size > self.pre_roll_bytes and self._pre_roll:
            self._pre_roll_size -= len(self._pre_roll.popleft())

    def _finish(self, reason: str) -> bytes:
        self.complete = True
        self.endpoint_offset = self._input_bytes / self.bytes_per_second
        self.endpoint_reason = reason
        return bytes(self._utterance)

    def feed(self, data: bytes) -> bytes | None:
        if self.complete or not data:
            return None
        stream_offset = self._input_bytes
        self._input_bytes += len(data)
        energy = self._rms(data)
        threshold = max(self.speech_start_threshold, self._noise_floor * 2.0)
        if not self.speech_started:
            self._append_pre_roll(data)
            if energy < threshold:
                self._noise_floor = energy if not self._noise_floor else (self._noise_floor * 0.95 + energy * 0.05)
            else:
                self._noise_floor = min(self._noise_floor, energy) if self._noise_floor else 0.0
            if energy < threshold:
                return None
            self.speech_started = True
            self.speech_start_offset = stream_offset / self.bytes_per_second
            self.segment_start_offset = max(
                0.0, (stream_offset - self._pre_roll_size) / self.bytes_per_second
            )
            for frame in self._pre_roll:
                self._utterance.extend(frame)
            self._pre_roll.clear()
            self._pre_roll_size = 0
            self._utterance.extend(data)
            return self._finish("max_utterance") if len(self._utterance) >= self.max_utterance_bytes else None

        self._utterance.extend(data)
        if len(self._utterance) >= self.max_utterance_bytes:
            return self._finish("max_utterance")
        if energy >= threshold:
            self._silence_bytes = 0
            self._post_roll_remaining = 0
            return None
        self._silence_bytes += len(data)
        if self._post_roll_remaining == 0 and self._silence_bytes >= self.trailing_silence_bytes:
            self._post_roll_remaining = self.post_roll_bytes
        if self._post_roll_remaining:
            self._post_roll_remaining -= len(data)
            if self._post_roll_remaining <= 0:
                return self._finish("trailing_silence")
        return None


class CallAudioCoordinator:
    def __init__(
        self,
        config: CallAudioConfig,
        *,
        phone_state: Callable[[], str] | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        voice_session_factory: Callable[..., Any] | None = None,
        acceptance_gate: Any | None = None,
    ) -> None:
        self.config = config
        self._run = run
        self.phone_state = phone_state or self._default_phone_state
        self._voice_session_factory = voice_session_factory
        self._acceptance_gate = acceptance_gate
        self._voice_session: Any | None = None
        self._bluetooth_input: str | None = None
        self._bluetooth_output: str | None = None
        self._muted_host_sinks: list[str] = []
        self._last_known_phone_state: str | None = None

    def capture_command(self, output_path: str) -> list[str]:
        if self.config.audio_backend == "bluetooth":
            device = self._bluetooth_device("sources", "bluez_input")
            return [
                "pw-record", "--target", device,
                "--rate", str(self.config.sample_rate),
                "--channels", str(self.config.channels),
                "--format", "s16",
                "--raw", "-",
            ]
        return [
            "arecord", "-D", self.config.capture_device, "-f", "S16_LE",
            "-r", str(self.config.sample_rate), "-c", str(self.config.channels),
            "-d", str(self.config.turn_seconds), output_path,
        ]

    def playback_command(self, input_path: str) -> list[str]:
        if self.config.audio_backend == "bluetooth":
            device = self._bluetooth_device("sinks", "bluez_output")
            return ["pw-play", "--target", device, input_path]
        return ["aplay", "-D", self.config.playback_device, input_path]

    def _configure_analog_input(self) -> None:
        """Route both motherboard ADCs to the splitter's Line-In jack."""
        result = self._run(
            ["amixer", "-c", "0", "sget", "Input Source"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "could not inspect analog input routing")
        count = sum(1 for line in result.stdout.splitlines() if "Item0:" in line and "'" in line)
        if not count:
            raise RuntimeError("analog Input Source mixer controls are unavailable")
        for index in range(count):
            result = self._run(
                ["amixer", "-c", "0", "sset", f"Input Source,{index}", "Line"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"could not route analog input {index} to Line")
        LOG.info("ANALOG_INPUT_ROUTED source=Line controls=%d", count)

    def _bluetooth_device(self, kind: str, prefix: str) -> str:
        cached = self._bluetooth_input if prefix == "bluez_input" else self._bluetooth_output
        if cached:
            return cached
        result = self._run(["pw-dump"], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "pw-dump could not inspect PipeWire nodes")
        try:
            objects = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"pw-dump returned invalid JSON: {exc}") from exc
        if not isinstance(objects, list):
            raise RuntimeError("pw-dump returned an unexpected object list")
        names = [
            obj.get("info", {}).get("props", {}).get("node.name")
            for obj in objects
            if obj.get("type") == "PipeWire:Interface:Node"
        ]
        matches = [name for name in names if isinstance(name, str) and name.startswith(prefix)]
        if not matches:
            raise RuntimeError(f"Bluetooth call {kind[:-1]} is unavailable; connect the phone with its Calls profile enabled")
        return matches[0]

    def wait_for_bluetooth_nodes(self) -> tuple[str, str]:
        deadline = time.monotonic() + self.config.start_wait_seconds
        last_error = "Bluetooth call nodes are unavailable"
        while time.monotonic() < deadline:
            try:
                input_node = self._bluetooth_device("sources", "bluez_input")
                output_node = self._bluetooth_device("sinks", "bluez_output")
                self._bluetooth_input = input_node
                self._bluetooth_output = output_node
                LOG.info("HFP_INPUT_READY node=%s", input_node)
                LOG.info("HFP_OUTPUT_READY node=%s", output_node)
                return input_node, output_node
            except RuntimeError as exc:
                last_error = str(exc)
                self._bluetooth_input = None
                self._bluetooth_output = None
                time.sleep(self.config.poll_seconds)
        LOG.error(
            "HFP_ROUTE_TIMEOUT wait_seconds=%.1f reason=%s",
            self.config.start_wait_seconds,
            last_error,
        )
        raise RuntimeError(last_error)

    def isolate_bluetooth_output(self, output_node: str | None = None) -> None:
        output_node = output_node or self._bluetooth_output
        if not output_node:
            raise RuntimeError("Bluetooth output node is not resolved")
        result = self._run(["pw-link", "-lI"], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "could not inspect PipeWire links")
        for line in result.stdout.splitlines():
            match = re.match(r"^\s*(\d+)\s+\|->\s+\d+\s+(\S+)$", line)
            if match and output_node in match.group(2):
                link_id = match.group(1)
                disconnected = self._run(
                    ["pw-link", "-d", link_id],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                if disconnected.returncode != 0:
                    raise RuntimeError(disconnected.stderr.strip() or f"could not disconnect PipeWire link {link_id}")
                LOG.info("HFP_OUTPUT_ROUTE_REMOVED link=%s target=%s", link_id, output_node)

    def isolate_bluetooth_input(self, input_node: str | None = None) -> None:
        """Mute physical host sinks without tearing down the HFP input stream."""
        result = self._run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "could not inspect host sinks")
        for line in result.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) < 2 or fields[1].startswith("bluez"):
                continue
            sink = fields[1]
            muted = self._run(
                ["pactl", "set-sink-mute", sink, "1"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if muted.returncode != 0:
                raise RuntimeError(muted.stderr.strip() or f"could not mute host sink {sink}")
            self._muted_host_sinks.append(sink)
            LOG.info("HFP_HOST_SINK_MUTED sink=%s", sink)

    def restore_host_sinks(self) -> None:
        for sink in self._muted_host_sinks:
            self._run(["pactl", "set-sink-mute", sink, "0"], capture_output=True, text=True, timeout=10, check=False)
        self._muted_host_sinks.clear()

    def _default_phone_state(self) -> str:
        adb = os.environ.get("HERMES_PHONE_ADB") or shutil.which("adb") or "adb"
        serial = os.environ.get("HERMES_PHONE_ADB_SERIAL")
        argv = [adb] + (["-s", serial] if serial else []) + [
            "shell", "printf '%s\\n' CALL_STATUS | nc -w 3 127.0.0.1 9999",
        ]
        result = self._run(argv, capture_output=True, text=True, timeout=8, check=False)
        if result.returncode != 0:
            return "UNKNOWN"
        raw = result.stdout.strip()
        if raw.startswith("OK "):
            try:
                return str(json.loads(raw[3:]).get("state", "UNKNOWN"))
            except json.JSONDecodeError:
                pass
        return "UNKNOWN"

    def should_continue(self) -> bool:
        state = self.phone_state()
        if state in {"DIALING", "RINGING", "OFFHOOK"}:
            self._last_known_phone_state = state
            return True
        if state == "IDLE":
            self._last_known_phone_state = state
            return False
        # A transient bridge/ADB read failure must not tear down an active
        # call.  Only an authoritative IDLE response ends the session.
        return self._last_known_phone_state in {"DIALING", "RINGING", "OFFHOOK"}

    def _hangup_call(self) -> None:
        """End the explicitly supervised acceptance call; never redial."""
        adb = os.environ.get("HERMES_PHONE_ADB") or shutil.which("adb") or "adb"
        serial = os.environ.get("HERMES_PHONE_ADB_SERIAL")
        command = [adb] + (["-s", serial] if serial else []) + [
            "shell", "printf '%s\\n' HANGUP | nc -w 15 127.0.0.1 9999",
        ]
        result = self._run(command, capture_output=True, text=True, timeout=20, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "acceptance hangup failed")
        LOG.info("ACCEPTANCE_HANGUP_REQUESTED")

    def _capture(self, output_path: str) -> None:
        result = self._run(
            self.capture_command(output_path),
            capture_output=True, text=True,
            timeout=self.config.turn_seconds + 8, check=False,
        )
        if result.returncode != 0:
            if self.config.audio_backend == "bluetooth":
                try:
                    with wave.open(output_path, "rb") as recording:
                        valid_capture = (
                            recording.getframerate() == self.config.sample_rate
                            and recording.getnchannels() == self.config.channels
                            and recording.getsampwidth() == 2
                            and recording.getnframes() >= self.config.turn_seconds * self.config.sample_rate
                        )
                except (OSError, wave.Error):
                    valid_capture = False
                if valid_capture:
                    LOG.warning(
                        "PW_RECORD_NONZERO_WITH_VALID_WAV exit=%d stderr=%s",
                        result.returncode, result.stderr.strip(),
                    )
                    return
            raise RuntimeError(result.stderr.strip() or f"capture exited {result.returncode}")

    def _capture_utterance(self, output_path: str) -> bool:
        """Capture one complete Bluetooth utterance from a continuous PCM stream."""
        command = self.capture_command(output_path)
        capture_started = datetime.now(timezone.utc)
        capture_started_mono = time.monotonic()
        process = subprocess.Popen(
            # pw-record inherits PIPEWIRE_DEBUG from the supervised relay. Its
            # diagnostics are not part of the PCM stream; piping stderr without
            # draining it can fill the pipe and block pw-record before stdout
            # delivers audio.
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=False, bufsize=0,
        )
        LOG.info("PW_RECORD_STARTED target=%s", self._bluetooth_input)
        assert process.stdout is not None
        detector = UtteranceEndpointDetector(
            sample_rate=self.config.sample_rate,
            pre_roll_seconds=self.config.pre_roll_seconds,
            speech_start_threshold=self.config.silence_rms,
            trailing_silence_seconds=self.config.trailing_silence_seconds,
            post_roll_seconds=self.config.post_roll_seconds,
            max_utterance_seconds=self.config.max_utterance_seconds,
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        utterance: bytes | None = None
        capture_bytes = 0
        try:
            while utterance is None:
                ready = selector.select(timeout=0.1)
                if not ready:
                    if self.phone_state() == "IDLE":
                        return False
                    continue
                chunk = process.stdout.read(self.config.sample_rate // 25 * 2)
                if not chunk:
                    break
                capture_bytes += len(chunk)
                was_started = detector.speech_started
                utterance = detector.feed(chunk)
                if detector.speech_started and not was_started:
                    LOG.info(
                        "VAD_SPEECH_START offset=%.3f segment_start=%.3f",
                        detector.speech_start_offset or 0.0,
                        detector.segment_start_offset or 0.0,
                    )
            if utterance is None:
                raise RuntimeError("Bluetooth capture ended before an utterance endpoint")
            with wave.open(output_path, "wb") as recording:
                recording.setnchannels(self.config.channels)
                recording.setsampwidth(2)
                recording.setframerate(self.config.sample_rate)
                recording.writeframes(utterance)
            metrics = self._audio_metrics(output_path)
            self._last_capture_evidence = {
                "schema": "rex-stt-boundary-v1",
                "wav_path": str(Path(output_path).resolve()),
                "capture_started_at": capture_started.isoformat(),
                "capture_started_monotonic": capture_started_mono,
                "capture_ended_at": self._timestamp_at(capture_started, detector.endpoint_offset),
                "segment_start_at": self._timestamp_at(capture_started, detector.segment_start_offset),
                "speech_start_at": self._timestamp_at(capture_started, detector.speech_start_offset),
                "endpoint_at": self._timestamp_at(capture_started, detector.endpoint_offset),
                "segment_start_offset_seconds": detector.segment_start_offset,
                "speech_start_offset_seconds": detector.speech_start_offset,
                "endpoint_offset_seconds": detector.endpoint_offset,
                "endpoint_reason": detector.endpoint_reason,
                "vad": {
                    "speech_start": detector.speech_start_offset is not None,
                    "speech_start_offset_seconds": detector.speech_start_offset,
                    "endpoint": detector.endpoint_offset is not None,
                    "endpoint_reason": detector.endpoint_reason,
                },
                **metrics,
            }
            LOG.info(
                "VAD_ENDPOINT offset=%.3f reason=%s duration=%.3f rms=%.6f peak=%.6f",
                detector.endpoint_offset or 0.0,
                detector.endpoint_reason,
                metrics["duration_seconds"],
                metrics["rms"],
                metrics["peak"],
            )
            return True
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            if process.stderr is not None:
                process.stderr.close()
            process.stdout.close()
            LOG.info("PW_RECORD_STOPPED returncode=%s bytes=%d", process.returncode, capture_bytes)

    def _rms(self, path: str) -> float:
        with wave.open(path, "rb") as source:
            data = source.readframes(source.getnframes())
            width = source.getsampwidth()
        if width != 2 or not data:
            return 0.0
        samples = memoryview(data).cast("h")
        return (sum(sample * sample for sample in samples) / len(samples)) ** 0.5 / 32768.0

    def _audio_metrics(self, path: str) -> dict[str, Any]:
        with wave.open(path, "rb") as source:
            frames = source.getnframes()
            rate = source.getframerate()
            channels = source.getnchannels()
            width = source.getsampwidth()
            data = source.readframes(frames)
        if width != 2 or not data:
            return {
                "sample_rate": rate,
                "channels": channels,
                "sample_width_bytes": width,
                "frames": frames,
                "duration_seconds": frames / rate if rate else 0.0,
                "rms": 0.0,
                "peak": 0.0,
            }
        samples = memoryview(data).cast("h")
        return {
            "sample_rate": rate,
            "channels": channels,
            "sample_width_bytes": width,
            "frames": frames,
            "duration_seconds": frames / rate if rate else 0.0,
            "rms": (sum(sample * sample for sample in samples) / len(samples)) ** 0.5 / 32768.0,
            "peak": max(abs(sample) for sample in samples) / 32768.0,
        }

    @staticmethod
    def _timestamp_at(start: datetime, offset: float | None) -> str | None:
        if offset is None:
            return None
        return (start + timedelta(seconds=offset)).isoformat()

    def _write_stt_evidence(self, evidence: dict[str, Any], directory: str) -> None:
        path = Path(directory) / "stt-boundary.jsonl"
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(evidence, ensure_ascii=False, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())

    @contextmanager
    def _artifact_directory(self):
        configured = os.environ.get("HERMES_PHONE_RETAIN_AUDIO_DIR", "").strip()
        if not configured:
            with tempfile.TemporaryDirectory(prefix="hermes-phone-call-") as directory:
                yield directory
            return
        root = Path(configured)
        root.mkdir(parents=True, exist_ok=True)
        call_id = f"call-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}"
        directory = root / call_id
        directory.mkdir()
        LOG.info("STT_ARTIFACT_ROOT path=%s retained=true", directory)
        yield str(directory)

    def _transcribe(self, path: str) -> str:
        from tools.voice_mode import transcribe_recording
        result = transcribe_recording(path)
        if not result.get("success"):
            raise RuntimeError(result.get("error", "STT failed"))
        return str(result.get("transcript", "")).strip()

    def _ensure_voice_session(self) -> None:
        if self._voice_session is None:
            if self._voice_session_factory is None:
                from rex_voice_v1.launch import RexVoiceSession
                self._voice_session_factory = lambda **kwargs: RexVoiceSession(**kwargs)
            try:
                self._voice_session = self._voice_session_factory(acceptance_gate=self._acceptance_gate)
            except TypeError:
                # Preserve the small injectable factory used by focused tests.
                self._voice_session = self._voice_session_factory()
            self._voice_session.start(
                observer=lambda event: LOG.info("REX_VOICE %s", event)
            )

    def _ask_hermes(self, transcript: str) -> str:
        """Run a phone turn through the isolated Rex Voice Pi runtime."""
        self._ensure_voice_session()
        backend = getattr(self._voice_session, "backend", None)
        if backend is not None and self._acceptance_gate is not None:
            evidence_ref = None
            if self._acceptance_gate.turns:
                evidence_ref = self._acceptance_gate.turns[-1].evidence_ref
            backend.set_acceptance_turn(
                transcript=transcript,
                model_facing_request=transcript,
                evidence_ref=evidence_ref,
            )
        reply = self._voice_session.prompt(transcript)
        if not isinstance(reply, str) or not reply.strip():
            raise RuntimeError("Rex Voice returned empty assistant text")
        return reply.strip()

    def _stop_voice_session(self, *, aborted: bool) -> tuple[Any, dict[str, Any] | None] | None:
        if self._voice_session is None:
            return None
        session = self._voice_session
        self._voice_session = None
        try:
            LOG.info("VOICE_MODEL_SHUTDOWN_BEGIN session_id=%s", getattr(session, "session_id", "unknown"))
            job = session.stop(aborted=aborted, launch_post_call=False)
            LOG.info("VOICE_MODEL_SHUTDOWN_COMPLETE session_id=%s", getattr(session, "session_id", "unknown"))
            return session, job
        except Exception:
            LOG.exception("REX_VOICE_STOP_FAILED")
            return session, None

    def _restore_normal_model_for_post_call(self) -> tuple[str, str] | None:
        """Restore and independently verify the model allowed to run work."""
        supervisor = Path(os.environ.get(
            "REX_VOICE_SUPERVISOR",
            os.environ.get("REX_VOICE_SUPERVISOR", ""),
        ))
        if not supervisor.is_file():
            LOG.error("POST_CALL_WORKER_START_BLOCKED job_id=unknown reason=supervisor_missing")
            return None
        LOG.info("NORMAL_MODEL_RESTORE_BEGIN")
        result = self._run(
            ["bash", str(supervisor), "switch-to-qwen"],
            capture_output=True, text=True, timeout=900, check=False,
        )
        if result.returncode != 0:
            LOG.error("NORMAL_MODEL_RESTORE_FAILED exit=%d", result.returncode)
            return None
        status = self._run(
            ["bash", str(supervisor), "status"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        try:
            payload = json.loads(status.stdout)
        except json.JSONDecodeError:
            LOG.error("NORMAL_MODEL_RESTORE_FAILED reason=invalid_supervisor_status")
            return None
        state = payload.get("state_file", {})
        qwen = payload.get("qwen", {})
        gemma = payload.get("gemma", {})
        ready = (
            status.returncode == 0
            and state.get("state") == "QWEN_READY"
            and state.get("active_model") == "qwen"
            and payload.get("lock") == "free"
            and qwen.get("container") == "running"
            and qwen.get("health") is True
            and qwen.get("model")
            and gemma.get("health") is False
        )
        if not ready:
            LOG.error(
                "NORMAL_MODEL_RESTORE_FAILED reason=readiness_not_verified state=%s active_model=%s qwen_health=%s lock=%s",
                state.get("state"), state.get("active_model"), qwen.get("health"), payload.get("lock"),
            )
            return None
        model = os.environ.get("REX_POST_CALL_MODEL") or str(qwen["model"])
        provider = os.environ.get("REX_POST_CALL_PROVIDER", "Qwen 27B")
        if model != qwen["model"]:
            LOG.error("NORMAL_MODEL_RESTORE_FAILED reason=model_mismatch configured=%s ready=%s", model, qwen["model"])
            return None
        LOG.info("NORMAL_MODEL_RESTORE_READY model=%s provider=%s", model, provider)
        return model, provider

    def _mark_post_call_blocked(self, session: Any, job: dict[str, Any], reason: str) -> None:
        try:
            queue = session.backend.store.root / "post-call"
            from rex_voice_v1.post_call import PostCallQueue

            post_call = PostCallQueue(queue)
            current = post_call.load(job["session_id"])
            current["execution_gate"] = {"status": "blocked", "reason": reason, "updated_at": time.time()}
            post_call.save(current)
        except Exception:
            LOG.exception("POST_CALL_BLOCKED_STATE_WRITE_FAILED job_id=%s", job.get("job_id"))
        LOG.error("POST_CALL_WORKER_START_BLOCKED job_id=%s reason=%s", job.get("job_id"), reason)

    def _release_deferred_post_call(self, deferred: tuple[Any, dict[str, Any] | None] | None, final_state: str) -> None:
        """Release queued work only after an authoritative IDLE and Qwen readiness."""
        if deferred is None:
            return
        session, job = deferred
        if job is None:
            return
        if final_state != "IDLE":
            self._mark_post_call_blocked(session, job, "call_not_idle")
            return
        restored = self._restore_normal_model_for_post_call()
        if restored is None:
            self._mark_post_call_blocked(session, job, "normal_model_not_ready")
            return
        model, provider = restored
        session.launch_post_call_worker(job, model=model, provider=provider)

    def _synthesize(self, text: str, output_path: str) -> str:
        from tools.tts_tool import text_to_speech_tool
        raw = text_to_speech_tool(text=text, output_path=output_path)
        result_path = output_path
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
                result_path = str(payload.get("file_path", output_path))
            except json.JSONDecodeError:
                pass
        if not Path(result_path).is_file() or Path(result_path).stat().st_size == 0:
            raise RuntimeError("TTS produced no audio file")
        return result_path

    def _to_wav(self, input_path: str, directory: str) -> str:
        if input_path.lower().endswith(".wav"):
            return input_path
        output_path = str(Path(directory) / "reply.wav")
        result = self._run([
            "ffmpeg", "-loglevel", "error", "-y", "-i", input_path,
            "-ar", str(self.config.sample_rate), "-ac", str(self.config.channels),
            "-sample_fmt", "s16", output_path,
        ], capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "ffmpeg conversion failed")
        return output_path

    def _play(self, path: str) -> None:
        playback_path = path
        temporary_path: str | None = None
        try:
            with wave.open(path, "rb") as source:
                matches = (
                    source.getframerate() == self.config.sample_rate
                    and source.getnchannels() == self.config.channels
                    and source.getsampwidth() == 2
                )
            if not matches:
                fd, temporary_path = tempfile.mkstemp(prefix="rex-playback-", suffix=".wav")
                os.close(fd)
                result = self._run(
                    ["ffmpeg", "-loglevel", "error", "-y", "-i", path,
                     "-ar", str(self.config.sample_rate), "-ac", str(self.config.channels),
                     "-sample_fmt", "s16", temporary_path],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip() or "startup audio conversion failed")
                playback_path = temporary_path
            path = playback_path
            if self.config.audio_backend == "bluetooth":
                self.isolate_bluetooth_output()
                LOG.info("PLAYING target=%s file=%s", self._bluetooth_output, path)
            result = self._run(
                self.playback_command(path),
                capture_output=True, text=True, timeout=120, check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"playback exited {result.returncode}")
            LOG.info("PLAYBACK_COMPLETE target=%s", self._bluetooth_output or self.config.playback_device)
        finally:
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)

    def _play_startup_message(self) -> None:
        path = self.config.startup_message_path
        if not path:
            return
        if not Path(path).is_file():
            raise RuntimeError(f"inbound startup message is missing: {path}")
        LOG.info("INBOUND_GREETING_START file=%s", path)
        self._play(path)
        LOG.info("INBOUND_GREETING_COMPLETE file=%s", path)

    def _play_ready_message(self) -> None:
        path = self.config.ready_message_path
        if not path:
            return
        if not Path(path).is_file():
            raise RuntimeError(f"inbound ready message is missing: {path}")
        LOG.info("INBOUND_READY_MESSAGE_START file=%s", path)
        self._play(path)
        LOG.info("INBOUND_READY_MESSAGE_COMPLETE file=%s", path)

    def _wait_for_inbound_readiness(self) -> None:
        path = self.config.readiness_path
        if not path:
            return
        deadline = time.monotonic() + self.config.readiness_wait_seconds
        while time.monotonic() < deadline:
            if self.phone_state() == "IDLE":
                raise RuntimeError("call ended while waiting for inbound voice readiness")
            try:
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                time.sleep(self.config.poll_seconds)
                continue
            status = payload.get("status") if isinstance(payload, dict) else None
            if status == "ready":
                LOG.info("INBOUND_VOICE_READY_CONFIRMED path=%s", path)
                return
            if status == "failed":
                raise RuntimeError(str(payload.get("error") or "inbound voice preparation failed"))
            time.sleep(self.config.poll_seconds)
        raise RuntimeError("timed out waiting for inbound voice readiness")

    def run_session(self, *, max_turns: int | None = None) -> int:
        turns = 0
        LOG.info("LISTENING capture_device=%s playback_device=%s", self.config.capture_device, self.config.playback_device)
        startup_deadline = time.monotonic() + self.config.start_wait_seconds
        while not self.should_continue() and time.monotonic() < startup_deadline:
            time.sleep(self.config.poll_seconds)
        if not self.should_continue():
            LOG.info("CALL_AUDIO_NO_ACTIVE_CALL")
            LOG.info("CALL_AUDIO_STOP_REASON reason=no_active_call")
            return 0
        if self.config.audio_backend == "analog":
            self._configure_analog_input()
        elif self.config.audio_backend == "bluetooth":
            self.wait_for_bluetooth_nodes()
            if self.config.manage_host_sinks:
                self.isolate_bluetooth_input()
            self.isolate_bluetooth_output()
        LOG.info("CALL_AUDIO_ACTIVE state=%s", self.phone_state())
        self._play_startup_message()
        if self.config.ready_message_path:
            self._wait_for_inbound_readiness()
            self._ensure_voice_session()
            self._play_ready_message()
        if self._acceptance_gate is not None:
            self._acceptance_gate.start_audio_active()
            LOG.info("ACCEPTANCE_GATE status=%s", self._acceptance_gate.status.value)
        failed = False
        stop_reason: str | None = None
        try:
            with self._artifact_directory() as directory:
                while True:
                    if not self.should_continue():
                        stop_reason = "authoritative_idle"
                        break
                    if max_turns is not None and turns >= max_turns:
                        stop_reason = "max_turns"
                        break
                    if self._acceptance_gate is not None:
                        gate_result = self._acceptance_gate.check_timeout()
                        LOG.info("ACCEPTANCE_GATE status=%s", gate_result.status.value)
                        if gate_result.status.value == "target-utterance-not-observed":
                            stop_reason = "acceptance_timeout"
                            break
                    capture_path = str(Path(directory) / f"turn-{turns}.wav")
                    self._last_capture_evidence = None
                    LOG.info("LISTENING turn=%d", turns + 1)
                    if self.config.audio_backend == "bluetooth":
                        if not self._capture_utterance(capture_path):
                            stop_reason = (
                                "authoritative_idle"
                                if self.phone_state() == "IDLE"
                                else "capture_ended"
                            )
                            break
                    else:
                        self._capture(capture_path)
                    evidence = getattr(self, "_last_capture_evidence", None)
                    rms = float(evidence["rms"]) if evidence else self._rms(capture_path)
                    LOG.info("CAPTURED turn=%d wav=%s rms=%.6f", turns + 1, capture_path, rms)
                    if rms < self.config.silence_rms:
                        time.sleep(self.config.poll_seconds)
                        continue
                    LOG.info("TRANSCRIBING turn=%d", turns + 1)
                    transcript = self._transcribe(capture_path)
                    if evidence is None:
                        evidence = {
                            "schema": "rex-stt-boundary-v1",
                            "wav_path": str(Path(capture_path).resolve()),
                            **self._audio_metrics(capture_path),
                        }
                    evidence["turn"] = turns + 1
                    evidence["transcript"] = transcript
                    evidence["model_facing_user_request"] = transcript
                    self._write_stt_evidence(evidence, directory)
                    LOG.info(
                        "MODEL_FACING_USER_REQUEST turn=%d text=%r evidence=%s",
                        turns + 1,
                        transcript,
                        Path(directory) / "stt-boundary.jsonl",
                    )
                    if not transcript:
                        LOG.info("TRANSCRIBING_EMPTY turn=%d", turns + 1)
                        continue
                    LOG.info("TRANSCRIPT turn=%d text=%r", turns + 1, transcript)
                    marker = write_live_success_marker(
                        transcript=transcript,
                        wav_path=capture_path,
                        turn=turns + 1,
                        phone_state=self.phone_state(),
                        call_session_id=os.environ.get("HERMES_CALL_WORKER_SESSION_ID", ""),
                        rex_session_id=str(getattr(self._voice_session, "session_id", "")),
                    )
                    if marker is not None:
                        LOG.info(
                            "REX_VOICE_LIVE_SUCCESS marker=%s call_session_id=%s rex_session_id=%s turn=%d",
                            marker,
                            os.environ.get("HERMES_CALL_WORKER_SESSION_ID", ""),
                            getattr(self._voice_session, "session_id", ""),
                            turns + 1,
                        )
                        self._hangup_call()
                        stop_reason = "live_success_confirmation"
                        break
                    if self._acceptance_gate is not None:
                        gate_result = self._acceptance_gate.observe_turn(
                            transcript=transcript,
                            model_facing_request=transcript,
                            evidence_ref=str(Path(capture_path).resolve()),
                        )
                        LOG.info(
                            "ACCEPTANCE_GATE status=%s classification=%s turn=%d",
                            gate_result.status.value,
                            gate_result.classification,
                            turns + 1,
                        )
                        if gate_result.status.value == "target-utterance-not-observed":
                            stop_reason = "acceptance_timeout"
                            break
                    LOG.info("THINKING turn=%d", turns + 1)
                    reply = self._ask_hermes(transcript)
                    LOG.info("RESPONSE turn=%d text=%r", turns + 1, reply)
                    LOG.info("SYNTHESIZING turn=%d", turns + 1)
                    tts_path = self._synthesize(reply, str(Path(directory) / f"reply-{turns}.wav"))
                    LOG.info("TTS_READY turn=%d file=%s", turns + 1, tts_path)
                    self._play(self._to_wav(tts_path, directory))
                    LOG.info("SETTLING turn=%d", turns + 1)
                    turns += 1
        except Exception:
            failed = True
            stop_reason = "runtime_error"
            raise
        finally:
            deferred = self._stop_voice_session(aborted=failed)
            final_state = self.phone_state()
            if stop_reason is None:
                stop_reason = "runtime_error" if failed else "normal_loop_exit"
            LOG.info("CALL_AUDIO_STOP_REASON reason=%s", stop_reason)
            LOG.info("CALL_AUDIO_STOPPED turns=%d state=%s", turns, final_state)
            if final_state == "IDLE":
                if self.config.manage_host_sinks:
                    self.restore_host_sinks()
                self._release_deferred_post_call(deferred, final_state)
            else:
                if self.config.manage_host_sinks and self._muted_host_sinks:
                    LOG.warning("HFP_HOST_SINKS_LEFT_MUTED_CALL_ACTIVE state=%s", final_state)
                self._release_deferred_post_call(deferred, final_state)
        return turns


def main() -> int:
    def stop_cleanly(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop_cleanly)
    signal.signal(signal.SIGINT, stop_cleanly)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-backend", choices=("analog", "bluetooth"), default=None)
    parser.add_argument("--capture-device", default=os.environ.get("HERMES_CALL_CAPTURE", "hw:0,0"))
    parser.add_argument(
        "--playback-device",
        default=os.environ.get("HERMES_CALL_PLAYBACK", "hw:CARD=PCH,DEV=0"),
    )
    parser.add_argument("--turn-seconds", type=int, default=4)
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--session-sinks-managed", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s %(message)s")
    LOG.info(
        "CALL_RUNTIME_SESSION_ID session_id=%s",
        os.environ.get("HERMES_CALL_WORKER_SESSION_ID") or "unbound",
    )
    config = CallAudioConfig.from_settings() if args.audio_backend is None else CallAudioConfig(
        audio_backend=args.audio_backend,
        capture_device=args.capture_device,
        playback_device=args.playback_device,
        sample_rate=16000 if args.audio_backend == "bluetooth" else 48000,
        channels=1 if args.audio_backend == "bluetooth" else 2,
        turn_seconds=args.turn_seconds,
        manage_host_sinks=not args.session_sinks_managed,
    )
    worker_lock = None
    if not os.environ.get("HERMES_CALL_WORKER_SESSION_ID"):
        lock_path = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "call-audio-worker.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        worker_lock = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error("CALL_WORKER_START_REJECTED reason=active_call_worker_lock")
            worker_lock.close()
            return 2
    acceptance_gate = None
    if os.environ.get("REX_PHYSICAL_ACCEPTANCE_TEST", "").strip().lower() in {"1", "true", "yes"}:
        from physical_acceptance_gate import PhysicalAcceptanceTurnGate

        def acceptance_hangup() -> None:
            adb = os.environ.get("HERMES_PHONE_ADB") or shutil.which("adb") or "adb"
            serial = os.environ.get("HERMES_PHONE_ADB_SERIAL")
            command = [adb] + (["-s", serial] if serial else []) + [
                "shell", "printf '%s\\n' HANGUP | nc -w 15 127.0.0.1 9999",
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "acceptance hangup failed")
            LOG.info("ACCEPTANCE_HANGUP_REQUESTED")

        acceptance_gate = PhysicalAcceptanceTurnGate(
            target_phrase=os.environ.get("REX_PHYSICAL_ACCEPTANCE_TARGET", "Create a document called phone save test."),
            max_turns=int(os.environ.get("REX_PHYSICAL_ACCEPTANCE_MAX_TURNS", "12")),
            timeout_seconds=float(os.environ.get("REX_PHYSICAL_ACCEPTANCE_TIMEOUT_SECONDS", "120")),
            hangup=acceptance_hangup,
            log=lambda message: LOG.info(message),
        )
    try:
        CallAudioCoordinator(config, acceptance_gate=acceptance_gate).run_session(max_turns=args.max_turns)
    except Exception:
        LOG.exception("CALL_AUDIO_FAILED")
        return 1
    finally:
        if worker_lock is not None:
            worker_lock.seek(0)
            worker_lock.truncate()
            worker_lock.flush()
            fcntl.flock(worker_lock.fileno(), fcntl.LOCK_UN)
            worker_lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
