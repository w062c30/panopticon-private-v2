import subprocess, sys, os, time

os.chdir(r"d:\Antigravity\Panopticon")

os.makedirs("logs", exist_ok=True)

env = dict(os.environ)
env["PANOPTICON_WHALE"] = "1"
env["PYTHONUNBUFFERED"] = "1"

# Orchestrator runs ALL tracks: Radar + OFI + Graph + Signal Engine + Whale Scanner
orch_proc = subprocess.Popen(
    [sys.executable, "-u", "run_hft_orchestrator.py"],
    stdout=open("logs/orchestrator_d35.log", "w", buffering=1),
    stderr=subprocess.STDOUT,
    env=env,
)
print("ORCH_PID=" + str(orch_proc.pid))
time.sleep(5)
alive = orch_proc.poll() is None
print("ORCH alive=" + str(alive))
if not alive:
    print("CHECK logs/orchestrator_d35.log")
