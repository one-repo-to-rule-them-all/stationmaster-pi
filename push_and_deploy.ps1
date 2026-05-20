# push_and_deploy.ps1
# Commits local changes, pushes to GitHub, and pulls on the Pi.
#
# Usage (from repo root in PowerShell):
#   .\push_and_deploy.ps1

$PI_USER = "cmpe8803"
$PI_HOST = "192.168.68.61"
$PI_REPO = "~/Software_repos/stationmaster-pi-main"

Set-Location $PSScriptRoot

Write-Host "[~] Staging changes..." -ForegroundColor Cyan
git add -A

$staged = git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "[~] Nothing new to commit." -ForegroundColor Yellow
} else {
    Write-Host "[+] Committing..." -ForegroundColor Green
    git commit -m "fix: jellyfin library path resolution + retry logic + fix_jf_libraries script"
}

Write-Host "[+] Pushing to GitHub..." -ForegroundColor Green
git push

Write-Host "[+] Pulling on Pi..." -ForegroundColor Green
ssh "${PI_USER}@${PI_HOST}" "cd ${PI_REPO} && git pull"

Write-Host ""
Write-Host "[+] Done. Now run on the Pi:" -ForegroundColor Green
Write-Host "    python3 ${PI_REPO}/fix_jf_libraries.py" -ForegroundColor Cyan
