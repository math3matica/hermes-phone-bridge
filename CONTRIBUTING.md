# Contributing

This tree is designed for review and GitHub publication, but the working copy
may contain pre-existing changes. Preserve them; do not reset or clean broadly.

## Development rules

- Keep the V1 `com.rex.sms` transport and its consume-on-read semantics
  compatible.
- Treat `src/`, `res/`, and `AndroidManifest.xml` as experimental Android
  inputs. Do not claim they reproduce the frozen APK.
- Do not rebuild, sign, install, or overwrite the frozen APK without explicit
  operator authorization. Do not test physical SMS, calls, or audio in CI.
- Add deterministic host tests with mocked subprocesses/filesystems for bridge,
  relay, plugin, and audio behavior.
- Never add personal phone data, device identifiers, logs, credentials, or
  signing material.

## Checks before a pull request

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q host plugin
```

Also inspect `git diff --check` in a Git checkout. The GitHub workflow runs
these checks offline with mocked hardware only. Manual device acceptance is a
separate, explicitly authorized gate documented in `MANUAL_ACCEPTANCE.md`.

## Changes to Android

Document package/version changes and update the artifact-boundary audit. A
new APK must be separately named, hashed, signed, and physically qualified;
never replace the frozen reference as part of a source-only change.

## License

No license has been selected by the project owner. Choose and add one before
public publication; until then, contributors should not assume redistribution
rights.
