"""Start the orchestrator with unbuffered output in background."""
import subprocess
import os
import sys
import time
import signal

os.environ["PANOPTICON_SHADOW"] = "1"
os.environ["PYTHONUNBUFFERED"] = "1"

timestamp = time.strftime("%Y%m%d_%H%M%S")
proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_dir = os.path.join(proj_root, "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"orchestrator_{timestamp}.log")
err_file = os.path.join(log_dir, f"orchestrator_{timestamp}.err")

print(f"Starting orchestrator, log: {log_file}", flush=True)

lf = open(log_file, "w", buffering=1)  # line buffered
ef = open(err_file, "w", buffering=1)

proc = subprocess.Popen(
    [sys.executable, "-u", "run_hft_orchestrator.py"],
    cwd=proj_root,
    stdout=lf,
    stderr=subprocess.STDOUT,
    env={**os.environ, "PANOPTICON_SHADOW": "1", "PYTHONUNBUFFERED": "1"},
)
print(f"Started PID: {proc.pid}", flush=True)
print(f"Monitor with: Get-Content '{log_file}' -Wait", flush=True)