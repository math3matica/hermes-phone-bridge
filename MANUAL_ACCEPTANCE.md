# Manual acceptance (explicitly gated)

This checklist is **not run by CI** and was not run for the standalone hygiene
checkpoint. It requires explicit operator authorization because it can send
messages, place/end calls, alter audio routing, install software, or consume
inbox data.

## Preconditions

- [ ] Confirm the target device, owner number, and test window.
- [ ] Preserve and verify the frozen APK checksum before any comparison.
- [ ] Confirm the test build is separately named and signed; never overwrite the
      frozen APK or its app data.
- [ ] Record ADB serial, Android version, package/version, and relevant routes.
- [ ] Confirm backups and an operator-authorized rollback plan.

## SMS transport

- [ ] Verify `PING` through the supported ADB shell/netcat path.
- [ ] Send one explicitly authorized test SMS and record the raw response.
- [ ] Receive and inspect one test SMS; verify `READ_INBOX` consume semantics.
- [ ] Confirm owner filtering and relay queue persistence independently.

## Calling and audio (separate gates)

- [ ] Verify TTS/model readiness before dialing.
- [ ] Place a single authorized owner-only call; intent acceptance is not call
      proof—poll `CALL_STATUS` and record terminal `IDLE`.
- [ ] For analog audio, record route, mixer source, capture metrics, and WAV.
- [ ] For Bluetooth, verify paired/trusted HFP/HSP input and output nodes after
      `OFFHOOK`; do not substitute A2DP or host-default speakers.
- [ ] Hang up, verify `IDLE`, cleanup, and model restoration.

## Release decision

- [ ] Attach raw device measurements and timestamps.
- [ ] Keep physical evidence separate from mocked tests and source inspection.
- [ ] Only publish a newly built APK after an authorized build/sign/install
      review; this repository's current source is otherwise experimental.
