$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot 'runtime'
$buildCache = Join-Path $projectRoot '.build'
New-Item -ItemType Directory -Force -Path $buildCache | Out-Null
$pythonZip = Join-Path $buildCache 'python-3.13.15-embed-amd64.zip'
$pythonSha256 = 'd1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf'
if (!(Test-Path -LiteralPath $pythonZip)) {
    Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip' -OutFile $pythonZip
}
$pythonStream = [System.IO.File]::OpenRead($pythonZip)
try {
    $actualHash = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash($pythonStream)).Replace('-', '').ToLowerInvariant()
} finally { $pythonStream.Dispose() }
if ($actualHash -ne $pythonSha256) {
    throw 'Python download checksum does not match the official release.'
}
python -m zipfile -e $pythonZip (Join-Path $runtimeRoot 'python')
if ($LASTEXITCODE -ne 0) { throw 'Could not unpack the verified Python runtime.' }
Set-Content -LiteralPath (Join-Path $runtimeRoot 'python\python313._pth') -Value "python313.zip`n.`n..`n..\vendor`n" -Encoding ascii
python -m pip install --only-binary=:all: --python-version 3.13 --platform win_amd64 --implementation cp --abi cp313 --upgrade --target (Join-Path $runtimeRoot 'vendor') -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install bundled Python dependencies.' }
$compilerPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $compilerPath /nologo /target:exe /platform:x64 /r:System.Web.Extensions.dll "/out:$runtimeRoot\codex-router.exe" (Join-Path $PSScriptRoot 'Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Could not compile the Windows launcher.' }
& (Join-Path $runtimeRoot 'python\python.exe') -m unittest discover -s $runtimeRoot -p test_router.py -v
if ($LASTEXITCODE -ne 0) { throw 'Router tests failed.' }
Write-Output 'Windows runtime built and tested.'
