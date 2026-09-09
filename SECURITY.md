# Security policy

## Scope

The host relay and Hermes plugin can access SMS transport commands and guarded
calling/audio helpers. The Android source is experimental; the frozen APK and
phone application data are operator-owned artifacts outside this tree.

## Do not disclose or commit

Never commit phone numbers, SMS bodies, Android IDs, ADB serials, device logs,
credentials, `.env` files, private profile state, keystores, signing keys, APKs,
or captured audio. The example configuration is intentionally nonfunctional.
Review ignored files and Git history before publication; removing a secret from
the current tree does not remove it from history.

## Reporting

Do not open a public issue for a suspected credential, phone-data exposure, or
active device vulnerability. Preserve only the minimum safe reproduction and
contact the project owner through the repository's private security channel.
If no private channel is configured, use an out-of-band contact agreed with the
owner and do not attach raw SMS, audio, or device logs.

## Safety boundaries

The configured owner restriction is a security boundary, not a convenience.
Physical send/dial/hangup/audio actions require explicit authorization and are
never part of CI. Report whether evidence is mocked, source-inspection-only,
or device-backed; do not promote one class into another.
