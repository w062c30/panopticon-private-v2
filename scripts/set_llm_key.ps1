param()

Write-Host "請輸入 NVIDIA API Key（輸入內容不會顯示）:"
$secure = Read-Host -AsSecureString
$bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  if ([string]::IsNullOrWhiteSpace($plain)) {
    Write-Error "NVIDIA_API_KEY 不可為空。"
    exit 1
  }
  $env:NVIDIA_API_KEY = $plain
  Write-Host "NVIDIA_API_KEY 已寫入目前終端 session（不落檔）。"
} finally {
  if ($bstr -ne [IntPtr]::Zero) {
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
}
