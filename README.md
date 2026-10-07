cat > README.md <<'EOF'
# DeepSea CAN Diagnostic Tool

A receive-only CAN diagnostic tool for the DeepSea embedded Linux challenge. It uses Python 3 and the standard library, listens on a SocketCAN interface, and supports both a terminal dashboard and the JSON Lines format required by the grader.

## Requirements

- Python 3
- A Linux system with SocketCAN support
- An active CAN interface, such as `vcan0`

The tool does not transmit CAN frames and does not use third-party CAN libraries.

## Run

Start the terminal dashboard:

```bash
python3 main.py --iface vcan0
