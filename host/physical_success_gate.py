#!/usr/bin/env python3
"""Live physical-call success marker for Rex Voice."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_MARKER = Path.home() / ".hermes" / "cache" / "rex-voice-success"
_WORDS = re.compile(r"\b(?:rex[,.!? ]+)?(?:this is |it(?:'s| is) )?work(?:s|ing)(?: now)?\b", re.IGNORECASE)
_STOP = re.compile(r"\bstop(?: the)?(?: recursive)? build\b|\bstop(?: the)? build\b", re.IGNORECASE)
_BLOCKED = re.compile(r"\b(?:not\s+work(?:s|ing)?|do\s+not\s+stop|don't\s+stop|say|said|saying|example|later|yesterday|earlier|quoted|quote|if I)\b", re.IGNORECASE)


def is_live_success_statement(transcript: str) -> bool:
    """Match only a direct confirmation plus a stop-build instruction."""
    text = " ".join(transcript.split())
    return bool(text and not _BLOCKED.search(text) and _WORDS.search(text) and _STOP.search(text))


def write_live_success_marker(
    *,
    transcript: str,
    wav_path: str,
    turn: int,
    phone_state: str,
    call_session_id: str,
    rex_session_id: str,
    marker: Path = _MARKER,
) -> Path | None:
    """Persist proof only while the real supervised call is OFFHOOK."""
    if phone_state != "OFFHOOK" or not call_session_id or not rex_session_id:
        return None
    if not is_live_success_statement(transcript):
        return None
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema": "rex-voice-live-success-v1",
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "phone_state": phone_state,
        "call_session_id": call_session_id,
        "rex_session_id": rex_session_id,
        "turn": turn,
        "transcript": transcript,
        "wav_path": str(Path(wav_path).resolve()),
        "source": "live_stt_offhook_rex_voice_session",
    }
    fd, temporary = tempfile.mkstemp(prefix=f".{marker.name}.", dir=marker.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return marker
