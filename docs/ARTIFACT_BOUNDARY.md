# Source versus frozen APK audit

This audit is intentionally read-only with respect to the frozen artifact and
phone application data. It records provenance; it does not claim a rebuild or
installation.

## Frozen release reference

The frozen APK is held in an operator-controlled comparison tree and is not
copied into this standalone tree. Direct inspection reported:

- SHA-256: `eadc06b5a1ff117696c1a8ce3dc35df0f47141fa1a63a9726da2383698f0abf0`
- Package: `com.rex.sms`
- Version: `1.0`, versionCode `1`
- Target/platform metadata: SDK 29, platform build `6.0.1`
- Launcher: `com.rex.sms.SetupActivity`
- Label: `RexBridge`
- Proven history: SMS `PING`, `SEND`, and consume-on-read `READ_INBOX` behavior
  is recorded in the comparison tree's `PROJECT_STATE.md`.

## What this source tree contains

`AndroidManifest.xml`, `src/`, and `res/` are experimental source inputs. They
are not a checked-out reproduction of the frozen release commit: compared with
the frozen comparison-tree Git commit `bcdf95a` (`sms-poc-working`), the
standalone manifest, `SetupActivity.java`, and `SmsBridgeService.java` differ;
`SmsReceiver.java` and `res/values/strings.xml` match byte-for-byte. The
standalone Android manifest currently declares versionCode `3` and versionName
`1.2`, so it must not be described as the source of the version-1.0 APK.

The later source adds call/audio/protocol work. No APK in this tree is a
release output, and no source hash can prove APK equivalence: compilation,
dexing, resource packaging, signing, and exact build inputs would all need to
be reproduced. The frozen APK checksum above is the only artifact identity
claimed here.

## Explicit evidence boundary

- Source inspection: **performed** (metadata and byte comparisons above).
- Frozen APK hash/manifest inspection: **performed** without modification.
- APK rebuild/sign/install: **not performed**.
- Physical SMS/call/audio acceptance: **not performed**.
- Host/plugin mocked tests: see `MANUAL_ACCEPTANCE.md` and test output reported
  with the delivery.

Do not use this document, current Android source, or host unit tests as evidence
that the frozen APK contains experimental call/audio behavior.
