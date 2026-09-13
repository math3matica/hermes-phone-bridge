# Hermes Phone Bridge

**Canonical development and runtime source:** this repository, `/home/math3matica/hermes-phone-bridge`.
The preserved directory `/home/math3matica/hermes-plugins-archives/hermes-phone-bridge-legacy-archive` is historical rollback material only; do not develop or install from it.

Standalone host integration and experimental Android source for the frozen
RexBridge transport used by Hermes. The project is intentionally split into
three independently owned layers:

- **Frozen release reference:** the operator-held `RexBridge_v1.0_sms_poc.apk`
  (`com.rex.sms`, version `1.0`, versionCode `1`, SHA-256 recorded in
  `docs/ARTIFACT_BOUNDARY.md`). It is not rebuilt, installed, copied, or
  replaced by this tree.
- **Experimental Android source:** `src/`, `res/`, and `AndroidManifest.xml`.
  This is a development input set, not proof of the frozen APK's contents or
  of a newly validated APK.
- **Current host integration:** `host/`, `plugin/hermes-phone/`, and
  `systemd/`. These can be tested with mocks and do not require an Android
  build or device.

## Safety and scope

The governing feature, flow, ownership, evidence, and change-control specification is [`docs/PLUGIN_SPEC.md`](docs/PLUGIN_SPEC.md). Read it before modifying Hermes Phone Bridge.

The V1 wire contract is preserved: ADB invokes a device-local listener at
`127.0.0.1:9999`, and `READ_INBOX` is consume-on-read. The host relay owns
owner filtering, durable queue state, and process locking. The Hermes plugin
provides explicit status/SMS/audio/call surfaces; it is not a general inbound
platform adapter.

No APK rebuild/install or physical SMS, call, or audio acceptance is part of
normal development or CI. See `MANUAL_ACCEPTANCE.md` for the separately
authorized manual gate and `docs/ARTIFACT_BOUNDARY.md` for the exact
source-versus-artifact audit.

## Repository layout

| Path | Role |
|---|---|
| `host/` | Relay, call/audio helpers, mocked-hardware tests |
| `plugin/hermes-phone/` | Native Hermes plugin manifest and entrypoint |
| `src/`, `res/`, `AndroidManifest.xml` | Experimental Android source inputs |
| `docs/` | Protocol, build constraints, configuration, artifact boundary |
| `examples/` | Sanitized configuration template |
| `.github/workflows/validation.yml` | Offline CI checks |

## Local verification

Use Python 3.11+ and install the test dependency in an isolated environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q host plugin
```

For an installed host package in a clean environment, use
`.venv/bin/python -m pip install -e '.[test]'`; the plugin directory is then
installed separately through Hermes's normal project/user plugin mechanism.
The Android source is not a Gradle project and has no checked-in SDK/toolchain
wrapper, so `docs/ANDROID_BUILD.md` documents constraints rather than a
guaranteed reproducible APK build. No APK download is provided by this
repository.

These commands use mocked subprocesses/filesystem fixtures only. They do not
invoke `adb`, `nc`, Android SDK tools, a phone, or a physical audio device.

## Configuration

Copy `examples/hermes-phone.env.example` to a private environment file and
set values appropriate for the local profile. Never commit the copy. Full
variable and state-path details are in `docs/CONFIGURATION.md`.

## Security, maturity, and limitations

The host relay, plugin, Android companion, and USB/ADB transport can access
private SMS and privileged cellular operations. Owner filtering and explicit
authorization are security boundaries, not convenience features. Keep ADB,
state files, SMS content, audio, and credentials local. The default/CI path
uses mocked subprocesses and filesystems only.

Implemented and automated-test verified: host relay state/locking, protocol
handling, plugin registration, SMS/call command construction, and audio worker
boundaries. The frozen APK is only an operator-held reference; current Android
source is experimental and not equivalent to it. Android build/install,
physical ADB, SMS, cellular call, and bidirectional audio acceptance remain
hardware-dependent; see `MANUAL_ACCEPTANCE.md`. This is a third-party project
for the Hermes Agent ecosystem and is not endorsed by Nous Research.

## License

MIT. See `LICENSE`.
