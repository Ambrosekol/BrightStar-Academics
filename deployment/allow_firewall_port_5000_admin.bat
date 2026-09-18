@echo off
echo This script should be run as Administrator.
echo.
netsh advfirewall firewall add rule name="Crainbow CBT Local Server" dir=in action=allow protocol=TCP localport=5000 profile=private
echo.
echo Firewall rule command completed.
pause
