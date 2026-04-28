import time, urllib.request, json

# Compare: curl fast, urllib slow to localhost:8001
# Hypothesis: uvicorn GIL contention? Or connection pool size?
# Test: measure DNS + TCP connect + request separately

# First, test with curl-style single request (no connection reuse)
print("=== Test 1: Raw socket timing ===")
import socket
t0 = time.monotonic()
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(5.0)
sock.connect(('127.0.0.1', 8001))
sock.sendall(b'GET /api/versions HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
resp = sock.recv(4096)
sock.close()
t1 = time.monotonic()
print(f'  Raw socket: {(t1-t0)*1000:.0f}ms')
print(f'  Response preview: {resp[:100]}')

print()

# Test: uvicorn with multiple workers? Check process count
print("=== Test 2: Check if uvicorn has multiple workers ===")
import subprocess
r = subprocess.run(['wmic', 'process', 'where', "name='python.exe' and commandline like '%uvicorn%'", 'get', 'processid,commandline'],
                   capture_output=True, text=True)
print(r.stdout)

# Test: simple HTTP/1.1 persistent connection
print()
print("=== Test 3: HTTP/1.1 persistent connection ===")
import http.client
conn = http.client.HTTPConnection('localhost', 8001, timeout=10)
for i in range(3):
    t0 = time.monotonic()
    conn.request('GET', '/api/versions')
    resp = conn.getresponse()
    data = resp.read()
    t1 = time.monotonic()
    print(f'  Run {i+1}: {(t1-t0)*1000:.0f}ms len={len(data)}')
conn.close()

print()
print("=== Test 4: httpx with reuse ===")
import httpx
with httpx.Client(timeout=10.0) as client:
    for i in range(3):
        t0 = time.monotonic()
        r = client.get('http://localhost:8001/api/versions')
        t1 = time.monotonic()
        print(f'  Run {i+1}: {(t1-t0)*1000:.0f}ms')
