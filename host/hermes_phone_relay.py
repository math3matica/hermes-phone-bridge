#!/usr/bin/env python3
"""Owner-only SMS <-> Hermes relay for the RexBridge Android service.

The Android APK remains a deliberately small SMS transport. This host process
owns polling, authorization, durable queueing, Hermes session continuity, and
reply delivery.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger("hermes_phone_relay")
DEFAULT_OWNER = "+141****6998"
DEFAULT_PORT = 9999
DEFAULT_QWEN_IDLE_TIMEOUT = 1800.0
DEFAULT_CALL_UNKNOWN_GRACE = 12.0
DEFAULT_CALL_WORKER_LOCK = "call-audio-worker.lock"
DEFAULT_RELAY_LOCK = "hermes-phone-relay.lock"
ANSWER_VERIFY_TIMEOUT = 8.0
ANSWER_VERIFY_INTERVAL = 0.25
INTERNAL_SMS_LINE = re.compile(r"^\s*!?\s*(?:review diff|git diff|git status|code review)\b.*$", re.IGNORECASE)
_ACTIVE_CALL_LOCKS: set[Path] = set()


@dataclass(frozen=True)
class SmsMessage:
    address: str
    timestamp: int
    body: str

    @property
    def message_id(self) -> str:
        material = f"{self.address}\0{self.timestamp}\0{self.body}".encode()
        return hashlib.sha256(material).hexdigest()


def normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if value.strip().startswith("+") and digits:
        return "+" + digits
    raise ValueError(f"cannot normalize phone number: {value!r}")


def parse_inbox_response(raw: str) -> list[SmsMessage]:
    if not raw.startswith("OK "):
        raise ValueError(raw.strip() or "empty bridge response")
    try:
        payload = json.loads(raw[3:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid inbox JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("inbox response is not a list")
    messages: list[SmsMessage] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("inbox item is not an object")
        messages.append(
            SmsMessage(
                address=normalize_phone(str(item["address"])),
                timestamp=int(item["timestamp"]),
                body=str(item["body"]),
            )
        )
    return messages


def parse_audio_test_response(raw: str) -> dict[str, float]:
    if not raw.startswith("OK "):
        raise ValueError(raw.strip() or "empty audio-test response")
    payload = json.loads(raw[3:])
    if not isinstance(payload, dict) or not all(key in payload for key in ("rms", "frequency", "tone")):
        raise ValueError("audio-test response lacks measurements")
    return {key: float(payload[key]) for key in ("rms", "frequency", "tone")}


def extract_hermes_reply(output: str) -> str:
    lines = output.strip().splitlines()
    for index, line in enumerate(lines):
        if line.startswith("session_id:"):
            reply = "\n".join(lines[index + 1 :]).strip()
            if reply:
                return reply
    return output.strip()


def format_sms_reply(output: str, *, max_chars: int = 1200) -> str:
    """Keep only concise, user-facing text suitable for one SMS response."""
    lines = [line.strip() for line in output.splitlines() if not INTERNAL_SMS_LINE.match(line)]
    reply = " ".join(line for line in lines if line)
    if len(reply) <= max_chars:
        return reply
    return reply[: max_chars - 1].rstrip() + "…"


def _default_state_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    current = hermes_home / "hermes_phone_relay.json"
    legacy = hermes_home / "rex_sms_relay.json"
    return legacy if not current.exists() and legacy.exists() else current


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"session_id": None, "pending": []}
    with path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict) or not isinstance(state.get("pending", []), list):
        raise ValueError(f"invalid relay state: {path}")
    return state


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class RelayInstanceLock:
    """Own the relay process itself, independently of the call-worker lock."""

    def __init__(self, path: Path | None = None, *, log: Any = LOG.info) -> None:
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        self.path = path or Path(os.environ.get("HERMES_RELAY_LOCK_PATH", hermes_home / DEFAULT_RELAY_LOCK))
        self.log = log
        self._handle: Any | None = None

    def acquire(self) -> bool:
        pid = os.getpid()
        self.log(f"RELAY_INSTANCE_START pid={pid}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            self.log("RELAY_INSTANCE_START_REJECTED reason=active_relay_lock")
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={pid}\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        self.log("RELAY_INSTANCE_LOCK_ACQUIRED")
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self.log(f"RELAY_INSTANCE_END pid={os.getpid()}")


class CallSessionSupervisor:
    """Own exactly one audio worker for the lifetime of an active call."""

    def __init__(
        self,
        *,
        phone_state: Any,
        hfp_nodes: Any,
        worker_factory: Any,
        mute_sinks: Any,
        restore_sinks: Any,
        on_end: Any | None = None,
        on_dialing: Any | None = None,
        on_ringing: Any | None = None,
        on_idle: Any | None = None,
        log: Any = LOG.info,
        unknown_grace_seconds: float = DEFAULT_CALL_UNKNOWN_GRACE,
        clock: Any = time.monotonic,
        worker_lock_path: Path | None = None,
    ) -> None:
        self.phone_state = phone_state
        self.hfp_nodes = hfp_nodes
        self.worker_factory = worker_factory
        self.mute_sinks = mute_sinks
        self.restore_sinks = restore_sinks
        self.on_end = on_end
        self.on_dialing = on_dialing
        self.on_ringing = on_ringing
        self.on_idle = on_idle
        self.log = log
        self.unknown_grace_seconds = unknown_grace_seconds
        self.clock = clock
        self.worker: Any | None = None
        self.session_active = False
        self.runtime_started = False
        self.runtime_failed = False
        self.runtime_session_id: str | None = None
        self.unknown_since: float | None = None
        self.last_state: str | None = None
        self._sinks_restored = False
        self._model_restored = False
        self.worker_lock_path = worker_lock_path or (
            Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
            / DEFAULT_CALL_WORKER_LOCK
        )
        self._worker_lock: Any | None = None
        self.call_session_id: str | None = None
        self._start_rejected = False
        self._ringing_handled = False
        self._dialing_handled = False

    def _acquire_worker_lock(self) -> bool:
        if self.worker_lock_path in _ACTIVE_CALL_LOCKS:
            self.log("CALL_WORKER_START_REJECTED reason=active_call_worker_lock")
            return False
        self.worker_lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.worker_lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            self.log("CALL_WORKER_START_REJECTED reason=active_call_worker_lock")
            return False
        self.call_session_id = f"call-{uuid.uuid4().hex[:12]}"
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "pid": os.getpid(),
            "session_id": self.call_session_id,
            "state": "active",
        }, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._worker_lock = handle
        _ACTIVE_CALL_LOCKS.add(self.worker_lock_path)
        return True

    def _release_worker_lock(self) -> None:
        handle = self._worker_lock
        self._worker_lock = None
        self.call_session_id = None
        if handle is None:
            return
        try:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            _ACTIVE_CALL_LOCKS.discard(self.worker_lock_path)
            handle.close()

    def _end(self) -> None:
        if not self.session_active:
            return
        worker = self.worker
        if worker is not None and worker.poll() is None:
            self.log("CALL_WORKER_STOP_REQUESTED")
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except Exception:
                try:
                    worker.kill()
                    worker.wait(timeout=5)
                except Exception:
                    pass
        self.worker = None
        self.log("CALL_WORKER_EXITED")
        self._release_worker_lock()
        if not self._sinks_restored:
            try:
                self.restore_sinks()
            finally:
                self._sinks_restored = True
                self.log("HOST_SINKS_RESTORED")
        if self.on_end is not None and not self._model_restored:
            try:
                self.on_end()
            finally:
                self._model_restored = True
            self.log("MODEL_RESTORE_COMPLETE")
        if self.runtime_started:
            self.log(f"CALL_RUNTIME_END session_id={self.runtime_session_id}")
        self.log("CALL_SESSION_END")
        self.session_active = False
        self.runtime_started = False
        self.runtime_failed = False
        self.runtime_session_id = None
        self.unknown_since = None
        self.last_state = "IDLE"

    def _start(self) -> None:
        if self.runtime_started:
            self.log("CALL_RUNTIME_START_REJECTED reason=runtime_already_started")
            return
        if not self._acquire_worker_lock():
            self._start_rejected = True
            return
        try:
            self.mute_sinks()
            self.worker = self.worker_factory()
        except Exception:
            self._release_worker_lock()
            raise
        self.session_active = True
        self.runtime_started = True
        self.runtime_failed = False
        self.runtime_session_id = self.call_session_id
        self.unknown_since = None
        self._sinks_restored = False
        self._model_restored = False
        self._start_rejected = False
        self.log("CALL_SESSION_START")
        self.log(f"CALL_SESSION_ID session_id={self.call_session_id}")
        self.log(f"CALL_RUNTIME_START session_id={self.runtime_session_id}")

    def tick(self) -> None:
        try:
            state = str(self.phone_state())
        except Exception:
            state = "UNKNOWN"
        active = state in {"DIALING", "RINGING", "OFFHOOK"}
        if state == "IDLE":
            if self.session_active:
                self._end()
            elif self._ringing_handled and self.on_end is not None and not self._model_restored:
                # A caller may hang up while preparation or answer is still in
                # progress.  Restore the previous model even though no worker
                # session was created.
                try:
                    self.on_end()
                finally:
                    self._model_restored = True
                self.log("MODEL_RESTORE_COMPLETE")
            self._start_rejected = False
            self._ringing_handled = False
            self._dialing_handled = False
            if self.on_idle is not None:
                self.on_idle()
            return
        if active:
            self.last_state = state
            self.unknown_since = None
            if state == "DIALING" and not self._dialing_handled:
                self._dialing_handled = True
                if self.on_dialing is not None:
                    try:
                        accepted = self.on_dialing()
                        if accepted is False:
                            self._dialing_handled = False
                            self.log("OUTBOUND_CALL_PREPARATION_DEFERRED reason=active_process")
                    except Exception as exc:
                        self.log(f"OUTBOUND_CALL_PREPARE_FAILED error={exc}")
                        self._start_rejected = True
                        return
            if state == "RINGING" and not self._ringing_handled:
                self._ringing_handled = True
                if self.on_ringing is not None:
                    try:
                        accepted = self.on_ringing()
                        if accepted is False:
                            self._ringing_handled = False
                            self.log("INBOUND_CALL_DEFERRED reason=active_process")
                    except Exception as exc:
                        self.log(f"INBOUND_CALL_PREPARE_FAILED error={exc}")
                        self._start_rejected = True
                        return
            if state == "OFFHOOK" and not self.session_active and not self._start_rejected:
                self._start()
        elif state == "UNKNOWN":
            if self.unknown_since is None:
                self.unknown_since = self.clock()
            if not self.session_active:
                return
            if self.clock() - self.unknown_since <= self.unknown_grace_seconds:
                return
            # A persistent bridge failure is not enough to end a live call.
            # HFP's presence is the independent corroboration boundary.
            if self.hfp_nodes():
                self.log("CALL_STATE_UNKNOWN_HFP_PRESENT")
                return
            self.log("CALL_STATE_UNKNOWN_HFP_ABSENT_PRESERVED")
            return
        else:
            return

        if not self.session_active or self.worker is None:
            return
        if self.worker.poll() is None:
            return
        exit_code = self.worker.returncode
        self.log(f"CALL_WORKER_FAILED exit={exit_code}")
        self.worker = None
        self.runtime_failed = True
        self.log(
            f"CALL_RUNTIME_FAILED session_id={self.runtime_session_id} exit={exit_code}"
        )
        self.log("CALL_WORKER_RESTART_SUPPRESSED reason=runtime_failed")


class HermesPhoneRelay:
    def __init__(
        self,
        *,
        owner: str = DEFAULT_OWNER,
        adb: str | None = None,
        adb_serial: str | None = None,
        port: int = DEFAULT_PORT,
        state_path: Path | None = None,
        hermes_bin: str = "hermes",
        qwen_lifecycle: "QwenLifecycle | None" = None,
    ) -> None:
        self.owner = normalize_phone(owner)
        self.adb = adb or os.environ.get("HERMES_PHONE_ADB") or shutil.which("adb") or "adb"
        self.adb_serial = adb_serial or os.environ.get("HERMES_PHONE_ADB_SERIAL")
        self.port = port
        self.state_path = state_path or _default_state_path()
        self.hermes_bin = hermes_bin
        self.qwen_lifecycle = qwen_lifecycle
        self._inbound_call = False
        self._relay_operation_active = False
        self._last_inbound_number: str | None = None
        self._deferred_inbound_number: str | None = None
        self._inbound_prepare_process: subprocess.Popen[str] | None = None
        self._inbound_prepare_thread: threading.Thread | None = None
        self._inbound_readiness_path = Path(
            os.environ.get(
                "HERMES_INBOUND_READINESS_PATH",
                str(Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "inbound-call-readiness.json"),
            )
        )
        self.state = _load_state(self.state_path)

    def call_state(self) -> str:
        response = self._bridge("CALL_STATUS", timeout=5)
        if not response.startswith("OK "):
            return "UNKNOWN"
        try:
            payload = json.loads(response[3:])
        except json.JSONDecodeError:
            return "UNKNOWN"
        state = payload.get("state") if isinstance(payload, dict) else None
        return state if state in {"IDLE", "RINGING", "DIALING", "OFFHOOK"} else "UNKNOWN"

    def inbound_caller_is_authorized(self) -> bool:
        """Return true only for a known caller matching the configured owner."""
        try:
            response = self._bridge("CALL_STATUS", timeout=5)
            if not response.startswith("OK "):
                return False
            payload = json.loads(response[3:])
            if not isinstance(payload, dict) or payload.get("state") != "RINGING":
                return False
            caller = str(payload.get("number", "")).strip()
            if not caller:
                return False
            self._last_inbound_number = normalize_phone(caller)
            return self._last_inbound_number == self.owner
        except (RuntimeError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def _defer_missed_inbound_call(self) -> None:
        number = self._last_inbound_number
        if number == self.owner:
            self._deferred_inbound_number = number
            LOG.info("INBOUND_MISSED_CALL_REDIAL_PENDING number=%s", number)

    def _redial_deferred_call(self) -> None:
        number = self._deferred_inbound_number
        if not number or self._relay_operation_active or self._inbound_call:
            return
        command = [self.adb]
        if self.adb_serial:
            command.extend(["-s", self.adb_serial])
        command.extend([
            "shell", "am", "start", "-W", "-a", "android.intent.action.CALL",
            "-d", f"tel:{number}",
        ])
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        if result.returncode == 0:
            LOG.info("INBOUND_MISSED_CALL_REDIAL_REQUESTED number=%s", number)
            self._deferred_inbound_number = None
        else:
            LOG.error("INBOUND_MISSED_CALL_REDIAL_FAILED number=%s error=%s", number, result.stderr.strip())

    def hfp_nodes_available(self) -> bool:
        result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            return False
        try:
            objects = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        names = {
            obj.get("info", {}).get("props", {}).get("node.name")
            for obj in objects if isinstance(obj, dict) and obj.get("type") == "PipeWire:Interface:Node"
        }
        return any(isinstance(name, str) and name.startswith("bluez_input") for name in names) and any(
            isinstance(name, str) and name.startswith("bluez_output") for name in names
        )

    def mute_call_sinks(self) -> None:
        self._call_sink_states: dict[str, str] = {}
        result = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "could not inspect host sinks")
        for line in result.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) < 2 or fields[1].startswith("bluez"):
                continue
            sink = fields[1]
            mute = subprocess.run(["pactl", "get-sink-mute", sink], capture_output=True, text=True, timeout=10, check=False)
            if mute.returncode != 0:
                raise RuntimeError(mute.stderr.strip() or f"could not inspect mute state for {sink}")
            self._call_sink_states[sink] = "1" if "yes" in mute.stdout.lower() else "0"
            changed = subprocess.run(["pactl", "set-sink-mute", sink, "1"], capture_output=True, text=True, timeout=10, check=False)
            if changed.returncode != 0:
                raise RuntimeError(changed.stderr.strip() or f"could not mute {sink}")
            LOG.info("HFP_HOST_SINK_MUTED sink=%s", sink)

    def restore_call_sinks(self) -> None:
        for sink, muted in getattr(self, "_call_sink_states", {}).items():
            subprocess.run(["pactl", "set-sink-mute", sink, muted], capture_output=True, text=True, timeout=10, check=False)
            LOG.info("HFP_HOST_SINK_RESTORED sink=%s mute=%s", sink, muted)
        self._call_sink_states = {}

    def call_worker_factory(self, session_id: str | None = None) -> subprocess.Popen[str]:
        script = Path(__file__).with_name("call_audio_loop.py")
        interpreter = os.environ.get("HERMES_PHONE_PYTHON", "")
        if not interpreter:
            interpreter = "/home/math3matica/.hermes/hermes-agent/venv/bin/python"
            if not Path(interpreter).is_file():
                interpreter = shutil.which("python3") or sys.executable
        log_path = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "phone-call-audio.log"
        log = log_path.open("a", encoding="utf-8")
        environment = {
            **os.environ,
            "HERMES_ROOT": os.environ.get("HERMES_ROOT", "/home/math3matica/.hermes/hermes-agent"),
            "REX_VOICE_ROOT": os.environ.get("REX_VOICE_ROOT", "/home/math3matica/hermes"),
            "HERMES_HOME": os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")),
            "HERMES_REX_VOICE_V1": os.environ.get("HERMES_REX_VOICE_V1", "1"),
            "OBSIDIAN_VAULT_PATH": os.environ.get(
                "OBSIDIAN_VAULT_PATH", str(Path.home() / "Documents" / "Rex Vault")
            ),
            "HERMES_CALL_WORKER_SESSION_ID": session_id or "",
            "HERMES_INBOUND_READINESS_PATH": str(self._inbound_readiness_path),
        }
        if self._inbound_call:
            environment["HERMES_INBOUND_CALL"] = "1"
        try:
            return subprocess.Popen(
                [interpreter, str(script), "--session-sinks-managed"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, text=True,
                env=environment,
            )
        finally:
            log.close()

    def _write_inbound_readiness(self, status: str, *, error: str | None = None) -> None:
        self._inbound_readiness_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"status": status, "updated_at": time.time()}
        if error:
            payload["error"] = error
        temporary = self._inbound_readiness_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        temporary.replace(self._inbound_readiness_path)


    def _prepare_gemma_after_answer(self, supervisor: Path) -> None:
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                ["bash", str(supervisor), "switch-to-gemma"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._inbound_prepare_process = process
            stdout, stderr = process.communicate(timeout=900)
            if process.returncode != 0:
                message = stderr.strip() or stdout.strip() or "Gemma preparation failed"
                self._write_inbound_readiness("failed", error=message)
                LOG.error("INBOUND_VOICE_MODE_FAILED error=%s", message)
                return
            self._write_inbound_readiness("ready")
            LOG.info("INBOUND_VOICE_MODE_READY")
        except subprocess.TimeoutExpired:
            if process is not None:
                process.kill()
                process.communicate()
            self._write_inbound_readiness("failed", error="Gemma preparation timed out")
            LOG.error("INBOUND_VOICE_MODE_FAILED error=timeout")
        except Exception:
            self._write_inbound_readiness("failed", error="Gemma preparation crashed")
            LOG.exception("INBOUND_VOICE_MODE_FAILED")
        finally:
            self._inbound_prepare_process = None

    def restore_qwen_after_call(self) -> None:
        process = self._inbound_prepare_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        thread = self._inbound_prepare_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        supervisor = Path(os.environ.get(
            "REX_VOICE_SUPERVISOR",
            "/home/math3matica/hermes/test-llama/model_supervisor.sh",
        ))
        if not supervisor.is_file():
            self._inbound_call = False
            self._inbound_prepare_thread = None
            self._write_inbound_readiness("idle")
            return
        result = subprocess.run(["bash", str(supervisor), "switch-to-qwen"], capture_output=True, text=True, timeout=900, check=False)
        if result.returncode == 0:
            LOG.info("QWEN_RESTORED_AFTER_CALL")
        else:
            LOG.error("QWEN_RESTORE_FAILED_AFTER_CALL exit=%d reason=%s", result.returncode, result.stderr.strip())
        self._inbound_call = False
        self._inbound_prepare_thread = None
        self._write_inbound_readiness("idle")

    def _qwen_ready_for_post_call(self) -> tuple[str, str] | None:
        """Return the verified normal-model identity, or no release authority."""
        supervisor = Path(os.environ.get(
            "REX_VOICE_SUPERVISOR",
            "/home/math3matica/hermes/test-llama/model_supervisor.sh",
        ))
        if not supervisor.is_file():
            LOG.error("POST_CALL_RECOVERY_BLOCKED reason=supervisor_missing")
            return None
        result = subprocess.run(
            ["bash", str(supervisor), "status"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            LOG.error("POST_CALL_RECOVERY_BLOCKED reason=invalid_supervisor_status")
            return None
        state = payload.get("state_file", {})
        qwen = payload.get("qwen", {})
        gemma = payload.get("gemma", {})
        if not (
            result.returncode == 0
            and state.get("state") == "QWEN_READY"
            and state.get("active_model") == "qwen"
            and payload.get("lock") == "free"
            and qwen.get("container") == "running"
            and qwen.get("health") is True
            and isinstance(qwen.get("model"), str)
            and gemma.get("health") is False
        ):
            LOG.error("POST_CALL_RECOVERY_BLOCKED reason=qwen_not_ready")
            return None
        model = os.environ.get("REX_POST_CALL_WORK_MODEL") or os.environ.get("REX_POST_CALL_MODEL") or str(qwen["model"])
        if model != qwen["model"]:
            LOG.error("POST_CALL_RECOVERY_BLOCKED reason=model_mismatch")
            return None
        return model, os.environ.get("REX_POST_CALL_WORK_PROVIDER", os.environ.get("REX_POST_CALL_PROVIDER", "Qwen 27B"))

    def _recover_blocked_post_call_jobs(self) -> None:
        """Retry only jobs blocked because the call was still active.

        The call worker can observe OFFHOOK during shutdown and must fail
        closed. Once the relay independently observes IDLE, this callback is
        the recovery boundary. A per-job advisory lock prevents duplicate
        detached launches when idle is polled repeatedly or another relay
        path races this recovery.
        """
        artifacts = Path(os.environ.get(
            "REX_VOICE_ARTIFACTS",
            str(Path.home() / ".hermes/cache/rex-voice-v1"),
        )).expanduser()
        queue_root = artifacts / "post-call"
        if not (queue_root / "jobs").is_dir():
            return
        model_identity = self._qwen_ready_for_post_call()
        if model_identity is None:
            return
        model, provider = model_identity
        from rex_voice_v1.post_call import PostCallQueue
        from rex_voice_v1.post_call_worker import start_detached

        queue = PostCallQueue(queue_root)
        for job in queue.pending():
            if job.get("status") != "queued":
                continue
            gate = job.get("execution_gate", {})
            if gate.get("status") not in {"awaiting_normal_model", "blocked"}:
                continue
            if gate.get("status") == "blocked" and gate.get("reason") not in {"call_not_idle", "qwen_not_ready"}:
                continue
            session_id = str(job.get("session_id", ""))
            if not session_id:
                continue
            lock_path = queue_root / f"{session_id}.launch.lock"
            with lock_path.open("a+") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    LOG.info("POST_CALL_RECOVERY_SKIPPED session_id=%s reason=launch_in_progress", session_id)
                    continue
                current = queue.load(session_id)
                if current.get("status") != "queued":
                    continue
                current_gate = current.get("execution_gate", {})
                if current_gate.get("status") not in {"awaiting_normal_model", "blocked"}:
                    continue
                if current_gate.get("status") == "blocked" and current_gate.get("reason") not in {"call_not_idle", "qwen_not_ready"}:
                    continue
                environment = {
                    **os.environ,
                    "REX_POST_CALL_MODEL": model,
                    "REX_POST_CALL_PROVIDER": provider,
                }
                current["execution_gate"] = {
                    "status": "released",
                    "model": model,
                    "provider": provider,
                }
                queue.save(current)
                try:
                    worker = start_detached(queue_root, session_id, env=environment)
                except Exception:
                    current["execution_gate"] = {
                        "status": "awaiting_normal_model",
                        "reason": "post-call worker launch failed; retry after verified QWEN_READY",
                    }
                    queue.save(current)
                    LOG.exception("POST_CALL_RECOVERY_FAILED session_id=%s", session_id)
                    continue
                LOG.info(
                    "POST_CALL_WORKER_RECOVERED session_id=%s job_id=%s pid=%s",
                    session_id,
                    current.get("job_id"),
                    getattr(worker, "pid", "unknown"),
                )
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _on_idle(self) -> None:
        self._redial_deferred_call()
        self._recover_blocked_post_call_jobs()

    def prepare_outbound_call(self) -> bool:
        """Prepare Gemma during dialing so the worker can play the wait prompt."""
        if self._relay_operation_active:
            return False
        if self._inbound_call or self._inbound_prepare_thread is not None:
            return True
        supervisor = Path(os.environ.get(
            "REX_VOICE_SUPERVISOR",
            "/home/math3matica/hermes/test-llama/model_supervisor.sh",
        ))
        if not supervisor.is_file():
            raise RuntimeError("model supervisor is unavailable")
        self._write_inbound_readiness("waiting")
        self._inbound_call = True
        self._inbound_prepare_thread = threading.Thread(
            target=self._prepare_gemma_after_answer,
            args=(supervisor,),
            name="rex-outbound-gemma-preparation",
            daemon=True,
        )
        self._inbound_prepare_thread.start()
        LOG.info("OUTBOUND_CALL_PREPARATION_STARTED")
        return True

    def prepare_inbound_call(self) -> bool:
        """Answer first, then prepare Gemma while the worker plays the wait prompt."""
        if not self.inbound_caller_is_authorized():
            raise RuntimeError("inbound caller is not authorized")
        if self._relay_operation_active:
            self._defer_missed_inbound_call()
            return False
        if self._inbound_call or self._inbound_prepare_thread is not None:
            return True
        supervisor = Path(os.environ.get(
            "REX_VOICE_SUPERVISOR",
            "/home/math3matica/hermes/test-llama/model_supervisor.sh",
        ))
        if not supervisor.is_file():
            raise RuntimeError("model supervisor is unavailable")
        self._write_inbound_readiness("waiting")
        self._inbound_call = True
        if not self._answer_inbound_call():
            self._inbound_call = False
            self._defer_missed_inbound_call()
            self.restore_qwen_after_call()
            raise RuntimeError("inbound answer was not confirmed OFFHOOK")
        LOG.info("INBOUND_CALL_ANSWER_REQUESTED")
        self._inbound_prepare_thread = threading.Thread(
            target=self._prepare_gemma_after_answer,
            args=(supervisor,),
            name="rex-inbound-gemma-preparation",
            daemon=True,
        )
        self._inbound_prepare_thread.start()
        LOG.info("INBOUND_CALL_WAIT_MESSAGE_PENDING")
        return True

    def _answer_inbound_call(self) -> bool:
        """Answer only when the phone confirms the transition to OFFHOOK.

        Samsung Telecom accepts the app-level API call without throwing even
        when the caller is not privileged, so an ``OK`` bridge response is not
        evidence that the call was answered.  The host-owned call key fallback
        uses the already-authorized ADB connection and preserves the caller
        allowlist above this boundary.
        """
        response = self._bridge("ANSWER", timeout=10)
        if self._wait_for_call_state("OFFHOOK"):
            return True
        LOG.warning("INBOUND_CALL_ANSWER_API_UNCONFIRMED response=%s", response)

        command = [self.adb]
        if self.adb_serial:
            command.extend(["-s", self.adb_serial])
        command.extend(["shell", "input", "keyevent", "KEYCODE_CALL"])
        fallback = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        if fallback.returncode != 0:
            LOG.error("INBOUND_CALL_ANSWER_FALLBACK_FAILED error=%s", fallback.stderr.strip())
            return False
        LOG.info("INBOUND_CALL_ANSWER_FALLBACK_REQUESTED")
        return self._wait_for_call_state("OFFHOOK")

    def _wait_for_call_state(self, expected: str, *, timeout: float = ANSWER_VERIFY_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.call_state() == expected:
                    return True
            except Exception:
                pass
            time.sleep(ANSWER_VERIFY_INTERVAL)
        return False

    def call_supervisor(self) -> CallSessionSupervisor:
        supervisor: CallSessionSupervisor
        supervisor = CallSessionSupervisor(
            phone_state=self.call_state,
            hfp_nodes=self.hfp_nodes_available,
            worker_factory=lambda: self.call_worker_factory(supervisor.call_session_id),
            mute_sinks=self.mute_call_sinks,
            restore_sinks=self.restore_call_sinks,
            on_end=self.restore_qwen_after_call,
            on_dialing=self.prepare_outbound_call,
            on_ringing=self.prepare_inbound_call,
            on_idle=self._on_idle,
            log=LOG.info,
        )
        return supervisor

    def _bridge(self, command: str, timeout: float) -> str:
        # The bridge receives a shell argument, not JSON.  Keep Unicode bytes
        # intact; json.dumps defaults to ensure_ascii=True and would turn e.g.
        # an em dash into literal ``\\u2014`` text in the delivered SMS.
        device_command = f"printf '%s\\n' {json.dumps(command, ensure_ascii=False)} | nc -w {int(timeout)} 127.0.0.1 {self.port}"
        adb_command = [self.adb]
        if self.adb_serial:
            adb_command.extend(["-s", self.adb_serial])
        adb_command.extend(["shell", device_command])
        result = subprocess.run(
            adb_command,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"adb exited {result.returncode}")
        return result.stdout.strip()

    def poll(self) -> int:
        messages = parse_inbox_response(self._bridge("READ_INBOX", 5))
        added = 0
        pending_ids = {item["message_id"] for item in self.state["pending"]}
        for message in messages:
            if message.address != self.owner:
                LOG.warning("discarding inbound SMS from unauthorized sender %s", message.address)
                continue
            if message.message_id in pending_ids:
                continue
            self.state["pending"].append({**asdict(message), "message_id": message.message_id})
            pending_ids.add(message.message_id)
            added += 1
        if added:
            _save_state(self.state_path, self.state)
        return added

    def _ask_hermes(self, message: str) -> str:
        prompt = (
            "You are replying by SMS to the owner of this Hermes session. "
            "Return only the final user-facing answer as plain text suitable for "
            "a phone message. Answer directly and concisely. Never emit workflow "
            "commands, review instructions, tool names, CLI banners, or internal "
            "routing. Do not mention the relay or this prompt unless asked.\n\n"
            f"Owner's SMS:\n{message}"
        )
        if self.qwen_lifecycle:
            self.qwen_lifecycle.ensure_ready()
        command = [self.hermes_bin, "chat", "-q", prompt, "-Q", "--source", "sms"]
        session_id = self.state.get("session_id")
        if session_id:
            command.extend(["--resume", str(session_id)])
        result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"hermes exited {result.returncode}")
        output = result.stdout
        for line in (result.stdout + "\n" + result.stderr).splitlines():
            if line.startswith("session_id:"):
                self.state["session_id"] = line.split(":", 1)[1].strip()
                _save_state(self.state_path, self.state)
                break
        reply = extract_hermes_reply(output)
        if not reply:
            raise RuntimeError("Hermes returned an empty reply")
        return reply

    def drain(self) -> int:
        completed = 0
        while self.state["pending"]:
            item = self.state["pending"][0]
            started = time.monotonic()
            reply = self._ask_hermes(item["body"])
            hermes_seconds = time.monotonic() - started
            sms_body = format_sms_reply(reply)
            if not sms_body:
                raise RuntimeError("Hermes returned no phone-readable reply")
            send_started = time.monotonic()
            response = self._bridge(f"SEND {self.owner} {sms_body}", 10)
            send_seconds = time.monotonic() - send_started
            if not response.startswith("OK sent to "):
                raise RuntimeError(response or "bridge rejected outbound reply")
            self.state["pending"].pop(0)
            _save_state(self.state_path, self.state)
            completed += 1
            LOG.info(
                "replied to SMS %s (hermes_seconds=%.3f sms_send_seconds=%.3f total_seconds=%.3f)",
                item["message_id"][:12],
                hermes_seconds,
                send_seconds,
                time.monotonic() - started,
            )
        return completed

    def run_once(self) -> tuple[int, int]:
        self._relay_operation_active = True
        try:
            added = self.poll()
            completed = self.drain()
            if self.qwen_lifecycle:
                self.qwen_lifecycle.maybe_stop()
            return added, completed
        finally:
            self._relay_operation_active = False


class QwenLifecycle:
    """Keep the configured Qwen container warm, then stop it after idle time."""

    def __init__(self, *, idle_timeout: float, compose_dir: Path, compose_file: str = "docker-compose.yml") -> None:
        self.idle_timeout = idle_timeout
        self.compose_dir = compose_dir
        self.compose_file = compose_file
        self.last_activity = time.monotonic()

    def _docker(self, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", "-f", self.compose_file, *args],
            cwd=self.compose_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def _running(self) -> bool:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", "qwen"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    def ensure_ready(self) -> None:
        if not self._running():
            result = self._docker("up", "-d", "qwen", timeout=120.0)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "failed to start qwen")
            deadline = time.monotonic() + 600.0
            while time.monotonic() < deadline and not self._running():
                time.sleep(2.0)
            if not self._running():
                raise RuntimeError("qwen did not become running within 600 seconds")
            LOG.info("Qwen container started on demand")
        self.last_activity = time.monotonic()

    def maybe_stop(self) -> None:
        if self.idle_timeout <= 0 or not self._running():
            return
        idle = time.monotonic() - self.last_activity
        if idle < self.idle_timeout:
            return
        result = self._docker("stop", "qwen", timeout=60.0)
        if result.returncode != 0:
            LOG.error("failed to stop idle qwen: %s", result.stderr.strip())
            return
        LOG.info("stopped Qwen after %.1f seconds idle", idle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner",
        default=os.environ.get("HERMES_PHONE_OWNER", os.environ.get("REX_SMS_OWNER", DEFAULT_OWNER)),
    )
    parser.add_argument("--adb", default=None)
    parser.add_argument("--adb-serial", default=None)
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument(
        "--qwen-idle-timeout",
        type=float,
        default=float(os.environ.get("HERMES_PHONE_QWEN_IDLE_TIMEOUT", DEFAULT_QWEN_IDLE_TIMEOUT)),
        help="stop the local qwen container after this many idle seconds; 0 disables lifecycle control",
    )
    parser.add_argument(
        "--qwen-compose-dir",
        type=Path,
        default=Path(os.environ.get("HERMES_PHONE_QWEN_COMPOSE_DIR", os.environ.get("REX_VOICE_ROOT", str(Path.home() / "hermes")))),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    relay_lock = RelayInstanceLock()
    if not relay_lock.acquire():
        return 1

    def _shutdown(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        relay = HermesPhoneRelay(
            owner=args.owner,
            adb=args.adb,
            adb_serial=args.adb_serial,
            state_path=args.state,
            qwen_lifecycle=(
                QwenLifecycle(idle_timeout=args.qwen_idle_timeout, compose_dir=args.qwen_compose_dir)
                if args.qwen_idle_timeout > 0
                else None
            ),
        )
        call_supervisor = relay.call_supervisor()
        if args.once:
            added, completed = relay.run_once()
            LOG.info("relay pass: queued=%d replied=%d", added, completed)
            return 0
        LOG.info("SMS relay active for owner %s", relay.owner)
        while True:
            try:
                call_supervisor.tick()
            except Exception:
                LOG.exception("call-session supervision failed; preserving call safety state")
            try:
                relay.run_once()
            except Exception:
                LOG.exception("relay pass failed; preserving durable queue")
            time.sleep(max(1.0, args.poll_interval))
    finally:
        relay_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())


# Compatibility name for the original RexBridge proof-of-concept callers.
RexSmsRelay = HermesPhoneRelay
