"""Vercel Serverless Function Entrypoint.

Exposes the FastAPI application as 'app' for the @vercel/python runtime.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Garante que a raiz do projeto esteja no sys.path para resolucao de imports
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Define flag serverless explicitamente
os.environ.setdefault("VERCEL", "1")

from run_standalone import app  # noqa: E402
