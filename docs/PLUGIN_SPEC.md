# Hermes Phone Bridge Plugin Specification

**Canonical repository:** `/home/math3matica/hermes-phone-bridge`
**Installed plugin:** `plugin/hermes-phone/`
**Purpose:** owner-authorized Android/ADB phone transport, SMS relay, cellular call control, and selectable call-audio integration

This is the governing feature and flow specification for Hermes Phone Bridge. Read it before changing the plugin, relay, Android transport, call worker, or audio path. It describes current ownership and evidence boundaries. A connected device or accepted Android intent is not equivalent to a successful message, call, or conversation.

## 1. Product definition

Hermes Phone Bridge connects Hermes to one configured owner phone through a deliberately small Android SMS transport and a host-side relay. The relay owns authorization, polling, durable queueing, Hermes/session continuity, and reply delivery. For calls, it also supervises the host audio worker and coordinates the external Call Assistant runtime.

The Android application is a transport component, not the conversational agent. It must remain small and must not become a second queue, model runtime, or unrestricted command executor.

## 2. Architecture and ownership

```text
Android Rex SMS transport
  <-> ADB selected by HERMES_PHONE_ADB / serial
  <-> host relay :9999
       |-- singleton relay lock
       |-- owner authorization
       |-- SMS inbox polling/deduplication/queue
       |-- Hermes session continuity/reply formatting
       |-- call supervisor
       v
Phone Bridge plugin tools / commands

Authorized cellular call
  -> Telecom/ADB call state
  -> HFP/analog prerequisites
  -> exactly one host call_audio_loop worker
  -> capture -> STT -> Call Assistant/Gemma -> TTS -> targeted playback
  -> hangup/IDLE -> session finalization and model restoration
```

| Component | Owns | Does not own |
|---|---|---|
| `plugin/hermes-phone/__init__.py` | native status/SMS/audio/call/hangup tool surfaces and ADB command construction | inbound polling/session continuity daemon ownership |
| Android `SmsBridgeService` | bounded device-side SMS transport and device commands | Hermes routing, model, durable host queue |
| `hermes_phone_relay.py` | singleton relay, owner authorization, polling, dedupe, queue, continuity, call supervisor | arbitrary second relay or hidden redial |
| `CallSessionSupervisor` | one worker lease per active call, terminal cleanup and callbacks | physical audio implementation details |
| `call_audio_loop.py` | host capture, utterance detection, STT, model prompt, TTS, playback | dialing and Telecom ownership |
| Phone/Telecom/ADB | phone command and call state | conversational success |
| Call Assistant | specialized model session, capability bridge, assignments, post-call handoff | ADB, HFP, physical playback |
| WirePlumber/ALSA/Bluetooth | physical audio route | model intent or SMS queue |

## 3. Public plugin surfaces

The plugin manifest exposes:

- `phone_status`: safe relay/device reachability status;
- `phone_sms_send`: owner-authorized SMS send;
- `phone_audio_test`: bounded audio transport test;
- `phone_audio_settings`: inspect/set `analog` or `bluetooth` backend;
- `/phone-audio`: audio backend command;
- `phone_call`: owner-only outbound call preparation and dialing;
- `phone_call_status`: independent call-state query;
- `phone_hangup`: hang up and independently wait for `IDLE`.

External side effects are explicitly marked and must remain owner-gated. The phone-call tool accepts only the configured owner target; arbitrary conversational numbers are not a supported capability.

The plugin may use `HERMES_PHONE_ADB` and `HERMES_PHONE_ADB_SERIAL`. When configured, every ADB invocation must use the selected binary and pinned serial. Do not silently fall back to a different device during live operation.

## 4. SMS flow

```text
Android inbox
  -> relay polling through ADB/nc bridge
  -> parse and normalize messages
  -> message_id = hash(address, timestamp, body)
  -> owner authorization
  -> deduplicate against durable state
  -> invoke Hermes session continuity
  -> extract and bound user-facing reply
  -> send reply through Android bridge
  -> persist pending/completed state
```

The relay filters internal review/control lines from SMS replies and bounds reply length. Queue state is durable and atomically written. A relay restart must not resend already acknowledged messages or lose pending work.

Only the configured owner is authorized to control the relay through SMS. Malformed bridge responses, invalid phone numbers, invalid inbox JSON, and transport errors are explicit failures.

## 5. Outbound call flow

```text
authorized phone_call request
  -> validate target equals configured owner
  -> verify Kokoro/TTS readiness
  -> switch external model supervisor to Gemma
  -> verify Gemma health/readiness
  -> issue ADB ACTION_CALL intent
  -> independently poll DIALING/RINGING/OFFHOOK/IDLE
  -> relay CallSessionSupervisor accepts one call worker
```

If preparation fails, no call intent should be issued. If dialing fails after preparation, attempt bounded model restoration and preserve the exact failure. `Status: ok` from Android proves intent acceptance only; it does not prove that a call connected or that conversation audio worked.

Physical call authorization is required for every real call, redial, retry, or hangup operation. Tests use injected runners and state readers instead of placing calls.

## 6. Call worker and audio flow

The expected acceptance chain is:

```text
HFP/analog prerequisites
  -> CALL_AUDIO_ACTIVE
  -> CAPTURED
  -> TRANSCRIPT
  -> RESPONSE or native tool call
  -> TTS_READY
  -> targeted physical playback
```

`call_audio_loop.py` owns the host-side half-duplex media path:

1. load explicit audio backend settings;
2. select analog or Bluetooth format/devices;
3. verify and configure the intended capture/playback route;
4. wait for active call state;
5. capture frames with bounded speech detection, pre-roll, trailing silence, and maximum utterance limits;
6. write bounded evidence and invoke Hermes STT;
7. send the transcript to one reused Call Assistant session;
8. synthesize the response through configured TTS;
9. play only to the physical phone route;
10. repeat until cancellation, hangup, terminal `IDLE`, or bounded failure.

For analog, playback must target the physical Line-Out ALSA path feeding the splitter, not the host default speaker sink. Capture must use the intended Line-In boundary. For Bluetooth, require negotiated HFP/HSP endpoints, specifically both `bluez_input...` and `bluez_output...` nodes; pairing or an `audio-gateway` card alone is insufficient.

A `CAPTURED` event with `TRANSCRIBING_EMPTY` is a receive/STT failure. A generated WAV or `TTS_READY` is not proof that the phone received sound.

## 7. Relay and worker ownership

There are two independent singleton boundaries:

- relay instance lock: exactly one authoritative `hermes_phone_relay.py` process;
- call worker lock: exactly one audio worker for one active call.

The relay supervisor is the launch authority. Do not start a second relay manually while the systemd user service owns the process. A stale process must be identified and replaced through the authoritative service boundary, not worked around by launching duplicates.

The worker receives explicit environment roots, including:

- `HERMES_ROOT`;
- `REX_VOICE_ROOT=/home/math3matica/hermes-call-assistant`;
- `HERMES_HOME`;
- `HERMES_CALL_WORKER_SESSION_ID`;
- `HERMES_PHONE_ADB` and `HERMES_PHONE_ADB_SERIAL` where applicable.

No production behavior may depend on the relay working directory or ambient `PYTHONPATH`.

## 8. Hangup and terminal cleanup

`phone_hangup` sends one bounded `HANGUP` bridge command and independently waits for authoritative `IDLE`. It must not report success based only on a returned command string. If the call is already transitioning or idle, preserve the exact rejection and verify state before classifying the result.

The supervisor must:

1. stop the Call Assistant/audio worker before bridge teardown;
2. observe terminal call state;
3. finalize or abort the Call Assistant session;
4. enqueue post-call work where configured;
5. restore the prior model through the configured supervisor;
6. preserve restoration failures rather than hiding them.

No automatic redial belongs in failure recovery. Any retry is a new externally authorized action.

## 9. Configuration and artifacts

Important configuration includes:

- owner number and ADB path/serial;
- relay port and systemd service environment;
- audio backend and device route settings;
- model supervisor and readiness endpoints;
- explicit Call Assistant/Hermes roots;
- relay state and call-audio evidence paths.

Keep source, installed plugin, live service, runtime cache, logs, WAVs, and Android artifacts as separate boundaries. Preserve failed call evidence, including relay logs, audio logs, ADB version/device output, WirePlumber logs, and lifecycle markers.

## 10. Failure isolation

| First missing evidence | Investigate first |
|---|---|
| no ADB/device reachability | selected ADB binary, serial, USB/device state |
| no bridge response | Android listener, ADB shell, relay port |
| no `DIALING`/`OFFHOOK` | Telecom/ADB invocation and phone state |
| no HFP endpoints | Bluetooth profile/backend/WirePlumber; stop before STT/TTS changes |
| `CALL_AUDIO_ACTIVE` but no `CAPTURED` | capture device, mixer, worker startup |
| `CAPTURED` but no `TRANSCRIPT` | STT import/model/VAD/input quality |
| transcript but no `RESPONSE` | Call Assistant session/model/extension |
| response but no `TTS_READY` | TTS provider/readiness |
| TTS ready but no physical speech | playback target/mixer/HFP sink/route |
| call not `IDLE` after hangup | Telecom state and bridge cleanup |

Do not make architectural changes before locating the first missing marker.

## 11. Change protocol

Before editing:

1. Read this specification and `AGENTS.md`.
2. For cross-repository call work, read the Call Assistant `docs/PLUGIN_DESIGN.md`, `docs/OPERATIONS.md`, and `docs/OWNERSHIP.md`.
3. Inspect the active systemd unit, process identity, selected ADB path/serial, and repository status.
4. Trace the relevant command from plugin surface through relay/worker/device boundary.
5. Decide whether the change is control-plane, telephony, audio, model/session, or evidence handling.
6. If an installed plugin symlink or service definition references this repository, treat it as a live source boundary. Migrate it to an immutable snapshot or develop in a detached staging worktree; do not assume a source edit is isolated.

While editing:

- preserve uncommitted work and failed evidence;
- keep one relay and one worker owner;
- keep ADB control separate from audio transport;
- keep phone authorization explicit;
- do not retry physical actions during diagnosis without authorization;
- do not treat connection, intent acceptance, or TTS file creation as conversation success;
- use injected runners/state readers for tests;
- update protocol/configuration documentation when commands or ownership change.

After editing:

- run focused bridge/plugin/audio tests in the isolated environment;
- run compile/syntax checks;
- stage the complete repository through Hermes Change Control with install subdirectory `plugin/hermes-phone`; review its digest before requesting deployment approval;
- run a fresh non-mutating status probe against the real bridge when applicable;
- verify the installed plugin and live service separately;
- for authorized physical calls, capture fresh call-state, HFP/audio-route, lifecycle, and human-observed playback evidence;
- report setup, telephony, receive/STT, model, TTS, and playback evidence separately.

## 12. Non-regression invariants

- One authoritative relay process.
- One worker per active call.
- Explicit ADB path and serial in production worker environments.
- Owner-only external phone actions.
- No hidden redial/retry.
- Android remains a bounded transport.
- Call Assistant remains a separate runtime.
- Hermes `/voice` remains independent.
- HFP endpoint readiness is distinct from Bluetooth pairing.
- `CALL_AUDIO_ACTIVE -> CAPTURED -> TRANSCRIPT -> RESPONSE -> TTS_READY -> playback` remains the acceptance chain.
- Failed physical evidence is preserved.
- Canonical source, immutable snapshot, installed plugin, service process, Android state, and physical behavior remain separately approved and separately verified boundaries.
