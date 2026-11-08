"""Deterministic TCP faults during SMB negotiation, never a mounted filesystem."""
import socket
import struct
import sys
import threading


def handle(peer):
    with peer:
        peer.settimeout(3)
        if not peer.recv(4):
            return
        print('SMB_REQUEST_RECEIVED', flush=True)
        if sys.argv[1] == 'reset':
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        else:
            threading.Event().wait(30)


with socket.socket() as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('0.0.0.0', 1445))
    listener.listen()
    print('FAULT_ENDPOINT_READY', flush=True)
    while True:
        peer, _ = listener.accept()
        threading.Thread(target=handle, args=(peer,), daemon=True).start()
