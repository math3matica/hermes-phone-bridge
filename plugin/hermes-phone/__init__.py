"""Native Hermes plugin surfaces for Hermes-Phone.

Inbound conversation continuity remains owned by the host relay process. These
small tools expose explicit phone status and outbound SMS actions to any
Hermes profile that has enabled this plugin.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


def _bridge(command: str, timeout: int = 10) -> str:
    adb = os.environ.get("HERMES_PHONE_ADB") or shutil.which("adb") or "adb"
    serial = os.environ.get("HERMES_PHONE_ADB_SERIAL")
    wire = f"printf '%s\\n' {json.dumps(command, ensure_ascii=False)} | nc -w {timeout} 127.0.0.1 9999"
    argv = [adb]
    if serial:
        argv += ["-s", serial]
    argv += ["shell", wire]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 5, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"adb exited {result.returncode}")
    return result.stdout.strip()


def _status(args: dict[str, Any], **kwargs: Any) -> str:
    response = _bridge("PING", timeout=5)
    return json.dumps({"ok": response == "PONG", "bridge_response": response}, ensure_ascii=False)


def _send(args: dict[str, Any], **kwargs: Any) -> str:
    number = str(args.get("number", "")).strip()
    message = str(args.get("message", ""))
    if not number or not message:
        raise ValueError("number and message are required")
    response = _bridge(f"SEND {number} {' '.join(message.split())}")
    return json.dumps({"ok": response.startswith("OK sent to "), "bridge_response": response}, ensure_ascii=False)


def _audio_test(args: dict[str, Any], **kwargs: Any) -> str:
    seconds = int(args.get("seconds", 5))
    frequency = float(args.get("frequency", 1000))
    return _bridge(f"AUDIO_TEST {seconds} {frequency}", timeout=seconds + 10)


def _owner_number() -> str:
    number = os.environ.get("HERMES_PHONE_OWNER", os.environ.get("REX_SMS_OWNER", ""))
    if not number:
        raise RuntimeError("HERMES_PHONE_OWNER is not configured")
    return number


def _supervisor(command: str) -> subprocess.CompletedProcess[str]:
    supervisor = os.environ.get("HERMES_PHONE_MODEL_SUPERVISOR", "").strip()
    if not supervisor:
        raise RuntimeError("HERMES_PHONE_MODEL_SUPERVISOR is not configured")
    return subprocess.run(
        ["bash", supervisor, command],
        capture_output=True, text=True, timeout=900, check=False,
    )


def _health(url: str, expected_model: str | None = None) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(url + "/health", timeout=5) as response:
            if response.status != 200:
                return False
        if expected_model is not None:
            with urllib.request.urlopen(url + "/v1/models", timeout=5) as response:
                payload = json.load(response)
            return payload.get("data", [{}])[0].get("id") == expected_model
        return True
    except Exception:
        return False


def _restore_qwen() -> None:
    result = _supervisor("switch-to-qwen")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Qwen restoration failed")
    if not _health("http://127.0.0.1:8080", "/models/Qwen3.8-27B-UD-Q4_K_XL.gguf"):
        raise RuntimeError("Qwen restoration did not pass health/model verification")


def _audio_settings_path() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "phone-audio.json"


def _audio_settings() -> dict[str, str]:
    path = _audio_settings_path()
    if not path.exists():
        return {"backend": "analog"}
    with path.open(encoding="utf-8") as handle:
        settings = json.load(handle)
    backend = settings.get("backend", "analog") if isinstance(settings, dict) else ""
    if backend not in {"analog", "bluetooth"}:
        raise RuntimeError(f"invalid phone audio backend in {path}")
    return {"backend": backend}


def _set_audio_backend(backend: str) -> str:
    if backend not in {"analog", "bluetooth"}:
        raise ValueError("audio backend must be analog or bluetooth")
    path = _audio_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"backend": backend}, indent=2) + "\n", encoding="utf-8")
    return json.dumps({"ok": True, "backend": backend, "settings_path": str(path)}, ensure_ascii=False)


def _audio_settings_tool(args: dict[str, Any], **kwargs: Any) -> str:
    backend = str(args.get("backend", "")).strip().lower()
    if not backend:
        return json.dumps({"ok": True, **_audio_settings()}, ensure_ascii=False)
    return _set_audio_backend(backend)


def _audio_settings_command(raw_args: str) -> str:
    value = raw_args.strip().lower()
    if not value or value == "status":
        return json.dumps({"ok": True, **_audio_settings()}, ensure_ascii=False)
    return _set_audio_backend(value)


def _adb_argv() -> list[str]:
    adb = os.environ.get("HERMES_PHONE_ADB") or shutil.which("adb") or "adb"
    serial = os.environ.get("HERMES_PHONE_ADB_SERIAL")
    return [adb] + (["-s", serial] if serial else [])


def _direct_call(phone: str) -> str:
    result = subprocess.run(
        _adb_argv() + ["shell", "am", "start", "-W", "-a",
                       "android.intent.action.CALL", "-d", f"tel:{phone}"],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"adb exited {result.returncode}")
    if "Status: ok" not in result.stdout:
        raise RuntimeError(f"Android did not accept call intent: {result.stdout.strip()}")
    return result.stdout.strip()


def _call(args: dict[str, Any], **kwargs: Any) -> str:
    requested = str(args.get("number", "")).strip()
    owner = _owner_number()
    # Calling is an external side effect. Keep this tool owner-only until a
    # caller-identity-aware gateway boundary exists; accepting arbitrary
    # numbers here would turn a conversational tool into a dialler for anyone.
    if requested and requested != owner:
        raise ValueError("phone_call may target only the configured owner")
    # The relay owns call-scoped model preparation.  Preparing Gemma here as
    # well races the relay's RINGING transition: one supervisor can restore
    # Qwen while the call worker is using Gemma.  Only verify the independent
    # TTS dependency before dialing; the relay waits for its own readiness
    # marker before the worker speaks.
    if not _health("http://127.0.0.1:5187"):
        try: _restore_qwen()
        except Exception: pass
        raise RuntimeError("Kokoro TTS health verification failed; call not dialed")
    try:
        launch = _direct_call(owner)
        return json.dumps({"ok": True, "call_intent": launch, "number": owner, "voice_prepared": "relay_pending"}, ensure_ascii=False)
    except Exception:
        try: _restore_qwen()
        except Exception: pass
        raise


def _call_status(args: dict[str, Any], **kwargs: Any) -> str:
    response = _bridge("CALL_STATUS", timeout=5)
    if not response.startswith("OK "):
        raise RuntimeError(response or "phone bridge rejected call status")
    try:
        payload = json.loads(response[3:])
    except json.JSONDecodeError as exc:
        raise RuntimeError("phone bridge returned invalid call status") from exc
    if not isinstance(payload, dict) or payload.get("state") not in {"IDLE", "RINGING", "OFFHOOK", "DIALING", "UNKNOWN"}:
        raise RuntimeError("phone bridge returned invalid call state")
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _hangup(args: dict[str, Any], **kwargs: Any) -> str:
    response = _bridge("HANGUP", timeout=15)
    if not response.startswith("OK hangup "):
        raise RuntimeError(response or "phone bridge rejected hangup")
    deadline = time.monotonic() + 30
    state = "UNKNOWN"
    while time.monotonic() < deadline:
        status = _bridge("CALL_STATUS", timeout=5)
        if status.startswith("OK "):
            try: state = json.loads(status[3:]).get("state", "UNKNOWN")
            except json.JSONDecodeError: state = "UNKNOWN"
        if state == "IDLE": break
        time.sleep(1)
    if state != "IDLE":
        raise RuntimeError(f"call did not reach IDLE after hangup (state={state})")
    return json.dumps({"ok": True, "bridge_response": response, "call_state": state}, ensure_ascii=False)


def register(ctx) -> None:
    ctx.register_tool(
        name="phone_status",
        toolset="hermes_phone",
        schema={
            "name": "phone_status",
            "description": "Check whether the configured Hermes-Phone Android SMS bridge is reachable.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        handler=_status,
        check_fn=lambda: True,
        emoji="📱",
        capabilities=("phone.status",),
    )
    ctx.register_tool(
        name="phone_sms_send",
        toolset="hermes_phone",
        schema={
            "name": "phone_sms_send",
            "description": "Send an SMS through the configured Hermes-Phone bridge.",
            "parameters": {
                "type": "object",
                "properties": {"number": {"type": "string"}, "message": {"type": "string"}},
                "required": ["number", "message"],
                "additionalProperties": False,
            },
        },
        handler=_send,
        check_fn=lambda: True,
        emoji="📱",
        capabilities=("phone.sms.send", "side_effect.external"),
    )
    ctx.register_tool(
        name="phone_audio_test",
        toolset="hermes_phone",
        schema={
            "name": "phone_audio_test",
            "description": "Record the Android phone microphone and measure a tone arriving through its audio input.",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "integer", "minimum": 1, "maximum": 15},
                    "frequency": {"type": "number", "minimum": 20, "maximum": 20000},
                },
                "additionalProperties": False,
            },
        },
        handler=_audio_test,
        check_fn=lambda: True,
        emoji="🔊",
        capabilities=("phone.audio.test",),
    )
    ctx.register_tool(
        name="phone_audio_settings",
        toolset="hermes_phone",
        schema={
            "name": "phone_audio_settings",
            "description": "Read or change the phone call audio transport. USB/ADB still controls the phone and places calls; this changes audio only.",
            "parameters": {
                "type": "object",
                "properties": {"backend": {"type": "string", "enum": ["analog", "bluetooth"]}},
                "additionalProperties": False,
            },
        },
        handler=_audio_settings_tool,
        check_fn=lambda: True,
        emoji="🔊",
        capabilities=("phone.audio.settings",),
    )
    ctx.register_command(
        "phone-audio",
        handler=_audio_settings_command,
        description="Show or switch phone call audio transport: analog or Bluetooth.",
        args_hint="[status|analog|bluetooth]",
    )
    ctx.register_tool(
        name="phone_call",
        toolset="hermes_phone",
        schema={
            "name": "phone_call",
            "description": "Dial the configured owner through the Android phone. Use only after voice mode and TTS readiness have been verified.",
            "parameters": {
                "type": "object",
                "properties": {"number": {"type": "string", "description": "Optional confirmation of the configured owner number."}},
                "additionalProperties": False,
            },
        },
        handler=_call,
        check_fn=lambda: True,
        emoji="📞",
        capabilities=("phone.call.start", "side_effect.external"),
    )
    ctx.register_tool(
        name="phone_call_status",
        toolset="hermes_phone",
        schema={
            "name": "phone_call_status",
            "description": "Read the current Android cellular call state.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        handler=_call_status,
        check_fn=lambda: True,
        emoji="📞",
        capabilities=("phone.call.status",),
    )
    ctx.register_tool(
        name="phone_hangup",
        toolset="hermes_phone",
        schema={
            "name": "phone_hangup",
            "description": "End the active Android cellular call.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        handler=_hangup,
        check_fn=lambda: True,
        emoji="📞",
        capabilities=("phone.call.end", "side_effect.external"),
    )
