# FM TCP Bridge

A macOS menu bar app that listens on a TCP port and hands each incoming message to a
FileMaker Server script via the OData API. It has a live **Traffic window** showing
everything coming in and going out.

```
TCP client ──▶ FM TCP Bridge (Mac, port 9100) ──POST──▶ FileMaker Server OData
          ◀── reply (script result / OK)       ◀─────── scriptResult
```

## How it behaves

- **One message per connection.** The message is complete when the client closes the
  connection, goes quiet for `message_idle_ms` (default 750 ms), or `connection_timeout_sec` passes.
- **Nothing is lost.** Each message is written to the queue folder *before* it's sent to
  FileMaker. If FileMaker is down or returns an error, it stays queued and is retried every
  `retry_interval_sec`, including after a restart.
- **Reply to the client** (`reply_mode`):
  - `result` – waits up to `reply_wait_sec` and sends back the script's `Exit Script` result.
    If FileMaker fails it replies `ERROR …`; if it's too slow, `QUEUED <id>`.
  - `ack` – replies `ack_text` immediately.
  - `none` – just closes.
- Bytes that aren't valid text in `text_encoding` are sent as base64 (`"encoding": "base64"`).

## Traffic window

Menu bar ⇄ → **Show Traffic Window** (⌘T). Colour-coded, timestamped lines:

| Label | Meaning |
|---|---|
| ◀ IN | data received from a TCP client (with the payload) |
| ▶ FM | the OData request sent to FileMaker (with the JSON body) |
| ◀ FM | FileMaker's response (green = OK, orange = HTTP error) |
| ▶ REPLY | what was written back to the TCP client |
| ✖ ERROR | failures, retries, rejected clients |

Buttons: Clear, Copy All, Auto-scroll, Show payloads (turn off for a compact one-line-per-event view).
The same events are written to `~/Library/Logs/FM TCP Bridge/bridge.log`.

## Build

```
./build.sh
```
Produces `dist/FM TCP Bridge.app`. Copy it to /Applications. To launch at login:
System Settings → General → Login Items → **+** → FM TCP Bridge.

The first time it listens, macOS may ask to allow incoming connections — click Allow.

To run without building (for development):
```
python3 -m venv .venv && .venv/bin/pip install rumps && .venv/bin/python fm_tcp_bridge_app.py
```

## Configure

1. Menu bar ⇄ → **Edit Settings…** (opens `~/Library/Application Support/FM TCP Bridge/config.json`).
2. Fill in `fm_host`, `fm_database`, `fm_script`, `fm_username`, and `listen_port`. Save.
3. ⇄ → **Reload Settings**.
4. ⇄ → **Set FileMaker Password…** (stored in the login Keychain, not the config file).
5. ⇄ → **Test FileMaker Connection** — watch the Traffic window for `HTTP 200`.

| Setting | Default | Notes |
|---|---|---|
| `listen_host` | `0.0.0.0` | `127.0.0.1` to accept only local clients |
| `listen_port` | `9100` | |
| `allowed_clients` | `[]` | list of IPs; empty = any |
| `fm_host` | | `https://your.fms.host` |
| `verify_tls` | `true` | `false` only for self-signed test servers |
| `parameter_mode` | `json` | `raw` sends only the payload text as the parameter |
| `max_attempts` | `0` | 0 = retry forever; otherwise moves to `failed/` |

## FileMaker side

The FileMaker account needs the **fmodata** extended privilege, and OData must be enabled in
FileMaker Server Admin Console. The script receives a JSON parameter like:

```json
{
  "id": "20260929-142211-503112-a1b2c3",
  "receivedAt": "2026-09-29T14:22:11.503-04:00",
  "remoteAddr": "192.168.1.50",
  "remotePort": 51234,
  "localPort": 9100,
  "encoding": "utf-8",
  "bytes": 42,
  "data": "the message text"
}
```

Example script (`Process TCP Message`):

```
Set Variable [ $param ; Get ( ScriptParameter ) ]
If [ JSONGetElement ( $param ; "test" ) = 1 ]
    Exit Script [ Text Result: "pong" ]
End If
Set Variable [ $data ; JSONGetElement ( $param ; "data" ) ]
Go to Layout [ "TCP_Inbox" ]
New Record/Request
Set Field [ TCP_Inbox::MessageID ; JSONGetElement ( $param ; "id" ) ]
Set Field [ TCP_Inbox::ReceivedAt ; JSONGetElement ( $param ; "receivedAt" ) ]
Set Field [ TCP_Inbox::Source ; JSONGetElement ( $param ; "remoteAddr" ) ]
Set Field [ TCP_Inbox::Payload ; $data ]
Commit Records/Requests [ With dialog: Off ]
# …parse / process $data here…
Exit Script [ Text Result: "OK" ]
```

Whatever `Exit Script` returns is what the TCP client gets back in `result` mode.
Because a message is retried until FileMaker returns HTTP 200, the same `id` can arrive
twice if a response is lost after the script ran — check `MessageID` if duplicates matter.

## Test from Terminal

```
printf 'HELLO|123' | nc -w 5 127.0.0.1 9100
```

## Files

- `fm_tcp_bridge_app.py` – menu bar app + Traffic window (rumps / PyObjC)
- `bridge_core.py` – listener, queue, OData client; also runs headless:
  `python3 bridge_core.py`
- `setup.py`, `build.sh` – py2app packaging
