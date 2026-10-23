@echo off
rem Knowbase entry point for cmd.exe / double-click / Task Scheduler: kb status, kb sync, ...
rem
rem Prefer this over calling .\kb.ps1 directly. The default execution policy on
rem Windows clients (Restricted / RemoteSigned) refuses unsigned .ps1 files, and
rem the message the user sees talks about digital signatures -- nothing to do with
rem this project. -ExecutionPolicy Bypass scopes the exemption to this one call
rem and does not change any machine setting.
rem
rem Uses pwsh (PowerShell 7+) when present, else the built-in powershell 5.1.
rem
rem ASCII only on purpose: cmd.exe parses this file line by line using the active
rem code page, so non-ASCII text here renders as mojibake on any machine whose
rem code page is not the one it was written for. The reasoning that would go in
rem Chinese comments lives in docs/decisions/windows-entry.md instead.
rem Pinned by tests/test_win_entry.py.
setlocal
set "PS=powershell"
where pwsh >nul 2>&1 && set "PS=pwsh"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0kb.ps1" %*
exit /b %ERRORLEVEL%
