@echo off
rem Claude Code as the harness, your llama-server model as the brain, Cua Driver as the hands.
rem
rem A batch file on purpose: PowerShell's Start-Process -ArgumentList silently mangles long
rem argument lists (the same failure that dropped llama-server's --host flag).
rem
rem Override any default before calling, e.g.:
rem   set LLAMA_URL=http://127.0.0.1:1237 & set LLAMA_API_KEY=... & start-claude-local-cua.cmd
rem Extra arguments are passed through to claude (for example a first prompt).
setlocal
if not defined LLAMA_URL set "LLAMA_URL=http://127.0.0.1:1237"
if not defined LLAMA_MODEL set "LLAMA_MODEL=local-cua"
if not defined LLAMA_API_KEY set "LLAMA_API_KEY=local-no-key-required"
if not defined LLAMA_CTX set "LLAMA_CTX=131072"

curl.exe -fsS --max-time 5 "%LLAMA_URL%/health" >nul 2>&1
if errorlevel 1 (
  echo llama-server is not healthy at %LLAMA_URL%. Start it first, then retry.
  exit /b 1
)

rem Claude Code sends ANTHROPIC_API_KEY as X-Api-Key, which llama-server accepts for --api-key.
set "ANTHROPIC_BASE_URL=%LLAMA_URL%"
set "ANTHROPIC_API_KEY=%LLAMA_API_KEY%"
set "CLAUDE_CODE_MAX_CONTEXT_TOKENS=%LLAMA_CTX%"
set "CUA_DRIVER_RS_TELEMETRY_ENABLED=false"

rem Run from the repository root so the relative paths in local-model.mcp.json resolve.
pushd "%~dp0..\..\.."
claude --bare --strict-mcp-config --mcp-config "integrations\cua\mcp\local-model.mcp.json" --append-system-prompt-file "integrations\cua\mcp\local-agent-instructions.md" --tools "" --model %LLAMA_MODEL% %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
