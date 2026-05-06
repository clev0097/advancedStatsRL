# Build a single-file Windows exe of RL Tracker.
# Requires: pip install -e .[dev]
# Output:   dist/RLTracker.exe

$ErrorActionPreference = 'Stop'

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

python -m PyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --name RLTracker `
    --collect-submodules rl_tracker `
    rl_tracker/__main__.py

Write-Host ""
Write-Host "Built: dist\RLTracker.exe" -ForegroundColor Green
