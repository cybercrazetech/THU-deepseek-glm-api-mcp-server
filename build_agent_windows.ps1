$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AgentPath = Join-Path $RootDir "agent.py"
$DistDir = Join-Path $RootDir "dist"
$BuildDir = Join-Path $RootDir "build"
$SpecPath = Join-Path $RootDir "thu-agent.spec"

py -3 -m PyInstaller `
  --clean `
  --onefile `
  --name thu-agent `
  --distpath $DistDir `
  --workpath $BuildDir `
  --specpath $RootDir `
  --exclude-module IPython `
  --exclude-module PIL `
  --exclude-module PyQt5 `
  --exclude-module PyQt6 `
  --exclude-module matplotlib `
  --exclude-module numpy `
  --exclude-module pygame `
  --exclude-module pytest `
  --exclude-module tkinter `
  --exclude-module traitlets `
  --exclude-module jedi `
  --exclude-module parso `
  --exclude-module gi `
  --exclude-module cryptography `
  --exclude-module bcrypt `
  $AgentPath

Write-Host ""
Write-Host "Built executable:"
Write-Host "  $DistDir\\thu-agent.exe"
