"""Start the orchestrator as a background process."""
import subprocess
import os
import sys
import time

os.environ["PANOPTICON_SHADOW"] = "1"

timestamp = time.strftime("%Y%m%d_%H%M%S")
log_file = f"logs/orchestrator_{timestamp}.log"
err_file = f"logs/orchestrator_{timestamp}.err"

print(f"Starting orchestrator, log: {log_file}")

with open(log_file, "w") as lf, open(err_file, "w") as ef:
    proc = subprocess.Popen(
        [sys.executable, "run_hft_orchestrator.py"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=lf,
        stderr=ef,
        env={**os.environ, "PANOPTICON_SHADOW": "1"},
    )
    print(f"Started PID: {proc.pid}")
    print(f"Log: {os.path.abspath(log_file)}")