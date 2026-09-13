# Agent instructions: Hermes Phone Bridge

## Canonical source and ownership

- Canonical phone-bridge source: `/home/math3matica/hermes-phone-bridge`.
- Its host relay owns phone authorization, dialing, call state, hangup, queueing, and host worker supervision.
- The canonical Call Assistant runtime is `/home/math3matica/hermes-call-assistant`.
- The installed Call Assistant plugin is `~/.hermes/plugins/hermes-call-assistant`.
- `/home/math3matica/hermes` is not the canonical Call Assistant source and must not be selected as the production `REX_VOICE_ROOT`.

Read the Call Assistant `docs/OPERATIONS.md` and `docs/OWNERSHIP.md` when changing the cross-repository call path.

## Runtime rules

1. Pass explicit `HERMES_ROOT`, `REX_VOICE_ROOT`, `HERMES_HOME`, and `HERMES_CALL_WORKER_SESSION_ID` to worker subprocesses.
2. Never depend on the relay's working directory or ambient `PYTHONPATH` for imports.
3. A relay or Hermes process that was already running before an edit is stale until restarted or replaced by a fresh production process.
4. The call worker must start only after the call and audio prerequisites pass; one worker owns one active call.
5. Do not redial or retry a failed physical call during diagnosis without explicit authorization.
6. Treat Bluetooth profile/node availability, capture, STT, model response, TTS, and playback as separate evidence layers.
7. Preserve raw logs and call artifacts, including failed runs. Never clean broadly.

## Diagnostic markers

Use the first missing marker to locate the failure:

`CALL_AUDIO_ACTIVE -> CAPTURED -> TRANSCRIPT -> RESPONSE -> TTS_READY -> playback`

`CAPTURED` plus `TRANSCRIBING_EMPTY` is an input/STT problem, not proof that TTS failed. `TTS_READY` is not proof that sound reached the phone. A paired Bluetooth device or Bluetooth card without `bluez_input...` and `bluez_output...` nodes is not a usable HFP call path.
