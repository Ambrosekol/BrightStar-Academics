import socket
import sys

if len(sys.argv) not in (2, 3):
    print("Usage: python port_test.py SERVER_IP [PORT]")
    raise SystemExit(2)

host = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) == 3 else 5000

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(3)
try:
    sock.connect((host, port))
    print(f"PASS: TCP port {port} is reachable on {host}")
except Exception as exc:
    print(f"FAIL: Could not reach {host}:{port}")
    print(exc)
    raise SystemExit(1)
finally:
    sock.close()
