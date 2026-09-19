param(
  [int]$Port = 8081,
  [switch]$Lan,
  [string[]]$AllowRoot = @(),
  [switch]$NoStart,
  [switch]$Offline,
  [switch]$SkipGoose
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$pythonCommand = $null
foreach ($candidate in @('py','python','python3')) {
  if (Get-Command $candidate -ErrorAction SilentlyContinue) {
    & $candidate -c 'import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
    if ($LASTEXITCODE -eq 0) { $pythonCommand = $candidate; break }
  }
}
if (-not $pythonCommand) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'Python 3.10+ and winget are missing. Install Python with pip/venv and rerun setup.ps1.'
  }
  & winget install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
  if ($LASTEXITCODE -ne 0) { throw 'Python installation failed.' }
  $env:Path = [Environment]::GetEnvironmentVariable('Path','User') + ';' + [Environment]::GetEnvironmentVariable('Path','Machine')
  $knownPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
  if (Test-Path $knownPython) { $pythonCommand = $knownPython }
  elseif (Get-Command python -ErrorAction SilentlyContinue) { $pythonCommand = 'python' }
  else { throw 'Python was installed; reopen PowerShell and rerun setup.ps1.' }
}
$deploymentArgs = @('deploy.py','setup','--port',"$Port")
if ($Lan) { $deploymentArgs += '--lan' }
if ($NoStart) { $deploymentArgs += '--no-start' }
if ($SkipGoose) { $deploymentArgs += '--skip-goose' }
if ($Offline) { $deploymentArgs += '--offline' }
foreach ($root in $AllowRoot) { $deploymentArgs += @('--allow-root',$root) }
& $pythonCommand @deploymentArgs
if ($LASTEXITCODE -ne 0) { throw 'ASG deployment failed; see output above.' }
