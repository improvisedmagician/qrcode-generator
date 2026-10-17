@echo off
chcp 65001 > nul
echo ========================================================
echo  CRIANDO ATALHO NA ÁREA DE TRABALHO
echo ========================================================

set "TARGET=%~dp0dist\GeradorDeQRCode.exe"
if not exist "%TARGET%" (
    set "TARGET=%~dp0GeradorDeQRCode.exe"
)

if not exist "%TARGET%" (
    echo ERRO: Não foi encontrado o arquivo GeradorDeQRCode.exe.
    echo Certifique-se de que o executável está na mesma pasta.
    pause
    exit /b 1
)

set "SHORTCUT=%USERPROFILE%\Desktop\Gerador de QR Code.lnk"

powershell -ExecutionPolicy Bypass -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath = '%TARGET%'; $s.WorkingDirectory = '%~dp0'; $s.Description = 'Gerador de QR Code Estático e Permanente'; $s.Save()"

if exist "%SHORTCUT%" (
    echo.
    echo [SUCESSO] Atalho criado na sua Área de Trabalho!
    echo Basta clicar duas vezes no ícone "Gerador de QR Code" na sua Área de Trabalho.
    echo.
) else (
    echo.
    echo Falha ao criar atalho automaticamente.
    echo Você pode clicar com o botão direito no GeradorDeQRCode.exe e selecionar "Enviar para -> Área de trabalho".
    echo.
)

pause
