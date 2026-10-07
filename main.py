import argparse
import json
import signal
import socket
import struct
import sys
import time
from collections import deque

#IDs CAN definidos por la prueba
TELEMETRY_BASE = 0x100
FAULT_ID = 0x1F0
DIAG_BASE = 0x6F0

MAX_DIAG_LENGTH = 64


STATS_INTERVAL = 2.0

#ID (4 bytes) | longitud (1 byte) | relleno (3 bytes) | data (8 bytes)
FRAME_FORMAT ="=IB3x8s"



#convierte un evento a JSON
def emit(event):
        line = json.dumps (event, separators=(",",":"))
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


# funcion que crea el socket CAN elegido, pone filtros y se conecta a vcan0
def create_can_socket(interface):
        can_socket = socket.socket (
                socket.AF_CAN,
                socket.SOCK_RAW,
                socket.CAN_RAW,

        )


        id_mask = 0x7FF

        filters =[
                (TELEMETRY_BASE, id_mask & ~0x003),
                (FAULT_ID, id_mask),
                (DIAG_BASE, id_mask & ~0x003),

        ]

        packed_filters = b"".join(
                struct.pack("=II", can_id, can_mask)
                for can_id, can_mask in filters
        )

        
        can_socket.setsockopt(101,1, packed_filters)
        #conecta el socket a la interfaz CAN elegida
        can_socket.bind((interface,))
        can_socket.settimeout(0.25)
        
        return can_socket


#funcion que separa los datos de una trama recibida: ID, longitud y datos

def unpack_frame(packet):
        expected_size = struct.calcsize(FRAME_FORMAT)
        
        if len(packet) != expected_size:
                return None
        
        raw_id, dlc, data = struct.unpack(FRAME_FORMAT, packet)


        if dlc > 8:
                return None

        can_id = raw_id & 0x7FF

        return can_id, dlc, data




#clase que permite realizar el diagnóstico.
class DiagnosticTool:
        
    def __init__(self, grader):
        self.grader = grader
        self.frames_processed = 0
        self.telemetry = {}
        self.faults = deque(maxlen=10)
        self.identifications = {}
        self.in_progress = {}
    #convierte los bytes de una lectura CAN en voltage, current, temp & status
    def decode_telemetry(self, module, data):
        voltage_raw, current_raw, temp_raw, status, seq = struct.unpack(
            "<HHBBH", data
        )

        event = {
            "type": "telemetry",
            "module": module,
            "seq": seq,
            "voltage": round(voltage_raw * 0.1, 1),
            "current": round(current_raw * 0.01, 2),
            "temp_c": temp_raw - 40,
            "enabled": bool(status & 0x01),
            "fault": bool(status & 0x02),
            "derated": bool(status & 0x04),
        }

        # Guarda la última lectura para mostrarla en el tablero normal.
        self.telemetry[module] = event

        # En modo evaluador, informa cada lectura como una línea JSON.
        if self.grader:
            emit(event)
    # obtiene los bytes de una falla y obtiene el módulo y elk código
    def decode_fault(self,data):
        
        module=data[0]
        code = data[1]

        event = {
            "type": "fault",
            "module": module,
            "code": code,
        }

        # Conserva las últimas diez fallas para el tablero.
        self.faults.append(event)

        # En modo evaluador, informa la falla inmediatamente.
        if self.grader:
            emit(event)
    #decodifica el paquete , cuenta las tramas procesadas, revisa el formato de la trama  
    def process_packet(self, packet):
        decoded = unpack_frame(packet)
        if decoded is None:
            return

        can_id, dlc, data = decoded

        if TELEMETRY_BASE <= can_id <= TELEMETRY_BASE + 3:
            self.frames_processed += 1
            if dlc == 8:
                module = can_id - TELEMETRY_BASE
                self.decode_telemetry(module, data)

        elif can_id == FAULT_ID:
            self.frames_processed += 1
            if dlc == 8:
                self.decode_fault(data)

        elif DIAG_BASE <= can_id <= DIAG_BASE + 3:
            # Contamos estas tramas; añadiremos su reensamblado enseguida.
            self.frames_processed += 1
            if dlc == 8:
                frame_type = data[0] & 0xF0
                if frame_type == 0x10:
                    self.start_diag_message(can_id, data, dlc)
                elif frame_type == 0x20:
                    self.add_diag_continuation(can_id, data, dlc)


    def start_diag_message(self, can_id, data, dlc):
        if dlc != 8:
            self.in_progress.pop(can_id, None)
            return

        # Un First Frame empieza con un byte cuyo nibble alto es 0x1.
        if data[0] & 0xF0 != 0x10:
            return

        length = ((data[0] & 0x0F) << 8) | data[1]

        # Un inicio nuevo reemplaza un intento anterior de ese mismo módulo.
        self.in_progress.pop(can_id, None)

        # El reto usa mensajes de más de 7 y hasta 64 bytes.
        if 7 < length <= MAX_DIAG_LENGTH:
            self.in_progress[can_id] = {
                "length": length,
                "payload": bytearray(data[2:8]),
                "next_seq": 1,
            }

    def add_diag_continuation(self, can_id, data, dlc):
        if dlc != 8:
            self.in_progress.pop(can_id, None)
            return

        state = self.in_progress.get(can_id)

        if state is None:
            return

        seq = data[0] & 0x0F

        if seq != state["next_seq"]:
            self.in_progress.pop(can_id, None)
            return


        state["payload"].extend(data[1:8])
        state["next_seq"] = (seq +1) & 0x0F

        if len(state["payload"]) >= state["length"]:
            payload = bytes(state["payload"][:state["length"]])
            self.in_progress.pop(can_id, None)

            try:
                text = payload.decode("ascii")
            except UnicodeDecodeError:
                return

            event = {
                "type": "diag_complete",
                "can_id": f"0x{can_id:x}",
                "string": text,
                "ts_ns": time.monotonic_ns(),
            }

            self.identifications[can_id] = text

            if self.grader:
                emit(event)



def stop_on_signal(_signum, _frame):
    raise KeyboardInterrupt


def run(interface, grader):
    tool = DiagnosticTool(grader)
    can_socket = None

    signal.signal(signal.SIGTERM, stop_on_signal)

    try:
        can_socket = create_can_socket(interface)
    except OSError as exc:
        print(f"CAN error: {exc}", file=sys.stderr)
        return 1

    last_stats_time = time.monotonic()

    try:
        while True:
            try:
                packet = can_socket.recv(16)
                tool.process_packet(packet)
            except socket.timeout:
                pass

            now = time.monotonic()
            if grader and now - last_stats_time >= STATS_INTERVAL:
                emit({
                    "type": "stats",
                    "frames_processed": tool.frames_processed,
                })
                last_stats_time = now

    except KeyboardInterrupt:
        pass
    finally:
        can_socket.close()
        if grader:
            emit({
                "type": "stats",
                "frames_processed": tool.frames_processed,
            })

    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", required=True)
    parser.add_argument("--grader", action="store_true")
    args = parser.parse_args()

    return run(args.iface, args.grader)


if __name__ == "__main__":
    raise SystemExit(main())
