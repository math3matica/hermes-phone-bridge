# Hermes-Phone host relay

`hermes_phone_relay.py` is the host-side relay for the frozen Android SMS APK. It polls
`READ_INBOX`, accepts only the configured owner number, resumes one durable
Hermes session, and sends the final Hermes answer back through `SEND`.

## Current installation

User service:

```text
hermes-phone-relay.service
```

State:

```text
~/.hermes/hermes_phone_relay.json
```

The installed service uses an operator-configured owner number and polls every five
seconds. It is enabled under the user systemd manager.

`hermes_phone_relay.py` is singleton-owned. It acquires
`$HERMES_HOME/hermes-phone-relay.lock` with an advisory `flock` before it
constructs the relay or polls the phone. A competing process exits with
`RELAY_INSTANCE_START_REJECTED reason=active_relay_lock` and cannot launch a
call worker. The lock is held for the process lifetime and is released by the
kernel when the owner exits.

Per-run Rex Voice capture settings belong in the optional systemd environment
file:

```text
~/.config/hermes/hermes-phone-relay.env
```

Apply changes by restarting the canonical owner:

```bash
systemctl --user daemon-reload
systemctl --user restart hermes-phone-relay.service
```

Do not launch a second relay manually for a capture run.

## Verification

```bash
systemctl --user status hermes-phone-relay.service
journalctl --user -u hermes-phone-relay.service -f
```

Send an SMS from the owner phone. The daemon should process it and send the
Hermes response back. The first message creates the durable Hermes session;
subsequent messages resume it.

## Safety and limitations

- Only the owner number is processed. Other inbound messages are consumed by
the frozen APK's `READ_INBOX` command and discarded by the relay.
- The local queue is written atomically before Hermes is invoked. Failed Hermes
or outbound operations remain queued for retry.
- A crash after Android accepts an outbound SMS but before the queue state is
written can produce one duplicate reply after restart; this is inherent in the
APK's one-line, no-message-ID protocol.
- Hermes replies are flattened to one line because the Android bridge accepts one socket
line per command.
- The Android foreground notification is currently suppressed by the phone's
notification permission state; the service itself remains active.
