import fcntl
import json
import importlib.util
import os
import subprocess
import sys
import time
import wave
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
import call_audio_loop

from hermes_phone_relay import (
    CallSessionSupervisor,
    HermesPhoneRelay,
    QwenLifecycle,
    RelayInstanceLock,
    SmsMessage,
    parse_audio_test_response,
    extract_hermes_reply,
    format_sms_reply,
    normalize_phone,
    parse_inbox_response,
)
from call_audio_loop import (
    CallAudioConfig,
    CallAudioCoordinator,
    UtteranceEndpointDetector,
    audio_settings_path,
    load_audio_settings,
)


def test_relay_instance_lock_is_exclusive_and_releases(tmp_path):
    events = []
    first = RelayInstanceLock(tmp_path / "hermes-phone-relay.lock", log=events.append)
    second = RelayInstanceLock(tmp_path / "hermes-phone-relay.lock", log=events.append)

    assert first.acquire()
    assert not second.acquire()
    assert "RELAY_INSTANCE_START_REJECTED reason=active_relay_lock" in events
    second.release()
    first.release()

    third = RelayInstanceLock(tmp_path / "hermes-phone-relay.lock", log=events.append)
    assert third.acquire()
    third.release()
    assert events.count("RELAY_INSTANCE_LOCK_ACQUIRED") == 2


def test_rejected_relay_main_does_not_construct_or_poll(tmp_path, monkeypatch):
    lock_path = tmp_path / "hermes-phone-relay.lock"
    owner = RelayInstanceLock(lock_path)
    assert owner.acquire()
    try:
        monkeypatch.setenv("HERMES_RELAY_LOCK_PATH", str(lock_path))
        monkeypatch.setattr(
            "hermes_phone_relay.HermesPhoneRelay",
            lambda **_: (_ for _ in ()).throw(AssertionError("constructed")),
        )
        monkeypatch.setattr("sys.argv", ["hermes_phone_relay.py", "--once"])
        import hermes_phone_relay

        assert hermes_phone_relay.main() == 1
    finally:
        owner.release()


def test_relay_lock_is_released_when_owner_process_exits(tmp_path):
    lock_path = tmp_path / "hermes-phone-relay.lock"
    code = (
        "import time; "
        "from hermes_phone_relay import RelayInstanceLock; "
        f"lock=RelayInstanceLock(__import__('pathlib').Path({str(lock_path)!r})); "
        "assert lock.acquire(); time.sleep(30)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parent,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent)},
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if lock_path.exists() and lock_path.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.02)
        assert lock_path.read_text(encoding="utf-8").startswith("pid=")
    finally:
        child.terminate()
        child.wait(timeout=5)
    replacement = RelayInstanceLock(lock_path)
    assert replacement.acquire()
    replacement.release()


def test_canonical_systemd_owner_uses_environment_file_for_per_run_config():
    unit = (Path(__file__).parents[1] / "systemd" / "hermes-phone-relay.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=-%h/.config/hermes/hermes-phone-relay.env" in unit
    assert "ExecStart=/usr/bin/python3 %h/hermes-phone-bridge/host/hermes_phone_relay.py" in unit
    assert "Restart=always" in unit


def _pcm_frame(amplitude: int, samples: int = 320) -> bytes:
    return (amplitude.to_bytes(2, "little", signed=True)) * samples


def test_utterance_longer_than_four_seconds_is_not_split():
    detector = UtteranceEndpointDetector(
        sample_rate=16000, pre_roll_seconds=0.3, trailing_silence_seconds=0.8,
        post_roll_seconds=0.2, max_utterance_seconds=20,
    )
    result = None
    for _ in range(250):
        result = detector.feed(_pcm_frame(5000)) or result
    assert result is None
    result = detector.feed(_pcm_frame(5000))
    assert result is None
    for _ in range(55):
        result = detector.feed(_pcm_frame(0)) or result
    assert result is not None
    assert len(result) / (2 * 16000) >= 5.0


def test_short_silence_inside_utterance_does_not_end_it():
    detector = UtteranceEndpointDetector(sample_rate=16000, trailing_silence_seconds=0.8)
    for _ in range(20):
        assert detector.feed(_pcm_frame(5000)) is None
    for _ in range(8):
        assert detector.feed(_pcm_frame(0)) is None
    for _ in range(20):
        assert detector.feed(_pcm_frame(5000)) is None
    result = None
    for _ in range(60):
        result = detector.feed(_pcm_frame(0)) or result
    assert result is not None


def test_trailing_silence_ends_utterance_with_post_roll():
    detector = UtteranceEndpointDetector(
        sample_rate=16000, pre_roll_seconds=0.4, trailing_silence_seconds=0.8,
        post_roll_seconds=0.2,
    )
    for _ in range(10):
        detector.feed(_pcm_frame(5000))
    result = None
    for _ in range(60):
        result = detector.feed(_pcm_frame(0)) or result
    assert result is not None
    assert len(result) / (2 * 16000) >= 1.0


def test_endpoint_detector_records_precise_vad_offsets_and_reason():
    detector = UtteranceEndpointDetector(
        sample_rate=16000, pre_roll_seconds=0.3,
        trailing_silence_seconds=0.8, post_roll_seconds=0.2,
    )
    for _ in range(2):
        assert detector.feed(_pcm_frame(0)) is None
    for _ in range(3):
        assert detector.feed(_pcm_frame(5000)) is None
    result = None
    for _ in range(55):
        result = detector.feed(_pcm_frame(0)) or result
    assert result is not None
    assert detector.speech_start_offset == 0.04
    assert detector.segment_start_offset == 0.0
    assert detector.endpoint_reason == "trailing_silence"
    assert detector.endpoint_offset == 1.08


def test_retained_artifact_directory_is_unique_and_writes_boundary_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PHONE_RETAIN_AUDIO_DIR", str(tmp_path))
    coordinator = CallAudioCoordinator(CallAudioConfig())
    with coordinator._artifact_directory() as directory:
        path = Path(directory) / "turn-0.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(_pcm_frame(5000, 160))
        evidence = {"schema": "rex-stt-boundary-v1", "wav_path": str(path), "transcript": "hello", "model_facing_user_request": "hello"}
        coordinator._write_stt_evidence(evidence, directory)
        assert path.is_file()
        records = (Path(directory) / "stt-boundary.jsonl").read_text(encoding="utf-8").splitlines()
        assert json.loads(records[0])["model_facing_user_request"] == "hello"
    assert list(tmp_path.glob("call-*/turn-0.wav"))


def test_maximum_utterance_duration_forces_endpoint():
    detector = UtteranceEndpointDetector(sample_rate=16000, max_utterance_seconds=2)
    result = None
    for _ in range(110):
        result = detector.feed(_pcm_frame(5000)) or result
    assert result is not None
    assert len(result) / (2 * 16000) <= 2.1


def test_normalize_phone_accepts_owner_variants():
    assert normalize_phone("5550100199") == "+15550100199"
    assert normalize_phone("+1 555-010-0199") == "+15550100199"


def test_parse_inbox_response_returns_messages():
    raw = '''OK [
      {"address": "+155****0199", "timestamp": 1788404186000,
       "body": "Hello Hermes"}
    ]'''
    assert parse_inbox_response(raw) == [
        SmsMessage(
            address="+1550199",
            timestamp=1788404186000,
            body="Hello Hermes",
        )
    ]


def test_parse_inbox_response_rejects_error():
    try:
        parse_inbox_response("ERR bridge unavailable")
    except ValueError as exc:
        assert "bridge unavailable" in str(exc)
    else:
        raise AssertionError("expected malformed inbox response to fail")


def test_extract_hermes_reply_ignores_session_banner():
    output = (
        "↻ Resumed session abc (2 user messages, 4 total messages)\n\n"
        "session_id: abc\nHere is the answer.\nSecond line.\n"
    )
    assert extract_hermes_reply(output) == "Here is the answer.\nSecond line."


def test_format_sms_reply_removes_internal_review_directive():
    assert format_sms_reply("! review diff\nThe actual answer.") == "The actual answer."


def test_ask_hermes_persists_session_id_from_stderr_and_uses_stdout_reply(tmp_path):
    relay = HermesPhoneRelay(state_path=tmp_path / "state.json", adb="adb", hermes_bin="hermes")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="The actual answer.\n",
            stderr="session_id: 20260903_123456_abcdef\n",
        )

    with patch("hermes_phone_relay.subprocess.run", side_effect=fake_run):
        assert relay._ask_hermes("hello") == "The actual answer."

    assert relay.state["session_id"] == "20260903_123456_abcdef"
    assert relay.state_path.exists()
    assert json.loads(relay.state_path.read_text())["session_id"] == "20260903_123456_abcdef"


def test_qwen_lifecycle_stops_only_after_idle_timeout(tmp_path):
    lifecycle = QwenLifecycle(idle_timeout=10, compose_dir=tmp_path)
    lifecycle.last_activity -= 11
    with patch.object(lifecycle, "_running", return_value=True), patch.object(
        lifecycle, "_docker", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
    ) as docker:
        lifecycle.maybe_stop()
    docker.assert_called_once_with("stop", "qwen", timeout=60.0)


def test_bridge_preserves_unicode_sms_text():
    captured = []

    def fake_run(command, **kwargs):
        captured.append(command[-1])
        return subprocess.CompletedProcess(command, 0, stdout="OK sent", stderr="")

    relay = HermesPhoneRelay(state_path=Path("/tmp/rex-sms-unicode-test-state.json"), adb="adb")
    with patch("hermes_phone_relay.subprocess.run", side_effect=fake_run):
        relay._bridge("SEND +15550100199 café — résumé", 10)

    assert "café" in captured[0]
    assert "résumé" in captured[0]
    assert "\\u00e9" not in captured[0]


def test_parse_audio_test_response_requires_measurements():
    result = parse_audio_test_response('OK {"rms":0.12,"frequency":997.5,"tone":0.08}')
    assert result == {"rms": 0.12, "frequency": 997.5, "tone": 0.08}


def test_idle_pre_call_state_does_not_require_dynamic_hfp_nodes(tmp_path):
    events = []
    hfp_checks = []
    supervisor = CallSessionSupervisor(
        phone_state=lambda: "IDLE",
        hfp_nodes=lambda: hfp_checks.append(True) or False,
        worker_factory=lambda: _LifecycleWorker(),
        mute_sinks=lambda: events.append("mute"),
        restore_sinks=lambda: events.append("restore"),
        log=events.append,
        worker_lock_path=tmp_path / "call-worker.lock",
    )

    supervisor.tick()

    assert hfp_checks == []
    assert supervisor.worker is None
    assert "CALL_SESSION_START" not in events
    lock_path = tmp_path / "call-worker.lock"
    assert not lock_path.exists() or lock_path.read_text() == ""


@pytest.mark.skipif(importlib.util.find_spec("rex_voice_v1") is None, reason="optional Rex Voice runtime is not installed")
def test_idle_releases_a_job_blocked_only_by_active_call(tmp_path, monkeypatch):
    from rex_voice_v1.post_call import PostCallQueue

    artifacts = tmp_path / "rex-voice-v1"
    queue = PostCallQueue(artifacts / "post-call")
    queue.enqueue("voice-recover", {"session_id": "voice-recover"})
    job = queue.load("voice-recover")
    job["execution_gate"] = {"status": "blocked", "reason": "call_not_idle"}
    queue.save(job)

    started = []
    monkeypatch.setenv("REX_VOICE_ARTIFACTS", str(artifacts))
    monkeypatch.setattr("hermes_phone_relay.HermesPhoneRelay._qwen_ready_for_post_call", lambda _self: ("/models/qwen.gguf", "Qwen 27B"))
    monkeypatch.setattr("rex_voice_v1.post_call_worker.start_detached", lambda root, session_id, **kwargs: started.append((root, session_id, kwargs)) or type("Worker", (), {"pid": 42})())

    relay = HermesPhoneRelay(state_path=tmp_path / "state.json", adb="adb")
    relay._recover_blocked_post_call_jobs()

    recovered = queue.load("voice-recover")
    assert recovered["execution_gate"] == {
        "status": "released",
        "model": "/models/qwen.gguf",
        "provider": "Qwen 27B",
    }
    assert len(started) == 1


@pytest.mark.skipif(importlib.util.find_spec("rex_voice_v1") is None, reason="optional Rex Voice runtime is not installed")
def test_idle_releases_job_waiting_for_normal_model_after_qwen_recovery(tmp_path, monkeypatch):
    from rex_voice_v1.post_call import PostCallQueue

    artifacts = tmp_path / "rex-voice-v1"
    queue = PostCallQueue(artifacts / "post-call")
    queue.enqueue("voice-awaiting-qwen", {"session_id": "voice-awaiting-qwen"})

    started = []
    monkeypatch.setenv("REX_VOICE_ARTIFACTS", str(artifacts))
    monkeypatch.setattr("hermes_phone_relay.HermesPhoneRelay._qwen_ready_for_post_call", lambda _self: ("/models/qwen.gguf", "Qwen 27B"))
    monkeypatch.setattr("rex_voice_v1.post_call_worker.start_detached", lambda root, session_id, **kwargs: started.append((root, session_id, kwargs)) or type("Worker", (), {"pid": 43})())

    relay = HermesPhoneRelay(state_path=tmp_path / "state.json", adb="adb")
    relay._recover_blocked_post_call_jobs()

    recovered = queue.load("voice-awaiting-qwen")
    assert recovered["execution_gate"] == {
        "status": "released",
        "model": "/models/qwen.gguf",
        "provider": "Qwen 27B",
    }
    assert len(started) == 1


@pytest.mark.skipif(importlib.util.find_spec("rex_voice_v1") is None, reason="optional Rex Voice runtime is not installed")
def test_idle_recovery_releases_before_launch_and_is_idempotent(tmp_path, monkeypatch):
    from rex_voice_v1.post_call import PostCallQueue

    artifacts = tmp_path / "rex-voice-v1"
    queue = PostCallQueue(artifacts / "post-call")
    queue.enqueue("voice-release-order", {"session_id": "voice-release-order"})
    monkeypatch.setenv("REX_VOICE_ARTIFACTS", str(artifacts))
    monkeypatch.setattr("hermes_phone_relay.HermesPhoneRelay._qwen_ready_for_post_call", lambda _self: ("/models/qwen.gguf", "Qwen 27B"))

    observed = []

    def fake_start(root, session_id, **kwargs):
        observed.append(queue.load(session_id)["execution_gate"]["status"])
        return type("Worker", (), {"pid": 44})()

    monkeypatch.setattr("rex_voice_v1.post_call_worker.start_detached", fake_start)
    relay = HermesPhoneRelay(state_path=tmp_path / "state.json", adb="adb")
    relay._recover_blocked_post_call_jobs()
    relay._recover_blocked_post_call_jobs()

    assert observed == ["released"]


@pytest.mark.skipif(importlib.util.find_spec("rex_voice_v1") is None, reason="optional Rex Voice runtime is not installed")
def test_idle_recovery_is_idempotent_for_released_job(tmp_path, monkeypatch):
    from rex_voice_v1.post_call import PostCallQueue

    artifacts = tmp_path / "rex-voice-v1"
    queue = PostCallQueue(artifacts / "post-call")
    queue.enqueue("voice-recover-once", {"session_id": "voice-recover-once"})
    job = queue.load("voice-recover-once")
    job["execution_gate"] = {"status": "released", "model": "/models/qwen.gguf", "provider": "Qwen 27B"}
    queue.save(job)
    monkeypatch.setenv("REX_VOICE_ARTIFACTS", str(artifacts))
    monkeypatch.setattr("hermes_phone_relay.HermesPhoneRelay._qwen_ready_for_post_call", lambda _self: ("/models/qwen.gguf", "Qwen 27B"))
    monkeypatch.setattr("rex_voice_v1.post_call_worker.start_detached", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("duplicate launch")))

    relay = HermesPhoneRelay(state_path=tmp_path / "state.json", adb="adb")
    relay._recover_blocked_post_call_jobs()


def test_call_session_supervisor_does_not_restart_worker_while_call_is_offhook(tmp_path):
    states = iter(["OFFHOOK", "OFFHOOK", "OFFHOOK", "IDLE"])
    events = []

    class Worker:
        def __init__(self, returncode=None):
            self.returncode = returncode
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.returncode = -15

    workers = [Worker(1), Worker(None)]

    supervisor = CallSessionSupervisor(
        phone_state=lambda: next(states),
        hfp_nodes=lambda: True,
        worker_factory=lambda: workers.pop(0),
        mute_sinks=lambda: events.append("mute"),
        restore_sinks=lambda: events.append("restore"),
        log=events.append,
        worker_lock_path=tmp_path / "call-worker.lock",
    )

    supervisor.tick()
    supervisor.tick()
    supervisor.tick()
    supervisor.tick()

    assert events.count("mute") == 1
    assert events.count("restore") == 1
    assert events.count("CALL_SESSION_START") == 1
    assert events.count("CALL_SESSION_END") == 1
    assert not any(event.startswith("CALL_WORKER_RESTART attempt=") for event in events)
    assert "CALL_RUNTIME_FAILED session_id=" in "\\n".join(events)
    assert "CALL_WORKER_RESTART_SUPPRESSED reason=runtime_failed" in events
    assert len(workers) == 1


class _LifecycleWorker:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.returncode = -15


def _supervisor_for_test(tmp_path, states, workers, events):
    return CallSessionSupervisor(
        phone_state=lambda: next(states),
        hfp_nodes=lambda: True,
        worker_factory=lambda: workers.pop(0),
        mute_sinks=lambda: events.append("mute"),
        restore_sinks=lambda: events.append("restore"),
        log=events.append,
        worker_lock_path=tmp_path / "call-worker.lock",
    )


def test_one_offhook_event_starts_one_worker(tmp_path):
    events = []
    workers = [_LifecycleWorker()]
    supervisor = _supervisor_for_test(tmp_path, iter(["OFFHOOK", "IDLE"]), workers, events)

    supervisor.tick()

    assert len(workers) == 0
    starts = [event for event in events if event == "CALL_SESSION_START"]
    assert len(starts) == 1
    assert supervisor.call_session_id is not None
    assert any(event == "CALL_SESSION_ID session_id=" + supervisor.call_session_id for event in events)

    supervisor.tick()
    assert supervisor.worker is None
    assert supervisor._worker_lock is None


def test_outbound_dialing_callback_runs_before_offhook_worker(tmp_path):
    events = []
    supervisor = CallSessionSupervisor(
        phone_state=lambda: ["DIALING", "OFFHOOK"][len([e for e in events if e == "prep"])],
        hfp_nodes=lambda: True,
        worker_factory=lambda: (events.append("worker"), _LifecycleWorker())[1],
        mute_sinks=lambda: None,
        restore_sinks=lambda: None,
        on_dialing=lambda: events.append("prep") or True,
        log=events.append,
        worker_lock_path=tmp_path / "call-worker.lock",
    )

    supervisor.tick()
    supervisor.tick()

    assert events.index("prep") < events.index("worker")


def test_relay_passes_session_id_to_worker_and_holds_lock(tmp_path):
    events = []
    worker = _LifecycleWorker()
    received = []

    supervisor: CallSessionSupervisor
    supervisor = CallSessionSupervisor(
        phone_state=lambda: "OFFHOOK",
        hfp_nodes=lambda: True,
        worker_factory=lambda: (received.append(supervisor.call_session_id), worker)[1],
        mute_sinks=lambda: None,
        restore_sinks=lambda: None,
        log=events.append,
        worker_lock_path=tmp_path / "call-worker.lock",
    )

    supervisor.tick()

    assert len(received) == 1
    assert received[0] == supervisor.call_session_id
    assert events.count("CALL_SESSION_START") == 1
    assert not any(event.startswith("CALL_WORKER_START_REJECTED") for event in events)
    handle = (tmp_path / "call-worker.lock").open("a+")
    try:
        with patch.object(fcntl, "flock", wraps=fcntl.flock) as flock:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("relay worker did not own the call lock")
            assert flock.call_count == 1
    finally:
        handle.close()
    supervisor._end()
    assert (tmp_path / "call-worker.lock").read_text() == ""


def test_production_runtime_session_id_is_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("REX_CALL_WORKER_SESSION_ID", raising=False)
    relay = HermesPhoneRelay(owner="+155****0199", state_path=tmp_path / "state.json")
    worker = _LifecycleWorker()
    with patch("hermes_phone_relay.subprocess.Popen", return_value=worker) as popen:
        result = relay.call_worker_factory("call-abc123")

    assert result is worker
    command = popen.call_args.args[0]
    environment = popen.call_args.kwargs["env"]
    assert command[1].endswith("/call_audio_loop.py")
    assert "--max-turns" not in command
    assert environment["HERMES_CALL_WORKER_SESSION_ID"] == "call-abc123"
    assert environment["HERMES_REX_VOICE_V1"] == "1"

    class FakeCoordinator:
        def __init__(self, config, acceptance_gate=None):
            self.config = config
            self.acceptance_gate = acceptance_gate

        def run_session(self, max_turns=None):
            return 0

    monkeypatch.setenv("HERMES_CALL_WORKER_SESSION_ID", environment["HERMES_CALL_WORKER_SESSION_ID"])
    with patch("call_audio_loop.CallAudioCoordinator", FakeCoordinator), \
            patch("call_audio_loop.LOG.info") as log_info, \
            patch("sys.argv", ["call_audio_loop.py", "--audio-backend", "analog", "--max-turns", "0", "--session-sinks-managed"]):
        assert call_audio_loop.main() == 0

    log_info.assert_any_call(
        "CALL_RUNTIME_SESSION_ID session_id=%s",
        "call-abc123",
    )


def test_duplicate_offhook_ticks_still_start_one_worker(tmp_path):
    events = []
    workers = [_LifecycleWorker()]
    supervisor = _supervisor_for_test(tmp_path, iter(["OFFHOOK", "OFFHOOK", "OFFHOOK", "IDLE"]), workers, events)

    for _ in range(4):
        supervisor.tick()

    assert len([event for event in events if event.startswith("CALL_SESSION_START")]) == 1
    assert events.count("CALL_WORKER_EXITED") == 1
    assert not workers


def test_second_worker_start_is_rejected_by_active_call_guard(tmp_path):
    events = []
    first_workers = [_LifecycleWorker()]
    second_workers = [_LifecycleWorker()]
    first = _supervisor_for_test(tmp_path, iter(["OFFHOOK"]), first_workers, events)
    second = _supervisor_for_test(tmp_path, iter(["OFFHOOK"]), second_workers, events)

    first.tick()
    second.tick()

    assert len(second_workers) == 1
    assert second.worker is None
    assert "CALL_WORKER_START_REJECTED reason=active_call_worker_lock" in events

    first._end()


def test_worker_failure_without_hfp_nodes_does_not_restart_or_redial(tmp_path):
    events = []
    workers = [_LifecycleWorker()]
    workers[0].returncode = 1
    supervisor = CallSessionSupervisor(
        phone_state=lambda: "OFFHOOK",
        hfp_nodes=lambda: False,
        worker_factory=lambda: workers.pop(0),
        mute_sinks=lambda: None,
        restore_sinks=lambda: None,
        log=events.append,
        worker_lock_path=tmp_path / "call-worker.lock",
    )

    supervisor.tick()

    assert workers == []
    assert supervisor.worker is None
    assert "CALL_WORKER_RESTART_SUPPRESSED reason=runtime_failed" in events
    assert not any(event.startswith("CALL_WORKER_RESTART attempt=") for event in events)


def test_phone_call_path_plus_relay_supervision_still_has_one_worker(tmp_path, monkeypatch):
    # Exercise the installed plugin entrypoint used by Hermes, not only the
    # source copy.  The two copies must remain behaviorally identical.
    plugin_path = Path(__file__).parents[1] / "plugin" / "hermes-phone" / "__init__.py"
    spec = importlib.util.spec_from_file_location("hermes_phone_plugin_test", plugin_path)
    assert spec is not None
    plugin = importlib.util.module_from_spec(spec)
    loader = spec.loader
    assert loader is not None
    loader.exec_module(plugin)
    monkeypatch.setenv("HERMES_PHONE_OWNER", "+155****0199")
    health_urls = []
    monkeypatch.setattr(plugin, "_health", lambda url, expected_model=None: health_urls.append((url, expected_model)) or True)
    monkeypatch.setattr(plugin, "_direct_call", lambda phone: "Status: ok")
    with patch.object(plugin.subprocess, "Popen", side_effect=AssertionError("phone_call spawned audio worker")):
        result = plugin._call({})
    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["voice_prepared"] == "relay_pending"
    assert health_urls == [("http://127.0.0.1:5187", None)]

    events = []
    workers = [_LifecycleWorker()]
    supervisor = _supervisor_for_test(tmp_path, iter(["OFFHOOK", "IDLE"]), workers, events)
    supervisor.tick()
    assert len([event for event in events if event.startswith("CALL_SESSION_START")]) == 1
    supervisor.tick()

def test_idle_exits_worker_and_next_fresh_call_gets_one_new_worker(tmp_path):
    events = []
    workers = [_LifecycleWorker(), _LifecycleWorker()]
    supervisor = _supervisor_for_test(
        tmp_path,
        iter(["OFFHOOK", "IDLE", "OFFHOOK", "IDLE"]),
        workers,
        events,
    )

    for _ in range(4):
        supervisor.tick()

    starts = [event for event in events if event.startswith("CALL_SESSION_ID")]
    assert len(starts) == 2
    assert starts[0] != starts[1]
    assert events.count("CALL_WORKER_EXITED") == 2
    assert supervisor.worker is None
    assert supervisor._worker_lock is None
    assert (tmp_path / "call-worker.lock").read_text() == ""
    assert not workers


def test_call_session_supervisor_keeps_session_during_unknown_grace():
    states = iter(["OFFHOOK", "UNKNOWN", "UNKNOWN", "OFFHOOK", "IDLE"])
    events = []
    worker = type("Worker", (), {"poll": lambda self: None, "terminate": lambda self: None, "wait": lambda self, timeout=None: None})()
    supervisor = CallSessionSupervisor(
        phone_state=lambda: next(states),
        hfp_nodes=lambda: True,
        worker_factory=lambda: worker,
        mute_sinks=lambda: events.append("mute"),
        restore_sinks=lambda: events.append("restore"),
        unknown_grace_seconds=0,
        log=events.append,
    )

    for _ in range(5):
        supervisor.tick()

    assert events.count("CALL_SESSION_START") == 1
    assert events.count("CALL_SESSION_END") == 1


def test_unknown_without_hfp_does_not_end_call_before_authoritative_idle(tmp_path):
    events = []
    worker = _LifecycleWorker()
    states = iter(["OFFHOOK", "UNKNOWN", "UNKNOWN", "IDLE"])
    supervisor = CallSessionSupervisor(
        phone_state=lambda: next(states),
        hfp_nodes=lambda: False,
        worker_factory=lambda: worker,
        mute_sinks=lambda: events.append("mute"),
        restore_sinks=lambda: events.append("restore"),
        unknown_grace_seconds=0,
        log=events.append,
        worker_lock_path=tmp_path / "call-worker.lock",
    )

    for _ in range(4):
        supervisor.tick()

    assert events.count("CALL_SESSION_START") == 1
    assert events.count("CALL_SESSION_END") == 1
    assert events.count("CALL_STATE_UNKNOWN_HFP_ABSENT_PRESERVED") == 2


def test_call_session_idle_stops_worker_and_restores_everything_once():
    states = iter(["OFFHOOK", "IDLE", "IDLE"])
    events = []
    calls = {"restore": 0, "model": 0}

    class Worker:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            self.returncode = -15

    worker = Worker()
    supervisor = CallSessionSupervisor(
        phone_state=lambda: next(states),
        hfp_nodes=lambda: True,
        worker_factory=lambda: worker,
        mute_sinks=lambda: None,
        restore_sinks=lambda: calls.__setitem__("restore", calls["restore"] + 1),
        on_end=lambda: calls.__setitem__("model", calls["model"] + 1),
        log=events.append,
    )

    supervisor.tick()
    supervisor.tick()
    supervisor.tick()

    assert supervisor.worker is None
    assert calls == {"restore": 1, "model": 1}
    assert events.count("CALL_WORKER_STOP_REQUESTED") == 1
    assert events.count("CALL_WORKER_EXITED") == 1
    assert events.count("HOST_SINKS_RESTORED") == 1
    assert events.count("MODEL_RESTORE_COMPLETE") == 1
    assert events.count("CALL_SESSION_END") == 1


def test_call_audio_coordinator_uses_line_capture_and_physical_lineout_playback():
    coordinator = CallAudioCoordinator(
        CallAudioConfig(
            capture_device="hw:0,0",
            playback_device="hw:CARD=PCH,DEV=0",
            sample_rate=48000,
            channels=2,
            turn_seconds=4,
        )
    )

    assert coordinator.capture_command("/tmp/turn.wav") == [
        "arecord", "-D", "hw:0,0", "-f", "S16_LE", "-r", "48000",
        "-c", "2", "-d", "4", "/tmp/turn.wav",
    ]
    assert coordinator.playback_command("/tmp/reply.wav") == [
        "aplay", "-D", "hw:CARD=PCH,DEV=0", "/tmp/reply.wav",
    ]


def test_analog_call_routes_capture_mux_to_line():
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:4] == ["amixer", "-c", "0", "sget"]:
            return subprocess.CompletedProcess(
                command, 0,
                stdout="Item0: 'Front Mic'\nItem0: 'Rear Mic'\n",
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    CallAudioCoordinator(CallAudioConfig(), run=fake_run)._configure_analog_input()
    assert commands == [
        ["amixer", "-c", "0", "sget", "Input Source"],
        ["amixer", "-c", "0", "sset", "Input Source,0", "Line"],
        ["amixer", "-c", "0", "sset", "Input Source,1", "Line"],
    ]


def test_bluetooth_backend_uses_pulse_call_audio_devices():
    pipewire = json.dumps([
        {"type": "PipeWire:Interface:Node", "info": {"props": {
            "node.name": "bluez_input.11_22_33_44_55_66.0"
        }}},
        {"type": "PipeWire:Interface:Node", "info": {"props": {
            "node.name": "bluez_output.11_22_33_44_55_66.1"
        }}},
    ])

    def fake_run(command, **kwargs):
        if command == ["pw-dump"]:
            return subprocess.CompletedProcess(command, 0, stdout=pipewire, stderr="")
        raise AssertionError(command)

    coordinator = CallAudioCoordinator(
        CallAudioConfig(audio_backend="bluetooth", sample_rate=16000, channels=1),
        run=fake_run,
    )
    assert coordinator.capture_command("/tmp/turn.wav") == [
        "pw-record", "--target", "bluez_input.11_22_33_44_55_66.0",
        "--rate", "16000", "--channels", "1", "--format", "s16",
        "--raw", "-",
    ]
    assert coordinator.playback_command("/tmp/reply.wav") == [
        "pw-play", "--target", "bluez_output.11_22_33_44_55_66.1",
        "/tmp/reply.wav",
    ]


def test_bluetooth_capture_does_not_pipe_pw_record_stderr(monkeypatch, tmp_path):
    captured = {}

    class FakeStream:
        def __init__(self, chunks):
            self.chunks = iter(chunks)

        def read(self, _size):
            return next(self.chunks, b"")

        def close(self):
            pass

    class FakeProcess:
        def __init__(self):
            self.stdout = FakeStream([
                (5000).to_bytes(2, "little", signed=True) * 320 * 20,
                b"\x00" * 320 * 2 * 40,
            ])
            self.stderr = None
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, **_kwargs):
            return self.returncode

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    class FakeSelector:
        def register(self, *_args):
            pass

        def select(self, **_kwargs):
            return [object()]

        def close(self):
            pass

    monkeypatch.setattr(call_audio_loop.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(call_audio_loop.selectors, "DefaultSelector", FakeSelector)
    coordinator = call_audio_loop.CallAudioCoordinator(
        call_audio_loop.CallAudioConfig(
            audio_backend="bluetooth",
            sample_rate=16000,
            channels=1,
            trailing_silence_seconds=0.1,
            post_roll_seconds=0.05,
        ),
        phone_state=lambda: "OFFHOOK",
    )
    coordinator._bluetooth_input = "bluez_input.11_22_33_44_55_66.0"

    assert coordinator._capture_utterance(str(tmp_path / "turn.wav")) is True
    assert captured["stderr"] is subprocess.DEVNULL
    assert (tmp_path / "turn.wav").is_file()


def test_bluetooth_waits_for_both_call_nodes_and_logs_ready():
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == ["pw-dump"]:
            return subprocess.CompletedProcess(
                command, 0,
                stdout=json.dumps([
                    {"type": "PipeWire:Interface:Node", "info": {"props": {
                        "node.name": "bluez_input.11_22_33_44_55_66.0"
                    }}},
                    {"type": "PipeWire:Interface:Node", "info": {"props": {
                        "node.name": "bluez_output.11_22_33_44_55_66.1"
                    }}},
                ]),
                stderr="",
            )
        raise AssertionError(command)

    coordinator = CallAudioCoordinator(
        CallAudioConfig(audio_backend="bluetooth", start_wait_seconds=0.1),
        run=fake_run,
    )
    assert coordinator.wait_for_bluetooth_nodes() == (
        "bluez_input.11_22_33_44_55_66.0",
        "bluez_output.11_22_33_44_55_66.1",
    )
    assert calls == [["pw-dump"], ["pw-dump"]]


def test_bluetooth_nodes_can_appear_after_offhook(monkeypatch):
    calls = []
    snapshots = [
        [],
        [],
        [
            {"type": "PipeWire:Interface:Node", "info": {"props": {
                "node.name": "bluez_input.C4_EF_3D_58_03_28.0"
            }}},
            {"type": "PipeWire:Interface:Node", "info": {"props": {
                "node.name": "bluez_output.C4_EF_3D_58_03_28.1"
            }}},
        ],
        [
            {"type": "PipeWire:Interface:Node", "info": {"props": {
                "node.name": "bluez_input.C4_EF_3D_58_03_28.0"
            }}},
            {"type": "PipeWire:Interface:Node", "info": {"props": {
                "node.name": "bluez_output.C4_EF_3D_58_03_28.1"
            }}},
        ],
    ]

    def fake_run(command, **kwargs):
        calls.append(command)
        snapshot = snapshots.pop(0)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(snapshot), stderr="")

    monkeypatch.setattr(call_audio_loop.time, "sleep", lambda _seconds: None)
    coordinator = CallAudioCoordinator(
        CallAudioConfig(audio_backend="bluetooth", start_wait_seconds=1, poll_seconds=0),
        run=fake_run,
    )

    assert coordinator.wait_for_bluetooth_nodes() == (
        "bluez_input.C4_EF_3D_58_03_28.0",
        "bluez_output.C4_EF_3D_58_03_28.1",
    )
    assert calls == [["pw-dump"], ["pw-dump"], ["pw-dump"], ["pw-dump"]]


def test_bluetooth_worker_discovers_nodes_after_offhook(monkeypatch):
    states = iter(["OFFHOOK", "OFFHOOK", "OFFHOOK", "IDLE", "IDLE"])
    coordinator = CallAudioCoordinator(
        CallAudioConfig(audio_backend="bluetooth", manage_host_sinks=False),
        phone_state=lambda: next(states),
    )
    discovered = []

    monkeypatch.setattr(
        coordinator,
        "wait_for_bluetooth_nodes",
        lambda: discovered.append("wait") or (
            "bluez_input.C4_EF_3D_58_03_28.0",
            "bluez_output.C4_EF_3D_58_03_28.1",
        ),
    )
    monkeypatch.setattr(coordinator, "isolate_bluetooth_output", lambda: None)
    monkeypatch.setattr(coordinator, "_stop_voice_session", lambda aborted: None)

    assert coordinator.run_session() == 0
    assert discovered == ["wait"]


def test_bluetooth_node_timeout_is_bounded_and_logs_route_failure(monkeypatch):
    clock = [0.0]

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    def fake_monotonic():
        value = clock[0]
        clock[0] += 0.2
        return value

    monkeypatch.setattr(call_audio_loop.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(call_audio_loop.time, "sleep", lambda _seconds: None)
    coordinator = CallAudioCoordinator(
        CallAudioConfig(audio_backend="bluetooth", start_wait_seconds=0.1, poll_seconds=0),
        run=fake_run,
    )

    with patch.object(call_audio_loop.LOG, "error") as error:
        try:
            coordinator.wait_for_bluetooth_nodes()
        except RuntimeError as exc:
            assert "Bluetooth call" in str(exc)
        else:
            raise AssertionError("expected bounded HFP discovery to fail")

    error.assert_called_once()
    assert error.call_args.args[0] == "HFP_ROUTE_TIMEOUT wait_seconds=%.1f reason=%s"


def test_bluetooth_isolates_all_existing_inputs_to_output():
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["pw-link", "-lI"]:
            return subprocess.CompletedProcess(
                command, 0,
                stdout=(
                    "40 alsa_input.usb-Blue_Snowball:capture_MONO\n"
                    "  98 |-> 99 bluez_output.11_22_33_44_55_66:input_MONO\n"
                    "39 alsa_output.pci:monitor_FR\n"
                    "  52 |-> 100 bluez_output.11_22_33_44_55_66:input_FR\n"
                ),
                stderr="",
            )
        if command == ["pw-link", "-d", "98"] or command == ["pw-link", "-d", "52"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(command)

    coordinator = CallAudioCoordinator(
        CallAudioConfig(audio_backend="bluetooth"),
        run=fake_run,
    )
    coordinator.isolate_bluetooth_output(
        "bluez_output.11_22_33_44_55_66"
    )
    assert commands == [
        ["pw-link", "-lI"],
        ["pw-link", "-d", "98"],
        ["pw-link", "-d", "52"],
    ]


def test_bluetooth_disconnects_caller_audio_from_host_speakers():
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command == ["pactl", "list", "short", "sinks"]:
            return subprocess.CompletedProcess(
                command, 0,
                stdout=(
                    "10 alsa_output.pci-speakers PipeWire\n"
                    "11 bluez_output.11_22_33_44_55_66 PipeWire\n"
                ),
                stderr="",
            )
        if command == ["pactl", "set-sink-mute", "alsa_output.pci-speakers", "1"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(command)

    coordinator = CallAudioCoordinator(CallAudioConfig(audio_backend="bluetooth"), run=fake_run)
    coordinator.isolate_bluetooth_input("bluez_input.11_22_33_44_55_66")
    assert commands == [
        ["pactl", "list", "short", "sinks"],
        ["pactl", "set-sink-mute", "alsa_output.pci-speakers", "1"],
    ]


def test_audio_settings_are_profile_local_and_default_to_analog(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert load_audio_settings() == {"backend": "analog"}
    assert audio_settings_path() == tmp_path / "phone-audio.json"


def test_call_audio_coordinator_stops_when_phone_becomes_idle():
    coordinator = CallAudioCoordinator(CallAudioConfig())
    states = iter(["OFFHOOK", "IDLE"])
    coordinator.phone_state = lambda: next(states)

    assert coordinator.should_continue() is True
    assert coordinator.should_continue() is False


def test_call_audio_coordinator_ignores_transient_unknown_after_offhook():
    coordinator = CallAudioCoordinator(CallAudioConfig())
    states = iter(["OFFHOOK", "UNKNOWN", "IDLE"])
    coordinator.phone_state = lambda: next(states)

    assert coordinator.should_continue() is True
    assert coordinator.should_continue() is True
    assert coordinator.should_continue() is False


def test_call_audio_coordinator_keeps_host_sinks_muted_if_bounded_while_offhook():
    coordinator = CallAudioCoordinator(
        CallAudioConfig(audio_backend="analog"),
        run=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="Item0: 'Front Mic'\nItem0: 'Rear Mic'\n" if command[:4] == ["amixer", "-c", "0", "sget"] else "",
            stderr="",
        ),
    )
    coordinator.phone_state = lambda: "OFFHOOK"
    coordinator._muted_host_sinks = ["alsa_output.pci-speakers"]
    restored = []
    coordinator.restore_host_sinks = lambda: restored.append(True)

    assert coordinator.run_session(max_turns=0) == 0
    assert not restored
    assert coordinator._muted_host_sinks == ["alsa_output.pci-speakers"]


def test_acceptance_target_on_turn_two_does_not_end_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PHONE_RETAIN_AUDIO_DIR", str(tmp_path))

    class FakeGate:
        def __init__(self):
            self.status = SimpleNamespace(value="waiting")

        def start_audio_active(self):
            pass

        def check_timeout(self):
            return SimpleNamespace(status=self.status)

        def observe_turn(self, **kwargs):
            if kwargs["transcript"].startswith("Create a document"):
                self.status = SimpleNamespace(value="passed")
            return SimpleNamespace(status=self.status, classification="target")

    transcripts = iter([
        "All right, do you hear me?",
        "Create a document called phone save test.",
        "turn three",
        "turn four",
        "turn five",
        "turn six",
        "turn seven",
        "turn eight",
        "turn nine",
        "turn ten",
    ])
    coordinator = CallAudioCoordinator(
        CallAudioConfig(audio_backend="bluetooth", manage_host_sinks=False),
        acceptance_gate=FakeGate(),
    )
    asked = []
    capability_calls = []
    state_reads = 0

    def phone_state():
        nonlocal state_reads
        state_reads += 1
        if len(asked) >= 10:
            return "IDLE"
        if state_reads == 4:
            return "UNKNOWN"
        return "OFFHOOK"

    coordinator.phone_state = phone_state
    coordinator.wait_for_bluetooth_nodes = lambda: (
        "bluez_input.test.0",
        "bluez_output.test.1",
    )
    coordinator.isolate_bluetooth_input = lambda input_node=None: None
    coordinator.isolate_bluetooth_output = lambda output_node=None: None
    coordinator._capture_utterance = lambda output_path: True
    coordinator._rms = lambda path: 0.1
    coordinator._audio_metrics = lambda path: {"rms": 0.1, "peak": 0.2}
    coordinator._transcribe = lambda path: next(transcripts)
    def ask_hermes(transcript):
        asked.append(transcript)
        if transcript.startswith("Create a document"):
            capability_calls.append("draft_manage create")
        return "ok"

    coordinator._ask_hermes = ask_hermes
    coordinator._synthesize = lambda text, output_path: output_path
    coordinator._to_wav = lambda input_path, directory: input_path
    coordinator._play = lambda path: None
    coordinator._stop_voice_session = lambda aborted: None

    with patch.object(call_audio_loop.LOG, "info") as log_info:
        assert coordinator.run_session() == 10
    assert len(asked) == 10
    assert capability_calls == ["draft_manage create"]
    assert any(
        call.args == ("CALL_AUDIO_STOP_REASON reason=%s", "authoritative_idle")
        for call in log_info.call_args_list
    )


def test_explicit_max_turns_stops_with_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PHONE_RETAIN_AUDIO_DIR", str(tmp_path))
    coordinator = CallAudioCoordinator(
        CallAudioConfig(audio_backend="bluetooth", manage_host_sinks=False),
        phone_state=lambda: "OFFHOOK",
    )
    coordinator.wait_for_bluetooth_nodes = lambda: (
        "bluez_input.test.0",
        "bluez_output.test.1",
    )
    coordinator.isolate_bluetooth_input = lambda input_node=None: None
    coordinator.isolate_bluetooth_output = lambda output_node=None: None
    coordinator._capture_utterance = lambda output_path: True
    coordinator._rms = lambda path: 0.1
    coordinator._audio_metrics = lambda path: {"rms": 0.1, "peak": 0.2}
    coordinator._transcribe = lambda path: "one turn"
    coordinator._ask_hermes = lambda transcript: "ok"
    coordinator._synthesize = lambda text, output_path: output_path
    coordinator._to_wav = lambda input_path, directory: input_path
    coordinator._play = lambda path: None
    coordinator._stop_voice_session = lambda aborted: None

    with patch.object(call_audio_loop.LOG, "info") as log_info:
        assert coordinator.run_session(max_turns=1) == 1
    assert any(
        call.args == ("CALL_AUDIO_STOP_REASON reason=%s", "max_turns")
        for call in log_info.call_args_list
    )


def test_call_audio_coordinator_uses_external_rex_voice_session():
    events = []
    gate = object()
    received_gates = []

    class FakeRexVoiceSession:
        def __init__(self, **kwargs):
            received_gates.append(kwargs["acceptance_gate"])

        def start(self, observer=None):
            events.append("start")

        def prompt(self, message):
            events.append(("prompt", message))
            return "Created the requested file."

        def stop(self, aborted=False, launch_post_call=True):
            events.append(("stop", aborted, launch_post_call))
            return None

    coordinator = CallAudioCoordinator(
        CallAudioConfig(),
        voice_session_factory=FakeRexVoiceSession,
        acceptance_gate=gate,
    )
    assert coordinator._ask_hermes("create a file named test.txt") == "Created the requested file."
    assert coordinator._ask_hermes("append one line") == "Created the requested file."
    assert events == [
        "start",
        ("prompt", "create a file named test.txt"),
        ("prompt", "append one line"),
    ]
    assert received_gates == [gate]
    coordinator._stop_voice_session(aborted=False)
    assert events[-1] == ("stop", False, False)


def test_inbound_ringing_prepares_once_and_starts_worker_only_after_offhook(tmp_path):
    events = []
    workers = [_LifecycleWorker()]
    states = iter(["RINGING", "RINGING", "OFFHOOK", "IDLE"])
    supervisor = CallSessionSupervisor(
        phone_state=lambda: next(states),
        hfp_nodes=lambda: True,
        worker_factory=lambda: workers.pop(0),
        mute_sinks=lambda: events.append("mute"),
        restore_sinks=lambda: events.append("restore"),
        on_ringing=lambda: events.append("prepare-and-answer"),
        log=events.append,
        worker_lock_path=tmp_path / "call-worker.lock",
    )

    supervisor.tick()
    supervisor.tick()
    assert events == ["prepare-and-answer"]
    assert supervisor.worker is None
    supervisor.tick()
    assert events.count("prepare-and-answer") == 1
    assert events.count("CALL_SESSION_START") == 1
    supervisor.tick()
    assert events.count("restore") == 1


def test_inbound_caller_authorization_is_owner_only_and_fail_closed(tmp_path):
    relay = HermesPhoneRelay(owner="+15550100199", state_path=tmp_path / "state.json")

    with patch.object(relay, "_bridge", return_value='OK {"state":"RINGING","number":"+15550100199"}'):
        assert relay.inbound_caller_is_authorized()

    with patch.object(relay, "_bridge", return_value='OK {"state":"RINGING","number":"+14170000000"}'):
        assert not relay.inbound_caller_is_authorized()

    with patch.object(relay, "_bridge", return_value='OK {"state":"RINGING","number":""}'):
        assert not relay.inbound_caller_is_authorized()


def test_unauthorized_inbound_call_is_not_prepared_or_answered(tmp_path):
    relay = HermesPhoneRelay(owner="+15550100199", state_path=tmp_path / "state.json")
    with patch.object(relay, "_bridge", return_value='OK {"state":"RINGING","number":"+14170000000"}'):
        with patch("hermes_phone_relay.subprocess.run") as run:
            try:
                relay.prepare_inbound_call()
            except RuntimeError as exc:
                assert str(exc) == "inbound caller is not authorized"
            else:
                raise AssertionError("unauthorized caller was accepted")
    run.assert_not_called()


def test_inbound_answer_requires_offhook_and_uses_call_key_fallback(tmp_path):
    relay = HermesPhoneRelay(owner="+155****0199", state_path=tmp_path / "state.json", adb_serial="device-1")
    with patch.object(relay, "_bridge", return_value="OK answer requested"):
        with patch.object(relay, "_wait_for_call_state", side_effect=[False, True]):
            with patch("hermes_phone_relay.subprocess.run") as run:
                run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
                assert relay._answer_inbound_call()
    run.assert_called_once_with(
        [relay.adb, "-s", "device-1", "shell", "input", "keyevent", "KEYCODE_CALL"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_inbound_answer_does_not_claim_success_without_offhook(tmp_path):
    relay = HermesPhoneRelay(owner="+155****0199", state_path=tmp_path / "state.json")
    with patch.object(relay, "_bridge", return_value="OK answer requested"):
        with patch.object(relay, "_wait_for_call_state", return_value=False):
            with patch("hermes_phone_relay.subprocess.run") as run:
                run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="rejected")
                assert not relay._answer_inbound_call()
    assert run.call_args.args[0][-3:] == ["input", "keyevent", "KEYCODE_CALL"]


def test_inbound_answers_before_starting_model_preparation(tmp_path, monkeypatch):
    supervisor = tmp_path / "supervisor.sh"
    supervisor.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("REX_VOICE_SUPERVISOR", str(supervisor))
    relay = HermesPhoneRelay(owner="+155****0199", state_path=tmp_path / "state.json")
    events = []

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            events.append("model-ready")
            return "", ""

        def poll(self):
            return self.returncode

    with patch.object(relay, "inbound_caller_is_authorized", return_value=True):
        with patch.object(relay, "_answer_inbound_call", side_effect=lambda: events.append("answered") or True):
            with patch("hermes_phone_relay.subprocess.Popen", return_value=FakeProcess()) as popen:
                relay.prepare_inbound_call()
                thread = relay._inbound_prepare_thread
                assert thread is not None
                thread.join(timeout=2)

    assert events[0] == "answered"
    assert "model-ready" in events
    assert popen.call_args.args[0][-1] == "switch-to-gemma"
    assert json.loads(relay._inbound_readiness_path.read_text())["status"] == "ready"


def test_inbound_readiness_worker_waits_for_ready_marker_and_fails_closed(tmp_path):
    readiness = tmp_path / "readiness.json"
    coordinator = CallAudioCoordinator(
        CallAudioConfig(readiness_path=str(readiness), readiness_wait_seconds=0.2, poll_seconds=0.01),
        phone_state=lambda: "OFFHOOK",
    )
    readiness.write_text(json.dumps({"status": "ready"}))
    coordinator._wait_for_inbound_readiness()

    readiness.write_text(json.dumps({"status": "failed", "error": "load failed"}))
    try:
        coordinator._wait_for_inbound_readiness()
    except RuntimeError as exc:
        assert str(exc) == "load failed"
    else:
        raise AssertionError("failed readiness was accepted")


def test_inbound_audio_order_is_wait_prompt_then_readiness_then_ready_prompt(tmp_path):
    events = []
    coordinator = CallAudioCoordinator(
        CallAudioConfig(
            audio_backend="analog",
            startup_message_path="wait.wav",
            ready_message_path="ready.wav",
            readiness_path=str(tmp_path / "readiness.json"),
        ),
        phone_state=lambda: "OFFHOOK",
    )
    coordinator._configure_analog_input = lambda: None
    coordinator._play_startup_message = lambda: events.append("wait-prompt")
    coordinator._wait_for_inbound_readiness = lambda: events.append("readiness-confirmed")
    coordinator._ensure_voice_session = lambda: events.append("voice-session-loaded")
    coordinator._play_ready_message = lambda: events.append("ready-prompt")
    coordinator._stop_voice_session = lambda aborted: None

    assert coordinator.run_session(max_turns=0) == 0
    assert events == ["wait-prompt", "readiness-confirmed", "voice-session-loaded", "ready-prompt"]


def test_inbound_hangup_before_offhook_restores_model(tmp_path):
    events = []
    states = iter(["RINGING", "IDLE"])
    supervisor = CallSessionSupervisor(
        phone_state=lambda: next(states),
        hfp_nodes=lambda: False,
        worker_factory=lambda: _LifecycleWorker(),
        mute_sinks=lambda: None,
        restore_sinks=lambda: None,
        on_ringing=lambda: events.append("prepare"),
        on_end=lambda: events.append("restore-model"),
        log=events.append,
        worker_lock_path=tmp_path / "call-worker.lock",
    )
    supervisor.tick()
    supervisor.tick()
    assert events == ["prepare", "restore-model", "MODEL_RESTORE_COMPLETE"]


def test_inbound_call_waits_for_active_relay_operation_then_answers(tmp_path, monkeypatch):
    supervisor = tmp_path / "supervisor.sh"
    supervisor.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("REX_VOICE_SUPERVISOR", str(supervisor))
    relay = HermesPhoneRelay(owner="+15550100199", state_path=tmp_path / "state.json")
    relay._relay_operation_active = True
    with patch.object(relay, "inbound_caller_is_authorized", return_value=True), patch.object(
        relay, "_answer_inbound_call"
    ) as answer:
        assert relay.prepare_inbound_call() is False
        answer.assert_not_called()
        relay._relay_operation_active = False
        with patch("hermes_phone_relay.subprocess.Popen") as popen:
            popen.return_value = SimpleNamespace(poll=lambda: 0)
            assert relay.prepare_inbound_call() is True
        answer.assert_called_once()


def test_missed_owner_call_redials_only_after_relay_operation_finishes(tmp_path):
    relay = HermesPhoneRelay(owner="+15550100199", state_path=tmp_path / "state.json", adb="adb")
    relay._last_inbound_number = relay.owner
    relay._defer_missed_inbound_call()
    relay._relay_operation_active = True
    with patch("hermes_phone_relay.subprocess.run") as run:
        relay._redial_deferred_call()
        run.assert_not_called()
        relay._relay_operation_active = False
        run.return_value = SimpleNamespace(returncode=0, stderr="")
        relay._redial_deferred_call()
    assert run.call_args.args[0][-7:] == ["am", "start", "-W", "-a", "android.intent.action.CALL", "-d", f"tel:{relay.owner}"]
    assert run.call_args.args[0][-8] == "shell"
    assert relay._deferred_inbound_number is None


def test_post_call_worker_launch_waits_for_verified_qwen_and_is_idempotent(tmp_path, monkeypatch):
    supervisor = tmp_path / "model_supervisor.sh"
    supervisor.write_text("#!/bin/sh\n")
    events = []
    ready_status = {
        "state_file": {"state": "QWEN_READY", "active_model": "qwen"},
        "lock": "free",
        "qwen": {"container": "running", "health": True, "model": "/models/Qwen3.8-27B-UD-Q4_K_XL.gguf"},
        "gemma": {"health": False},
    }

    def fake_run(command, **_kwargs):
        events.append(command[-1])
        if command[-1] == "status":
            return SimpleNamespace(returncode=0, stdout=json.dumps(ready_status), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    coordinator = CallAudioCoordinator(CallAudioConfig(), run=fake_run)
    monkeypatch.setenv("REX_VOICE_SUPERVISOR", str(supervisor))
    assert coordinator._restore_normal_model_for_post_call() == (
        "/models/Qwen3.8-27B-UD-Q4_K_XL.gguf", "Qwen 27B"
    )
    assert events == ["switch-to-qwen", "status"]

    launched = []
    class Session:
        def launch_post_call_worker(self, job, *, model, provider):
            launched.append((job["job_id"], model, provider))

    coordinator._release_deferred_post_call(
        (Session(), {"job_id": "post-call-voice-1", "session_id": "voice-1"}), "IDLE"
    )
    assert launched == [("post-call-voice-1", "/models/Qwen3.8-27B-UD-Q4_K_XL.gguf", "Qwen 27B")]


@pytest.mark.skipif(importlib.util.find_spec("rex_voice_v1") is None, reason="optional Rex Voice runtime is not installed")
def test_post_call_worker_stays_blocked_when_qwen_restore_fails(tmp_path, monkeypatch):
    supervisor = tmp_path / "model_supervisor.sh"
    supervisor.write_text("#!/bin/sh\n")

    def failed_run(command, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="restore failed")

    class Store:
        root = tmp_path

    class Backend:
        store = Store()

    class Session:
        backend = Backend()
        def launch_post_call_worker(self, *args, **kwargs):
            raise AssertionError("worker must not launch while Qwen is unavailable")

    from rex_voice_v1.post_call import PostCallQueue
    queue = PostCallQueue(tmp_path / "post-call")
    queue.enqueue("voice-2", {"call_record": {"status": "completed"}})
    coordinator = CallAudioCoordinator(CallAudioConfig(), run=failed_run)
    monkeypatch.setenv("REX_VOICE_SUPERVISOR", str(supervisor))
    coordinator._release_deferred_post_call(
        (Session(), {"job_id": "post-call-voice-2", "session_id": "voice-2"}), "IDLE"
    )
    job = queue.load("voice-2")
    assert job["status"] == "queued"
    assert job["execution_gate"] == {"status": "blocked", "reason": "normal_model_not_ready", "updated_at": job["execution_gate"]["updated_at"]}


def test_post_call_worker_stays_blocked_while_call_is_not_idle():
    launched = []
    class Session:
        def launch_post_call_worker(self, *args, **kwargs):
            launched.append(True)
    coordinator = CallAudioCoordinator(CallAudioConfig())
    blocked = []
    coordinator._mark_post_call_blocked = lambda _session, job, reason: blocked.append((job["job_id"], reason))
    coordinator._release_deferred_post_call(
        (Session(), {"job_id": "post-call-voice-3", "session_id": "voice-3"}), "OFFHOOK"
    )
    assert blocked == [("post-call-voice-3", "call_not_idle")]
    assert launched == []
