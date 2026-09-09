# Configuration

The plugin and relay are profile-local. Keep the environment file outside the
repository (the supplied `examples/hermes-phone.env.example` is sanitized).

| Variable | Required | Purpose |
|---|---:|---|
| `HERMES_HOME` | Recommended | Hermes profile root; relay state and audio selection live below it |
| `HERMES_PHONE_OWNER` | For relay/calls | Single allowed owner number |
| `HERMES_PHONE_ADB` | No | Explicit `adb` executable; otherwise PATH lookup |
| `HERMES_PHONE_ADB_SERIAL` | No | Select one connected device |
| `HERMES_PHONE_MODEL_SUPERVISOR` | For guarded calls | Private supervisor script for model restoration |

`phone-audio.json` is written under `HERMES_HOME` and contains only
`{"backend":"analog"}` or `{"backend":"bluetooth"}`. The default is analog.

The Android listener, when present on the phone, remains device-local at
`127.0.0.1:9999`; do not replace the documented ADB shell/netcat transport with
`adb reverse` for the known baseline without a separately authorized test.

Never place phone numbers, SMS bodies, credentials, APKs, signing keys, or
runtime logs in tracked files.
