# DeepSea CAN Diagnostic Tool

A receive-only CAN diagnostic tool for the DeepSea embedded Linux challenge. It monitors up to four power modules, decodes their telemetry and fault codes, and reassembles their identification strings from multiple CAN frames.

The tool runs on Linux with SocketCAN. It uses Python 3's standard library and provides a terminal dashboard for normal use and newline-delimited JSON (NDJSON) output for automated evaluation.

## Requirements

- Python 3
- Linux with SocketCAN support
- An active CAN interface, such as `vcan0`

The program uses `socket` with `AF_CAN`, `SOCK_RAW`, and `CAN_RAW`, and `struct` to handle CAN frames. It does not use third-party CAN libraries and never transmits CAN frames.

## Run

Start the terminal dashboard:

```bash
python3 main.py --iface vcan0
```

Start grader mode:

```bash
python3 main.py --iface vcan0 --grader
```

In grader mode, stdout contains only NDJSON events, one JSON object per line. Each line is flushed immediately. Diagnostics and errors are written to stderr.

## CAN messages

The tool processes the message IDs defined by the challenge:

| CAN ID | Message | Decoding |
| --- | --- | --- |
| `0x100`–`0x103` | Module telemetry | Voltage, current, temperature, status bits, and sequence number |
| `0x1F0` | Fault code | Module number and fault code |
| `0x6F0`–`0x6F3` | Module identification | Reassembled serial number and firmware string |
| `0x200`–`0x2FF` | Unrelated traffic | Ignored |

Telemetry values are converted using the challenge's scaling rules. For example, voltage is the raw little-endian value multiplied by `0.1`, and current is the raw little-endian value multiplied by `0.01`.

## Filtering

The program installs SocketCAN raw-socket filters for the telemetry, fault, and identification ID ranges. Filtering in the kernel prevents unrelated CAN frames, including the noise range `0x200`–`0x2FF`, from being delivered to the application.

The masks match the standard 11-bit CAN IDs and the relevant frame flags. For the three four-ID ranges, the lower two identifier bits are left variable; for the single fault ID, all identifier bits are matched.

The application also routes received frames by CAN ID before decoding them. `frames_processed` counts only recognized telemetry, fault, and diagnostic frames. Noise frames are not counted.

## Diagnostic-message reassembly

Identification strings are longer than one CAN frame, so the sender splits each string into a First Frame and one or more Consecutive Frames. The program reconstructs each message as follows:

1. A First Frame declares the total message length and carries the first six bytes.
2. Each Consecutive Frame carries a sequence number and up to seven more bytes.
3. The program appends data only when the sequence number is the expected one. The sequence number advances modulo 16.
4. Once the declared number of bytes has been received, the program decodes exactly that many bytes as ASCII and reports the completed string.

Reassembly state is keyed by CAN ID. Each module therefore has an independent in-progress message, so frames from different modules can be interleaved safely.

The declared message length must be between 8 and 64 bytes. There can be at most one in-progress message for each of the four diagnostic IDs, and each message is capped at 64 bytes. This places a fixed bound on reassembly memory.

An attempt is discarded when its CAN ID receives a new First Frame, an invalid diagnostic frame, or a Consecutive Frame with an unexpected sequence number. Orphan Consecutive Frames are ignored. Completed attempts are removed from the in-progress state. The implementation does not use a time-based expiry: incomplete attempts remain bounded to one per known module and are replaced or discarded by the events above. A timeout could be added if the protocol later defines an appropriate inactivity limit; choosing one without such a requirement could discard a valid message whose frames were delayed.

## Grader output

The `--grader` mode emits events as they are decoded:

- `telemetry`: one event for each decoded telemetry frame.
- `fault`: one event for each decoded fault-code frame.
- `diag_complete`: one event when an identification message has been fully reassembled.
- `stats`: the running count of recognized, real CAN frames processed. Stats are emitted periodically and once more during shutdown.

Example events:

```json
{"type":"telemetry","module":0,"seq":4821,"voltage":412.3,"current":118.55,"temp_c":47,"enabled":true,"fault":false,"derated":false}
{"type":"fault","module":2,"code":1}
{"type":"diag_complete","can_id":"0x6f0","string":"SN:PMU-4471-A FW:2.3.1","ts_ns":88123456789}
{"type":"stats","frames_processed":15234}
```

The diagnostic completion timestamp is taken from `time.monotonic_ns()` when reassembly finishes. Abandoned attempts do not generate `diag_complete` events.

## Test with the challenge generator

On the challenge Raspberry Pi, run the tool in one shell and the generator in another:

```bash
python3 main.py --iface vcan0 --grader
```

```bash
python3 ~/challenge/can_generator/generator.py --iface vcan0 --duration 90
```

The generator supports an `--out` option to save its ground-truth log for comparison:

```bash
python3 ~/challenge/can_generator/generator.py \
  --iface vcan0 \
  --duration 90 \
  --out /tmp/deepsea-ground-truth.jsonl
```

## Design choices and limitations

- **Kernel-side filtering:** reduces irrelevant traffic delivered to the application while leaving decoding and event counting explicit in Python.
- **Per-ID reassembly:** keeps concurrent module messages independent and makes the maximum number of active states known.
- **Length and sequence validation:** prevents oversized payloads and incomplete or out-of-order attempts from being reported as valid identification strings.
- **Bounded fault history:** the dashboard retains only the ten most recent fault events.
- **No timeout-based expiry:** partial reassembly state is bounded by the four known IDs and is removed by completion, invalid input, or a restarting First Frame. If the protocol later specifies a maximum inter-frame delay, a matching inactivity timeout could be added.
- **Potential future improvements:** add unit tests for malformed frames and each reassembly recovery case, and implement a timeout if the protocol defines a safe expiry interval.

## Repository contents

```text
.
├── main.py
└── README.md
```
