"""Script de compilação automatizada do executável Windows (.exe) com PyInstaller.

Gera um executável portátil único em dist/GeradorDeQRCode.exe
que pode ser enviado para qualquer cliente e executado em qualquer computador
sem necessidade de Python, VS Code ou qualquer dependência instalada.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent

def build():
    print("\n" + "=" * 70)
    print(" INICIANDO COMPILAÇÃO DO EXECUTÁVEL PORTÁTIL (.EXE)")
    print("=" * 70)

    # Verifica se PyInstaller está instalado no ambiente
    try:
        import PyInstaller
        print(f"-> PyInstaller detectado: versão {PyInstaller.__version__}")
    except ImportError:
        print("ERRO: PyInstaller não encontrado no ambiente atual.")
        print("Execute: pip install pyinstaller")
        sys.exit(1)

    dist_dir = ROOT_DIR / "dist"

    # Argumentos do PyInstaller
    cmd = [
        sys.executable,
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name", "GeradorDeQRCode",
        "--paths", str(ROOT_DIR),
        "--add-data", f"{ROOT_DIR / 'services'};services",
        # Hidden imports essenciais para Uvicorn, FastAPI e renderização gráfica
        "--hidden-import", "uvicorn",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols",
        "--hidden-import", "uvicorn.protocols.http",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "fastapi",
        "--hidden-import", "segno",
        "--hidden-import", "reportlab",
        "--hidden-import", "reportlab.lib",
        "--hidden-import", "reportlab.lib.units",
        "--hidden-import", "reportlab.lib.utils",
        "--hidden-import", "reportlab.pdfgen",
        "--hidden-import", "reportlab.pdfgen.canvas",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
        "--hidden-import", "PIL.ImageFont",
        "--hidden-import", "pydantic",
        "--hidden-import", "pydantic_core",
        "--hidden-import", "starlette",
        "--hidden-import", "fakeredis",
        "--hidden-import", "asyncio",
        "--collect-all", "reportlab",
        "--collect-all", "segno",
        str(ROOT_DIR / "run_standalone.py")
    ]

    print("-> Executando PyInstaller...")
    result = subprocess.run(cmd, cwd=str(ROOT_DIR))

    if result.returncode != 0:
        print("\n" + "!" * 70)
        print("FALHA NA COMPILAÇÃO!")
        print("!" * 70)
        sys.exit(result.returncode)

    exe_path = dist_dir / "GeradorDeQRCode.exe"
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print("\n" + "=" * 70)
        print(" SUCESSO: EXECUTÁVEL CRIADO COM ÊXITO!")
        print("=" * 70)
        print(f"-> Arquivo pronto: {exe_path}")
        print(f"-> Tamanho final:  {size_mb:.1f} MB")
        print("=" * 70)
        print("Você pode copiar o arquivo 'GeradorDeQRCode.exe' para qualquer computador.")
        print("Ao dar 2 cliques, o sistema abrirá automaticamente no navegador do usuário.")
        print("=" * 70 + "\n")
    else:
        print("ERRO: Executável não foi localizado na pasta dist.")

if __name__ == "__main__":
    build()
