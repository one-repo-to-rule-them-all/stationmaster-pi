# push.ps1 — commit and push all local changes to GitHub
# Run from PowerShell: .\push.ps1

Set-Location $PSScriptRoot

Write-Host "[~] Staging all changes..." -ForegroundColor Cyan
git add -A

git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "[~] Nothing to commit." -ForegroundColor Yellow
} else {
    $msg = Read-Host "Commit message (leave blank for default)"
    if (-not $msg) { $msg = "chore: update stationmaster-pi scripts" }
    git commit -m $msg
}

Write-Host "[+] Pushing to GitHub..." -ForegroundColor Green
git push

Write-Host "[+] Done. On the Pi run:" -ForegroundColor Green
Write-Host "    cd ~/Software_repos/stationmaster-pi-main && git pull" -ForegroundColor Cyan
