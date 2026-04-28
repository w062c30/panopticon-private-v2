param()

if (-not $env:NVIDIA_API_KEY) {
  Write-Error "未檢測到 NVIDIA_API_KEY。請先執行 .\\scripts\\set_llm_key.ps1"
  exit 1
}

python -m panopticon_py.main_loop
