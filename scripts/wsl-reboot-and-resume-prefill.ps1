# WSL 재기동 후 prefill 벤치 자동 재개
$ErrorActionPreference = "Stop"
$script = "/home/yunahe/ai-runtime/cursor-local-llm/scripts/resume-prefill-after-wsl-reboot.sh"
$distro = "Ubuntu-24.04"

Write-Host "WSL shutdown..."
wsl --shutdown
Start-Sleep -Seconds 8

Write-Host "WSL restart + resume benchmark..."
wsl -d $distro -u yunahe -e bash -lc "chmod +x $script && nohup bash $script >/dev/null 2>&1 &"
Write-Host "Resume script launched in background. Log: tmp/prefill-scale-bench/resume-after-wsl.log"
