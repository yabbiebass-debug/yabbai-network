@echo off
:: YABBAI Network V2 — Windows launcher
:: Starts all Python backends + opens the hub in your browser

title YABBAI Network V2

echo ============================================================
echo  YABBAI NETWORK V2 — starting all services
echo ============================================================

:: Load .env if present
if exist .env (
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        if not "%%a"=="" if not "%%a:~0,1%"=="#" set %%a=%%b
    )
)

if "%ADMIN_API_KEY%"=="" (
    echo WARNING: ADMIN_API_KEY not set. Copy .env.example to .env first.
    echo.
)

:: Start each backend in its own window
start "YABBAI Revenue System :7870" cmd /k "python -m uvicorn revenue_system.unified_server:app --host 0.0.0.0 --port 7870 --log-level warning"
timeout /t 1 /nobreak >nul

start "YABBAI AI :7860" cmd /k "python -m uvicorn yabbai_local.api.server:app --host 0.0.0.0 --port 7860 --log-level warning"
timeout /t 1 /nobreak >nul

start "DeFi Simulator :8002" cmd /k "python -m uvicorn defi_simulator.api.server:app --host 0.0.0.0 --port 8002 --log-level warning"
timeout /t 1 /nobreak >nul

start "GoldScout :8001" cmd /k "python -m uvicorn goldscout.server:app --host 0.0.0.0 --port 8001 --log-level warning"
timeout /t 1 /nobreak >nul

start "Ops Cockpit :7880" cmd /k "python -m uvicorn yabbai_ops.server:app --host 0.0.0.0 --port 7880 --log-level warning"
timeout /t 2 /nobreak >nul

echo.
echo  Services started:
echo    Revenue System  → http://localhost:7870
echo    YABBAI AI       → http://localhost:7860
echo    DeFi Simulator  → http://localhost:8002
echo    GoldScout       → http://localhost:8001
echo    Ops Cockpit     → http://localhost:7880
echo.
echo  Open the Hub HTML in your browser:
echo    hub\index.html  (or deploy to Netlify)
echo.
echo  OR run the unified gateway on port 8080:
echo    python yabbai_network_server.py
echo ============================================================
pause
