# Android build constraints

The source is intentionally kept compatible with the existing RexBridge build procedure. The project historically compiles against the Android API 23 bootclasspath while using reflection for APIs introduced later. Do not replace that arrangement with a modern API level or Gradle migration without running the Android regression checks against the frozen device baseline.

## Reproducibility

The repository currently contains Java source and resource inputs, but no checked-in Gradle wrapper. A future build host must provide the Android SDK/platform tools and the exact API-23 compilation inputs used by the existing scripts. Build outputs, SDK paths, signing keys, keystores, device files, and logs are excluded from source control.

Before a release build:

1. Verify the package remains `com.rex.sms` and versionCode/versionName match the intended release.
2. Compile against the API-23 bootclasspath.
3. Preserve reflection guards for newer `SmsManager`, Telecom, and audio APIs.
4. Run host unit tests and a separately authorized physical acceptance test.
5. Sign with an external keystore; never copy that keystore into this tree.
6. Record the resulting artifact checksum outside source files.

The frozen APK and application data are protected operator artifacts, not build inputs to overwrite.
