import json
from pathlib import Path

from physical_success_gate import is_live_success_statement, write_live_success_marker


def test_live_success_accepts_natural_confirmations():
    assert is_live_success_statement("Rex, it's working now. Stop the recursive build.")
    assert is_live_success_statement("This is working. You can stop the recursive build.")
    assert is_live_success_statement("Okay Rex, this works. Stop the build.")


def test_live_success_rejects_referential_or_historical_text():
    assert not is_live_success_statement("Say this is working and stop the recursive build later.")
    assert not is_live_success_statement("I said earlier that this was working; stop the recursive build.")
    assert not is_live_success_statement("This is not working. Do not stop the recursive build.")


def test_marker_requires_live_offhook_sessions_and_records_wav(tmp_path: Path):
    wav = tmp_path / "confirm.wav"
    wav.write_bytes(b"wav")
    marker = tmp_path / "rex-voice-success"
    assert write_live_success_marker(
        transcript="Rex, this is working. Stop the recursive build.",
        wav_path=str(wav),
        turn=4,
        phone_state="IDLE",
        call_session_id="call-test",
        rex_session_id="rex-voice-test",
        marker=marker,
    ) is None
    assert not marker.exists()
    result = write_live_success_marker(
        transcript="Rex, this is working. Stop the recursive build.",
        wav_path=str(wav),
        turn=4,
        phone_state="OFFHOOK",
        call_session_id="call-test",
        rex_session_id="rex-voice-test",
        marker=marker,
    )
    assert result == marker
    payload = json.loads(marker.read_text())
    assert payload["source"] == "live_stt_offhook_rex_voice_session"
    assert payload["call_session_id"] == "call-test"
    assert payload["rex_session_id"] == "rex-voice-test"
    assert payload["wav_path"] == str(wav.resolve())
