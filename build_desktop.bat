@echo off
cd /d "%~dp0"
echo [1/2] Generating offline assets, RWS icon, and Word User Guide...
python scripts\generate_assets_and_guide.py
if errorlevel 1 exit /b 1

echo [2/2] Building standalone RWS Desktop Application (.exe)...
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name "RWS_Patent_TM_Estimator" ^
  --icon "assets\rws_icon.ico" ^
  --distpath "release" ^
  --add-data "static;static" ^
  --hidden-import "docx" ^
  --hidden-import "reportlab" ^
  --hidden-import "python_multipart" ^
  --hidden-import "multipart" ^
  --hidden-import "lxml._elementpath" ^
  --exclude-module "pandas" ^
  --exclude-module "numpy" ^
  --exclude-module "google" ^
  --exclude-module "grpc" ^
  desktop_app.py

echo.
echo Build complete! Shareable files are in: %~dp0release\
