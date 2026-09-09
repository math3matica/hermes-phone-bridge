# Hermes-Phone Bridge Protocol V1

This document describes the protocol currently implemented by `SmsBridgeService`. It is not a proposal for a new wire format.

## Transport and connection

The host uses USB ADB and executes a device-side shell command. The service listens only on device-local `127.0.0.1:9999`; `adb reverse` is not used. Each request is a new TCP connection, one UTF-8 line terminated by LF, followed by one response line and connection close. The host relay serializes access with a process lock.

Typical transport:

```sh
adb shell 'printf "%s\\n" "PING" | nc -w 3 127.0.0.1 9999'
```

There is no request identifier, framing beyond one line, authentication field, or negotiated version in V1.

## Requests and responses

Syntax is ASCII command text plus UTF-8 arguments. The first space separates a command from its argument.

| Request | Success response | Failure response |
|---|---|---|
| `PING` | `PONG` | transport failure |
| `SEND <phone> <message>` | `OK sent to <phone>` | `FAIL <class>: <message>` |
| `READ_INBOX` | `OK <JSON array>` | `FAIL ...` or transport failure |
| `CALL <phone>` | `OK dialing <phone>` | `FAIL ...` |
| `CALL_STATUS` | `OK <JSON object>` | `FAIL ...` |
| `HANGUP` | `OK hangup <value>` | `FAIL ...` |
| `ANSWER` | `OK answer requested` | `FAIL ...` |
| `AUDIO_TEST <seconds> [frequency]` | `OK {"rms":...,"frequency":...,"tone":...,"frames":...}` | `FAIL ...` |

Unknown commands return `ERR unknown command`; an empty input returns `ERR empty input`.

`SEND` takes the first token as the phone number and the remainder of the line as message. Messages longer than 160 characters are divided by Android's `SmsManager`; V1 does not return a message id or delivery receipt. The current host plugin normalizes whitespace before sending, so arbitrary line breaks are not representable through that surface.

`READ_INBOX` returns JSON objects written by the SMS receiver. It consumes/deletes the JSON files after reading. This destructive read is intentional V1 behavior. A host timeout or connection loss can therefore create uncertainty about whether a read occurred.

## Timeouts and retries

The Android listener has no protocol-level timeout negotiation. Host callers choose bounded `nc` timeouts: short health/status requests, longer inbox reads, and command-specific bounds for audio/call operations. ADB subprocesses add a small process timeout around the network timeout.

V1 has no transaction id or idempotency key. Retrying `SEND` after a timeout can send a duplicate SMS. Retrying `READ_INBOX` can lose messages if the device consumed them before the response was lost. The relay persists its queue/session state and uses a process lock, but those measures do not make the wire protocol transactional.

## Version strategy

V1 has no version field and must remain wire-compatible with the frozen Android application. Protocol V2 should add a capability/version handshake, request ids, explicit UTF-8 length-safe framing, structured error codes, send idempotency keys, and an acknowledged/non-consuming inbox operation before changing these semantics. V2 must not silently alter V1's consume-on-read behavior.
