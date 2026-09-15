$ErrorActionPreference = "Stop"

Write-Host "[TraceForge] Building and starting the local pipeline..." -ForegroundColor Cyan
docker compose up -d --build

function Wait-Endpoint {
    param([string]$Url, [int]$Attempts = 40)
    for ($i = 0; $i -lt $Attempts; $i++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "Timed out waiting for $Url"
}

Wait-Endpoint "http://localhost:8080/readyz"
Wait-Endpoint "http://localhost:8081/healthz"

Write-Host "[TraceForge] Sending synthetic coding-agent sessions with deliberate privacy test vectors..." -ForegroundColor Cyan
docker compose run --rm --no-deps privacy-gateway traceforge-demo --endpoint privacy-gateway:4317 --count 3

Write-Host "[TraceForge] Demo ready: http://localhost:8081" -ForegroundColor Green
Write-Host "The synthetic email, prompt, source code and bearer token must not appear in persistent storage." -ForegroundColor DarkGray
