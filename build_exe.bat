@echo off
cd /d "%~dp0"
echo ============================================
echo  TapoViewer.exe をビルドします(初回のみ)
echo ============================================
echo.
echo [1/3] 依存パッケージをインストール中...
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto err_pip
echo.
echo [2/3] exe をビルド中(数分かかります)...
python -m PyInstaller --onefile --noconsole --name TapoViewer --icon icon.ico --collect-all customtkinter --collect-all imageio_ffmpeg tapo_viewer.py
if errorlevel 1 goto err_build
echo.
echo [3/3] 完了!
echo.
echo   dist\TapoViewer.exe が生成されました。
echo   デスクトップ等にコピーして、ダブルクリックで起動できます。
echo.
pause
exit /b 0

:err_pip
echo.
echo パッケージのインストールに失敗しました。
echo Python がインストールされ、PATH が通っているか確認してください。
echo   確認コマンド: python --version
pause
exit /b 1

:err_build
echo.
echo exe のビルドに失敗しました。上のエラーメッセージを確認してください。
pause
exit /b 1
