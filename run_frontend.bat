@echo off
cd /d %~dp0frontend
echo === Poros (AI Distribution Administrator) - FRONTEND ===
echo Memasang dependensi (pertama kali agak lama)...
call npm install
echo.
echo Frontend jalan di http://localhost:5173
echo Biarkan jendela ini terbuka. Tutup untuk stop.
call .\node_modules\.bin\vite.cmd --host 0.0.0.0
pause
