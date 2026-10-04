"""Offline protocol/unit/integration tests, no simulator or external network."""
import base64
import hashlib
import json
import socket
import threading
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from src.training_core.bus_observer import BusClient, BusError, parse_bus_telemetry, observe


def frame(text: str, op=1):
    raw = text.encode()
    if len(raw) < 126:
        return bytes([0x80 | op, len(raw)]) + raw
    return bytes([0x80 | op, 126]) + len(raw).to_bytes(2,'big') + raw


class FakeServer:
    def __init__(self, texts, *, good_handshake=True):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.texts = texts
        self.good_handshake = good_handshake
        self.errors = []
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.thread.join(timeout=4)
        self.listener.close()
        if self.errors: raise self.errors[0]

    def serve(self):
        try:
            self.listener.settimeout(3)
            conn, _ = self.listener.accept()
            with conn:
                conn.settimeout(3)
                req = bytearray()
                while b'\r\n\r\n' not in req:
                    req.extend(conn.recv(8192))
                key = next(x.split(':',1)[1].strip() for x in req.decode().split('\r\n') if x.lower().startswith('sec-websocket-key:'))
                magic='258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
                answer=base64.b64encode(hashlib.sha1((key+magic).encode()).digest()).decode()
                if not self.good_handshake: answer='INVALID'
                conn.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                              'Connection: Upgrade\r\nSec-WebSocket-Accept: '+answer+'\r\n\r\n').encode())
                if self.good_handshake:
                    for content in self.texts:
                        try: conn.sendall(content)
                        except (ConnectionError, BrokenPipeError): break
                    # Keep connection long enough for a short observer fixture if needed.
                    threading.Event().wait(.15)
        except (OSError, ValueError) as e:
            self.errors.append(e)


class ProtocolTests(unittest.TestCase):
    def test_ws_valid_handshake_and_telemetry(self):
        pkt=frame(json.dumps({'type':'telemetry','data':{'lat':-7.347,'pose_source':'fg'}}))
        with FakeServer([pkt]) as srv, BusClient(srv.port) as bus:
            got=parse_bus_telemetry(bus.recv_text(.5))
            self.assertEqual(got['lat'], -7.347)
            self.assertEqual(got['pose_source'], 'fg')

    def test_bad_handshake_fails_closed(self):
        with FakeServer([],good_handshake=False) as srv:
            with self.assertRaises(BusError):
                with BusClient(srv.port): pass

    def test_nontelemetry_rejected(self):
        self.assertIsNone(parse_bus_telemetry('not json'))
        self.assertIsNone(parse_bus_telemetry('{"type":"cmd","data":{"armed":true}}'))
        self.assertIsNone(parse_bus_telemetry('{"type":"telemetry","data":[]}'))

    def test_only_allowed_fields_pass(self):
        d=parse_bus_telemetry(json.dumps({'type':'telemetry','data':{'lat':2,'armed':False,'evil':{'a':1},'mode':['bad']}}))
        self.assertEqual(d, {'lat':2,'armed':False})

    def test_ping_or_fragmentation(self):
        text=json.dumps({'type':'telemetry','data':{'alt_msl':351}})
        raw=text.encode()
        split=len(raw)//2
        parts = [bytes([0x89,0]),bytes([0x01,split])+raw[:split],bytes([0x80,len(raw)-split])+raw[split:]]
        with FakeServer(parts) as srv, BusClient(srv.port) as bus:
            self.assertEqual(parse_bus_telemetry(bus.recv_text(.5))['alt_msl'],351)

    def test_bus_unavailable_no_simulator_action(self):
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        with tempfile.TemporaryDirectory() as td:
            code=observe(Path(td),port,1)
            self.assertEqual(code,2)
            self.assertEqual(len(list((Path(td)/'runtime/dev003').glob('*.jsonl'))),0)

    def test_observe_live_bus_writes_diagnostic_not_training(self):
        pkt=frame(json.dumps({'type':'telemetry','data':{'lat':-7.347,'lon':108.2464,
                    'alt_msl':349.4,'pose_source':'fg','armed':False,'mode':'MANUAL'}}))
        with FakeServer([pkt]) as srv, tempfile.TemporaryDirectory() as td:
            status=observe(Path(td),srv.port,1)
            self.assertEqual(status,0)
            records=list((Path(td)/'runtime/dev003').glob('bus-*.jsonl'))
            self.assertEqual(len(records),1)
            rows=[json.loads(line) for line in records[0].read_text().splitlines()]
            self.assertGreaterEqual(len(rows),1)
            sample=next(row for row in rows if row['kind']=='telemetry')
            self.assertEqual(sample['source'],'existing_f1_ui_bus')
            self.assertFalse(sample['synthetic'])
            self.assertEqual(sample['data']['pose_source'],'fg')
            self.assertEqual(sample['data']['lat'],-7.347)
            self.assertEqual(len(list((Path(td)/'runtime/dev003').glob('dev003-*.sqlite'))),1)

    def test_oversize_rejected(self):
        # Frame header advertises >1 MB without allocating the payload.
        size=(1<<20)+1
        with FakeServer([bytes([0x81,0x7f])+size.to_bytes(8,'big')]) as srv, BusClient(srv.port) as bus:
            with self.assertRaises(BusError): bus.recv_text(.5)


if __name__ == '__main__': unittest.main()

