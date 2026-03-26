# run_flutter.ps1
# Reads GOOGLE_MAPS_API_KEY from .env and injects it into web/index.html before running Flutter.

$envFile = "$PSScriptRoot\..\backend\.env"
$indexTemplate = "$PSScriptRoot\web\index.html"

# Parse the .env file
$envVars = @{}
Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]+?)\s*=\s*"?([^"]*)"?\s*$') {
        $envVars[$Matches[1]] = $Matches[2]
    }
}

$apiKey = $envVars["GOOGLE_MAPS_API_KEY"]
if (-not $apiKey) {
    Write-Error "GOOGLE_MAPS_API_KEY not found in $envFile"
    exit 1
}

# Inject the key into index.html
(Get-Content $indexTemplate -Raw) -replace '\{\{GOOGLE_MAPS_API_KEY\}\}', $apiKey | Set-Content $indexTemplate

Write-Host "Injected GOOGLE_MAPS_API_KEY into index.html"

# Run Flutter for web
flutter run -d chrome

# Restore the template token after running so the key doesn't sit in the file
(Get-Content $indexTemplate -Raw) -replace [regex]::Escape($apiKey), '{{GOOGLE_MAPS_API_KEY}}' | Set-Content $indexTemplate
Write-Host "Restored index.html template token"
