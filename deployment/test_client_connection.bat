@echo off
set /p SERVER_IP=Enter the CBT server IPv4 address (example 192.168.1.10): 
echo.
echo Testing network reachability...
ping %SERVER_IP%
echo.
echo If ping works, open:
echo http://%SERVER_IP%:5000/
echo.
pause
