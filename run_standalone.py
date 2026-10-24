"""Servidor Local Interativo da Plataforma SaaS de QR Codes Dinâmicos.

UI/UX moderna, limpa, responsiva e de alta densidade:
- Dimensões ajustadas para caber perfeitamente na tela (Desktop, Laptop, Tablet e Mobile)
- Alternância instantânea de visualização: Modo Grid (Cards compactos) e Modo Lista (Alta densidade)
- Resolução 100% à prova de falhas: nunca retorna 404 para slugs criados (Redis multi-host + fallback em memória)
- Suporte a IP local (Wi-Fi) para escanear QR codes diretamente com a câmera do celular
- Modal responsivo com altura máxima controlada (max-h-[88vh]) e barra de ações sempre visível
- CRUD completo com Live Preview em tempo real e proteção de contraste
- Modal de ampliação (Zoom) para escanear ou inspecionar o código em alta definição
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import csv
import html
import io
import json
import logging
import os
import re
import secrets
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import fakeredis
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from services.api.app.qr_render import InvalidDesign, QRDesign, render, validate, verify_readable
from services.redirect.app.capture import EventPipeline, build_event
from services.redirect.app.config import Settings
from services.redirect.app.main import DEEPLINK_HTML, NO_STORE
from services.redirect.app.resolver import Resolver
from services.redirect.app.rules import decide
from services.analytics_worker.worker.enrich import COLUMNS, enrich

CACHE_BUST_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("demo")

# Detecção automática do IP local da rede (para testes com câmera de smartphone)
def detect_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

LAN_IP = detect_lan_ip()

IS_SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
REDIS_URL = os.getenv("REDIS_URL")
if not REDIS_URL:
    upstash_rest_url = os.getenv("UPSTASH_REDIS_REST_URL")
    upstash_rest_token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if upstash_rest_url and upstash_rest_token:
        host = urlsplit(upstash_rest_url).netloc or upstash_rest_url.replace("https://", "").replace("http://", "").split("/")[0]
        REDIS_URL = f"rediss://default:{upstash_rest_token}@{host}:6379"

API_URL = os.getenv("API_URL")
if API_URL:
    log.info("Binding Vercel ativo para 'api': %s", API_URL)

if REDIS_URL:
    try:
        import redis as sync_redis_mod
        redis_sync = sync_redis_mod.from_url(REDIS_URL, decode_responses=True)
        redis_async = aioredis.from_url(REDIS_URL, decode_responses=True)
        log.info("Conectado ao Redis externo (Upstash / Cloud).")
    except Exception as exc:
        log.warning("Falha ao inicializar REDIS_URL (%r), usando fallback in-memory", exc)
        fake_server = fakeredis.FakeServer()
        redis_sync = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
        redis_async = fakeredis.FakeAsyncRedis(server=fake_server, decode_responses=True)
else:
    fake_server = fakeredis.FakeServer()
    redis_sync = fakeredis.FakeRedis(server=fake_server, decode_responses=True)
    redis_async = fakeredis.FakeAsyncRedis(server=fake_server, decode_responses=True)

# Registro de exclusões (tombstones) para impedir ressurgimento de slugs deletados
DELETED_SLUGS: set[str] = {"rede-03-85c4"}

# Banco de dados simulado em memória (códigos padrão ativos de demonstração)
IN_MEMORY_DB: dict[str, dict] = {
    "promo": {
        "v": 1,
        "name": "Banner Institucional São Paulo",
        "qr_id": "11111111-1111-1111-1111-111111111111",
        "workspace_id": "00000000-0000-0000-0000-000000000003",
        "status": "active",
        "default_url": "https://minhaloja.com.br/produtos",
        "fallback_url": "https://minhaloja.com.br/encerrado",
        "expires_at": None,
        "utm": {"utm_source": "qrcode", "utm_medium": "banner-sp"},
        "design": {
            "foreground": "#0F172A",
            "background": "#FFFFFF",
            "background_type": "solid",
            "background_color_end": None,
            "module_style": "rounded",
            "finder_style": "rounded",
            "frame_text": "ESCANEIE AQUI",
        },
        "rules": [],
        "created_at": "2026-10-16T18:00:00Z",
    },
    "blackfriday": {
        "v": 2,
        "name": "Campanha Black Friday 2026",
        "qr_id": "22222222-2222-2222-2222-222222222222",
        "workspace_id": "00000000-0000-0000-0000-000000000003",
        "status": "active",
        "default_url": "https://minhaloja.com.br/loja-normal",
        "fallback_url": "https://minhaloja.com.br/encerrado",
        "expires_at": None,
        "utm": {"utm_source": "qrcode", "utm_campaign": "bf2026"},
        "design": {
            "foreground": "#991B1B",
            "background": "#FFF1EB",
            "background_type": "gradient",
            "background_color_end": "#ACE0F9",
            "module_style": "square",
            "finder_style": "square",
            "frame_text": "BLACK FRIDAY 50% OFF",
        },
        "rules": [],
        "created_at": "2026-10-16T18:15:00Z",
    },
    "insta": {
        "v": 3,
        "name": "Cartão de Visitas — Instagram Direto",
        "qr_id": "33333333-3333-3333-3333-333333333333",
        "workspace_id": "00000000-0000-0000-0000-000000000003",
        "status": "active",
        "default_url": "https://instagram.com/empresa",
        "fallback_url": None,
        "expires_at": None,
        "utm": None,
        "design": {
            "foreground": "#4C1D95",
            "background": "#E0F2FE",
            "background_type": "gradient",
            "background_color_end": "#DCFCE7",
            "module_style": "dots",
            "finder_style": "dots",
            "frame_text": "SIGA NO INSTAGRAM",
        },
        "rules": [],
        "created_at": "2026-10-16T18:30:00Z",
    },
}

# Sincroniza em Redis sob múltiplos hostnames para tolerância total
KNOWN_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", LAN_IP]

# Persistência local em arquivo JSON (para modo Desktop offline / qualquer máquina sem internet ou Redis)
def get_local_db_path() -> Path:
    if "PYTEST_CURRENT_TEST" in os.environ:
        return Path(__file__).resolve().parent / ".pytest_local_db.json"
    app_data = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    if app_data:
        base_dir = Path(app_data) / "GeradorQRCode"
    else:
        base_dir = Path.home() / ".gerador_qrcode"
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir / "qrcodes_db.json"
    except Exception:
        return Path("qrcodes_db.json")

LOCAL_DB_FILE = get_local_db_path()

def save_local_db():
    if IS_SERVERLESS:
        return
    try:
        data_to_save = {
            "qrcodes": {k: v for k, v in IN_MEMORY_DB.items() if k not in DELETED_SLUGS},
            "deleted": list(DELETED_SLUGS),
        }
        with open(LOCAL_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log.warning("Falha ao salvar banco local em %s: %r", LOCAL_DB_FILE, exc)

def load_local_db():
    if IS_SERVERLESS or not LOCAL_DB_FILE.exists():
        return
    try:
        with open(LOCAL_DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        saved_qrcodes = data.get("qrcodes", {})
        saved_deleted = set(data.get("deleted", []))
        DELETED_SLUGS.update(saved_deleted)
        for s_slug, s_payload in saved_qrcodes.items():
            if s_slug not in DELETED_SLUGS:
                IN_MEMORY_DB[s_slug] = s_payload
        log.info("Banco local carregado com sucesso de %s (%d códigos)", LOCAL_DB_FILE, len(saved_qrcodes))
    except Exception as exc:
        log.warning("Falha ao carregar banco local de %s: %r", LOCAL_DB_FILE, exc)

# Carrega códigos gravados localmente no computador
load_local_db()

# Recupera túmulos de exclusão salvos no Redis para persistência inter-instâncias
if redis_sync:
    try:
        saved_deletions = redis_sync.smembers("deleted_qr_slugs")
        if saved_deletions:
            DELETED_SLUGS.update(saved_deletions)
        for d in list(DELETED_SLUGS):
            IN_MEMORY_DB.pop(d, None)
            redis_sync.srem("all_qr_slugs", d)
            redis_sync.delete(f"qr_meta:{d}")
            redis_sync.hdel("qr_clicks", d)
            redis_sync.sadd("deleted_qr_slugs", d)
            for h in KNOWN_HOSTS:
                redis_sync.delete(f"qr:{h}:{d}")
    except Exception as exc:
        log.warning("Falha ao ler deleted_qr_slugs do Redis: %r", exc)

# Remove imediatamente da memória quaisquer slugs que já foram deletados
for d in list(DELETED_SLUGS):
    IN_MEMORY_DB.pop(d, None)

def sync_redis_slug(slug: str, payload: dict):
    clean = slug.strip().lstrip("/").lower()
    if clean in DELETED_SLUGS:
        return
    payload_str = json.dumps(payload)
    if redis_sync:
        try:
            for h in KNOWN_HOSTS:
                redis_sync.set(f"qr:{h}:{clean}", payload_str, ex=86400)
            redis_sync.sadd("all_qr_slugs", clean)
            redis_sync.set(f"qr_meta:{clean}", payload_str)
        except Exception as exc:
            log.warning("Falha ao sincronizar slug %s no Redis: %r", clean, exc)
    save_local_db()

for slug, payload in list(IN_MEMORY_DB.items()):
    if slug not in DELETED_SLUGS:
        sync_redis_slug(slug, payload)

CAPTURED_SCANS: list[dict] = []
SCAN_COUNTS: dict[str, int] = {}

if redis_sync:
    try:
        saved_clicks = redis_sync.hgetall("qr_clicks")
        if saved_clicks:
            SCAN_COUNTS.update({k: int(v) for k, v in saved_clicks.items()})
    except Exception as exc:
        log.warning("Falha ao ler qr_clicks do Redis: %r", exc)


async def simulate_analytics_worker():
    log.info("Worker de Analytics iniciado em background.")
    while True:
        try:
            resp = await redis_async.xread({"scans": "0"}, count=100, block=500)
            if resp:
                for stream_name, messages in resp:
                    for msg_id, fields in messages:
                        enriched = enrich(fields, salt="demo-salt-lgpd")
                        data = dict(zip(COLUMNS, enriched))
                        CAPTURED_SCANS.append(data)
                        log.info(
                            "Scan capturado: slug=%s disp=%s so=%s hash=%s...",
                            data["slug"],
                            data["device_type"],
                            data["os"],
                            str(data["visitor_hash"])[:10],
                        )
                        await redis_async.xdel("scans", msg_id)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(1)


# Resolver resiliente: se falhar no Redis, consulta o banco em memória local ou Redis externo
async def fake_pg_fetch(hostname: str, slug: str):
    clean_slug = slug.strip().lstrip("/").lower()
    if clean_slug in DELETED_SLUGS:
        return None
    if REDIS_URL:
        try:
            if await redis_async.sismember("deleted_qr_slugs", clean_slug):
                DELETED_SLUGS.add(clean_slug)
                return None
        except Exception:
            pass

    item = IN_MEMORY_DB.get(clean_slug)
    if item is None and REDIS_URL:
        try:
            cached_meta = await redis_async.get(f"qr_meta:{clean_slug}")
            if cached_meta:
                item = json.loads(cached_meta)
                IN_MEMORY_DB[clean_slug] = item
        except Exception:
            pass

    if item:
        return {
            "id": item.get("qr_id", "00000000-0000-0000-0000-000000000000"),
            "workspace_id": item.get("workspace_id", "00000000-0000-0000-0000-000000000003"),
            "status": item["status"],
            "default_url": item["default_url"],
            "fallback_url": item.get("fallback_url"),
            "expires_at": None,
            "utm_params": item.get("utm"),
            "version": item.get("v", 1),
            "rules": item.get("rules", []),
        }
    return None


spool_path = Path("/tmp/spool") if IS_SERVERLESS else Path("./spool")
settings = Settings(spool_dir=spool_path)
resolver = Resolver(redis_async, fake_pg_fetch)
pipeline = EventPipeline(redis_async, stream="scans", maxlen=100000, spool_dir=settings.spool_dir, queue_size=10000)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task = None
    if not IS_SERVERLESS:
        pipeline.start()
        worker_task = asyncio.create_task(simulate_analytics_worker())
    yield
    if not IS_SERVERLESS:
        if worker_task:
            worker_task.cancel()
        await pipeline.stop()


from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="QR Dinâmico — Enterprise SaaS", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class QRCreateDTO(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=100)
    target_url: Optional[str] = ""
    foreground: str = "#0F172A"
    background: str = "#FFFFFF"
    background_type: str = "solid"  # "solid" | "gradient"
    background_color_end: Optional[str] = None
    frame_text: Optional[str] = "ESCANEIE AQUI"
    module_style: str = "rounded"
    status: str = "active"


class QRBatchCreateDTO(BaseModel):
    count: int = Field(default=10, ge=1, le=500)
    prefix: str = Field(default="qr-", max_length=50)
    name_prefix: str = Field(default="Código Lote", max_length=100)
    foreground: str = "#0F172A"
    background: str = "#FFFFFF"
    background_type: str = "solid"
    background_color_end: Optional[str] = None
    frame_text: Optional[str] = "ESCANEIE AQUI"
    module_style: str = "rounded"


class QRUpdateDTO(BaseModel):
    name: Optional[str] = None
    target_url: Optional[str] = None
    status: Optional[str] = None
    foreground: Optional[str] = None
    background: Optional[str] = None
    background_type: Optional[str] = None
    background_color_end: Optional[str] = None
    frame_text: Optional[str] = None
    module_style: Optional[str] = None


# =====================================================================
# UI / UX DESIGN COMPACTO, ELEGANTE E 100% RESPONSIVO
# =====================================================================
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR" class="h-full bg-zinc-50/80">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
  <title>QR Code Estático — Estúdio de Criação Permanente</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <script>
    tailwind.config = {
      theme: {
        extend: {
          fontFamily: {
            sans: ['"Plus Jakarta Sans"', 'sans-serif'],
            mono: ['"JetBrains Mono"', 'monospace'],
          }
        }
      }
    }
  </script>
  <style>
    /* Scrollbars elegantes e discretas */
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 9999px; }
    ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }

    /* Utilitário para rolagem horizontal sem barra de rolagem (mobile tabs) */
    .no-scrollbar::-webkit-scrollbar { display: none; }
    .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }

    /* Prevenção de auto-zoom indesejado no iOS Safari ao tocar em inputs */
    @media (max-width: 640px) {
      input, select, textarea {
        font-size: 16px !important;
      }
    }
  </style>
</head>
<body class="min-h-full font-sans text-zinc-900 antialiased flex flex-col overflow-x-hidden">

  <!-- Navbar Compacta e Funcional (56px) -->
  <header class="bg-white/95 backdrop-blur-md border-b border-zinc-200/80 sticky top-0 z-30 shadow-2xs transition-colors">
    <div class="max-w-7xl mx-auto px-3 sm:px-6 h-14 flex items-center justify-between gap-2 sm:gap-3">
      
      <!-- Brand & Status -->
      <div class="flex items-center space-x-2 sm:space-x-3 shrink-0">
        <div class="w-8 h-8 rounded-lg bg-zinc-900 flex items-center justify-center text-white text-sm shadow-xs shrink-0">
          <i class="fa-solid fa-qrcode"></i>
        </div>
        <div class="flex items-center space-x-1.5 sm:space-x-2">
          <span class="font-bold text-sm tracking-tight text-zinc-900">QR Estático</span>
          <span class="hidden sm:inline-block text-[9px] font-bold uppercase tracking-wider bg-emerald-50 text-emerald-700 px-1.5 py-0.5 rounded border border-emerald-200">100% Permanente</span>
        </div>
      </div>

      <!-- Botões de Ação -->
      <div class="flex items-center space-x-1.5 sm:space-x-2 shrink-0">
        <div class="hidden lg:flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200/60">
          <i class="fa-solid fa-bolt text-emerald-600 text-[10px]"></i>
          1º Scan Ativa • Controle Total
        </div>

        <!-- Seletor de Fundo do Dashboard (Desktop) -->
        <div class="relative hidden sm:block" id="container-seletor-fundo">
          <button type="button" onclick="toggleMenuFundo(event)" id="btn-menu-fundo" class="inline-flex items-center gap-1.5 bg-zinc-100 hover:bg-zinc-200/80 text-zinc-700 border border-zinc-200 text-xs font-semibold px-2.5 py-1.5 sm:px-3 sm:py-2 rounded-xl transition active:scale-95" title="Mudar plano de fundo do painel">
            <i class="fa-solid fa-palette text-indigo-600 text-xs"></i>
            <span>Fundo</span>
            <i class="fa-solid fa-chevron-down text-[9px] text-zinc-400"></i>
          </button>
          
          <div id="dropdown-fundo" class="hidden absolute right-0 mt-2 w-56 bg-white/95 backdrop-blur-md rounded-2xl shadow-xl border border-zinc-200/80 py-1.5 z-50">
            <div class="px-3 py-1.5 text-[10px] font-bold text-zinc-400 uppercase tracking-wider border-b border-zinc-100 flex items-center justify-between">
              <span>Fundo da Tela</span>
              <span class="text-[9px] text-indigo-600 font-semibold">Ao vivo</span>
            </div>
            <div class="p-1 space-y-0.5">
              <button type="button" onclick="aplicarFundoDashboard('default')" class="w-full px-2.5 py-1.5 rounded-lg text-left text-xs text-zinc-700 hover:bg-zinc-100 flex items-center justify-between transition">
                <span class="flex items-center gap-2">
                  <span class="w-3.5 h-3.5 rounded-full bg-zinc-200 border border-zinc-300 inline-block shrink-0"></span>
                  <span>Padrão (Clean Zinc)</span>
                </span>
                <i id="check-bg-default" class="fa-solid fa-check text-indigo-600 text-[11px]"></i>
              </button>
              <button type="button" onclick="aplicarFundoDashboard('aurora')" class="w-full px-2.5 py-1.5 rounded-lg text-left text-xs text-zinc-700 hover:bg-zinc-100 flex items-center justify-between transition">
                <span class="flex items-center gap-2">
                  <span class="w-3.5 h-3.5 rounded-full bg-gradient-to-tr from-teal-200 via-indigo-200 to-purple-200 border border-teal-300 inline-block shrink-0"></span>
                  <span>Gradiente Aurora</span>
                </span>
                <i id="check-bg-aurora" class="fa-solid fa-check text-indigo-600 text-[11px] hidden"></i>
              </button>
              <button type="button" onclick="aplicarFundoDashboard('sunset')" class="w-full px-2.5 py-1.5 rounded-lg text-left text-xs text-zinc-700 hover:bg-zinc-100 flex items-center justify-between transition">
                <span class="flex items-center gap-2">
                  <span class="w-3.5 h-3.5 rounded-full bg-gradient-to-tr from-amber-200 via-rose-200 to-indigo-200 border border-amber-300 inline-block shrink-0"></span>
                  <span>Gradiente Sunset</span>
                </span>
                <i id="check-bg-sunset" class="fa-solid fa-check text-indigo-600 text-[11px] hidden"></i>
              </button>
              <button type="button" onclick="aplicarFundoDashboard('mesh')" class="w-full px-2.5 py-1.5 rounded-lg text-left text-xs text-zinc-700 hover:bg-zinc-100 flex items-center justify-between transition">
                <span class="flex items-center gap-2">
                  <span class="w-3.5 h-3.5 rounded-full bg-gradient-to-tr from-indigo-300 via-purple-300 to-pink-300 border border-indigo-300 inline-block shrink-0"></span>
                  <span>Gradiente Cyber Mesh</span>
                </span>
                <i id="check-bg-mesh" class="fa-solid fa-check text-indigo-600 text-[11px] hidden"></i>
              </button>
              <button type="button" onclick="aplicarFundoDashboard('dark')" class="w-full px-2.5 py-1.5 rounded-lg text-left text-xs text-zinc-700 hover:bg-zinc-100 flex items-center justify-between transition">
                <span class="flex items-center gap-2">
                  <span class="w-3.5 h-3.5 rounded-full bg-gradient-to-tr from-slate-900 via-indigo-950 to-zinc-900 border border-zinc-500 inline-block shrink-0"></span>
                  <span>Gradiente Dark Slate</span>
                </span>
                <i id="check-bg-dark" class="fa-solid fa-check text-indigo-600 text-[11px] hidden"></i>
              </button>
            </div>
          </div>
        </div>

        <button onclick="abrirModalLote()" class="inline-flex items-center gap-1.5 bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 text-white text-xs font-bold px-2.5 py-1.5 sm:px-3.5 sm:py-2 rounded-xl shadow-xs transition active:scale-95 shrink-0">
          <i class="fa-solid fa-bolt text-[11px]"></i>
          <span><span class="hidden sm:inline">Gerar em </span>Lote</span>
        </button>

        <a href="/api/export-batch/pdf-sheet" target="_blank" class="hidden md:inline-flex items-center gap-1.5 bg-white hover:bg-zinc-100 text-zinc-700 border border-zinc-200 text-xs font-semibold px-2.5 py-1.5 sm:px-3 sm:py-2 rounded-xl transition" title="Baixar folha A4 com todos os códigos para impressão">
          <i class="fa-solid fa-file-pdf text-rose-500 text-[11px]"></i>
          <span>Folha PDF</span>
        </a>

        <a href="/api/export-batch/csv" target="_blank" class="hidden md:inline-flex items-center gap-1.5 bg-white hover:bg-zinc-100 text-zinc-700 border border-zinc-200 text-xs font-semibold px-2.5 py-1.5 sm:px-3 sm:py-2 rounded-xl transition" title="Exportar planilha CSV com todos os códigos">
          <i class="fa-solid fa-file-csv text-emerald-600 text-[11px]"></i>
          <span>CSV</span>
        </a>

        <button onclick="abrirModalCriar()" class="inline-flex items-center gap-1.5 bg-zinc-900 hover:bg-zinc-800 text-white text-xs font-semibold px-2.5 py-1.5 sm:px-3.5 sm:py-2 rounded-xl shadow-xs transition active:scale-95 shrink-0">
          <i class="fa-solid fa-plus text-[10px]"></i>
          <span><span class="hidden sm:inline">Novo</span> Avulso</span>
        </button>

        <!-- Botão Limpar Tudo (Desktop) -->
        <button onclick="limparTudo()" class="hidden sm:inline-flex items-center gap-1.5 bg-zinc-100 hover:bg-rose-50 text-zinc-500 hover:text-rose-600 border border-zinc-200 text-xs font-medium px-2 py-1.5 rounded-xl transition shrink-0" title="Limpar todos os QR Codes">
          <i class="fa-solid fa-trash-can text-[11px]"></i>
        </button>

        <!-- Menu "Mais Ações" para Mobile (< sm) -->
        <div class="relative sm:hidden" id="container-menu-mobile">
          <button type="button" onclick="toggleMenuMobile(event)" id="btn-menu-mobile" class="w-8 h-8 rounded-xl bg-zinc-100 hover:bg-zinc-200/80 text-zinc-700 border border-zinc-200 flex items-center justify-center transition active:scale-95 shrink-0" title="Mais opções">
            <i class="fa-solid fa-ellipsis-vertical text-xs"></i>
          </button>
          
          <div id="dropdown-mobile" class="hidden absolute right-0 mt-2 w-52 bg-white/95 backdrop-blur-md rounded-2xl shadow-xl border border-zinc-200/80 py-1.5 z-50 divide-y divide-zinc-100">
            <div class="p-1 space-y-0.5">
              <a href="/api/export-batch/pdf-sheet" target="_blank" class="w-full px-3 py-2 rounded-xl text-left text-xs font-semibold text-zinc-700 hover:bg-zinc-100 flex items-center gap-2.5 transition">
                <i class="fa-solid fa-file-pdf text-rose-500 text-xs"></i>
                <span>Baixar Folha A4 (PDF)</span>
              </a>
              <a href="/api/export-batch/csv" target="_blank" class="w-full px-3 py-2 rounded-xl text-left text-xs font-semibold text-zinc-700 hover:bg-zinc-100 flex items-center gap-2.5 transition">
                <i class="fa-solid fa-file-csv text-emerald-600 text-xs"></i>
                <span>Exportar Planilha CSV</span>
              </a>
            </div>

            <!-- Opções de Fundo no Mobile -->
            <div class="p-2 space-y-1">
              <span class="text-[10px] font-bold text-zinc-400 uppercase tracking-wider block px-1">Fundo da Tela</span>
              <div class="grid grid-cols-5 gap-1.5 px-1 py-1">
                <button type="button" onclick="aplicarFundoDashboard('default'); fecharMenuMobile();" class="w-6 h-6 rounded-full bg-zinc-200 border border-zinc-300" title="Padrão"></button>
                <button type="button" onclick="aplicarFundoDashboard('aurora'); fecharMenuMobile();" class="w-6 h-6 rounded-full bg-gradient-to-tr from-teal-200 via-indigo-200 to-purple-200 border border-teal-300" title="Aurora"></button>
                <button type="button" onclick="aplicarFundoDashboard('sunset'); fecharMenuMobile();" class="w-6 h-6 rounded-full bg-gradient-to-tr from-amber-200 via-rose-200 to-indigo-200 border border-amber-300" title="Sunset"></button>
                <button type="button" onclick="aplicarFundoDashboard('mesh'); fecharMenuMobile();" class="w-6 h-6 rounded-full bg-gradient-to-tr from-indigo-300 via-purple-300 to-pink-300 border border-indigo-300" title="Cyber"></button>
                <button type="button" onclick="aplicarFundoDashboard('dark'); fecharMenuMobile();" class="w-6 h-6 rounded-full bg-gradient-to-tr from-slate-900 via-indigo-950 to-zinc-900 border border-zinc-500" title="Dark"></button>
              </div>
            </div>

            <div class="p-1">
              <button type="button" onclick="fecharMenuMobile(); limparTudo();" class="w-full px-3 py-2 rounded-xl text-left text-xs font-semibold text-rose-600 hover:bg-rose-50 flex items-center gap-2.5 transition">
                <i class="fa-solid fa-trash-can text-xs text-rose-500"></i>
                <span>Limpar Todos os QR Codes</span>
              </button>
            </div>
          </div>
        </div>

      </div>

    </div>
  </header>

  <!-- Container Principal -->
  <main class="flex-1 max-w-7xl w-full mx-auto px-3 sm:px-6 py-3.5 sm:py-6 space-y-3.5 sm:space-y-5">

    <!-- KPI Strip: Métricas Operacionais em Tempo Real -->
    <section class="grid grid-cols-2 lg:grid-cols-4 gap-2 sm:gap-3">
      <div class="bg-white p-2.5 sm:p-3.5 rounded-xl border border-zinc-200/80 shadow-2xs flex flex-col justify-between gap-1.5 sm:flex-row sm:items-center">
        <div>
          <span class="text-[10px] sm:text-[11px] font-medium text-zinc-400 block truncate">Total de Códigos</span>
          <span id="kpi-total" class="text-lg sm:text-2xl font-bold text-zinc-900 tracking-tight">0</span>
        </div>
        <span class="text-[9px] sm:text-[10px] text-zinc-600 font-semibold bg-zinc-100 px-1.5 sm:px-2 py-0.5 rounded-md flex items-center gap-1 self-start sm:self-auto shrink-0">
          <i class="fa-solid fa-layer-group text-[8px] sm:text-[9px] text-indigo-600"></i> Cadastrados
        </span>
      </div>

      <div class="bg-white p-2.5 sm:p-3.5 rounded-xl border border-zinc-200/80 shadow-2xs flex flex-col justify-between gap-1.5 sm:flex-row sm:items-center">
        <div>
          <span class="text-[10px] sm:text-[11px] font-medium text-zinc-400 block truncate">Ativos (Online)</span>
          <span id="kpi-ativos" class="text-lg sm:text-2xl font-bold text-emerald-600 tracking-tight">0</span>
        </div>
        <span class="text-[9px] sm:text-[10px] text-emerald-700 font-semibold bg-emerald-50 px-1.5 sm:px-2 py-0.5 rounded-md flex items-center gap-1 self-start sm:self-auto shrink-0">
          <i class="fa-solid fa-circle-check text-[8px] sm:text-[9px]"></i> Redirecionando
        </span>
      </div>

      <div class="bg-white p-2.5 sm:p-3.5 rounded-xl border border-zinc-200/80 shadow-2xs flex flex-col justify-between gap-1.5 sm:flex-row sm:items-center">
        <div>
          <span class="text-[10px] sm:text-[11px] font-medium text-zinc-400 block truncate">Aguardando 1º Scan</span>
          <span id="kpi-unclaimed" class="text-lg sm:text-2xl font-bold text-amber-600 tracking-tight">0</span>
        </div>
        <span class="text-[9px] sm:text-[10px] text-amber-700 font-medium bg-amber-50 px-1.5 sm:px-2 py-0.5 rounded-md flex items-center gap-1 self-start sm:self-auto shrink-0">
          <i class="fa-solid fa-hourglass-half text-[8px] sm:text-[9px]"></i> Virgens
        </span>
      </div>

      <div class="bg-white p-2.5 sm:p-3.5 rounded-xl border border-zinc-200/80 shadow-2xs flex flex-col justify-between gap-1.5 sm:flex-row sm:items-center">
        <div>
          <span class="text-[10px] sm:text-[11px] font-medium text-zinc-400 block truncate">Desativados</span>
          <span id="kpi-desativados" class="text-lg sm:text-2xl font-bold text-rose-600 tracking-tight">0</span>
        </div>
        <span class="text-[9px] sm:text-[10px] text-rose-700 font-semibold bg-rose-50 px-1.5 sm:px-2 py-0.5 rounded-md flex items-center gap-1 self-start sm:self-auto shrink-0">
          <i class="fa-solid fa-pause text-[8px] sm:text-[9px]"></i> Pausados
        </span>
      </div>
    </section>

    <!-- Banner Informativo da Plataforma Dinâmica -->
    <section class="bg-white rounded-xl border border-indigo-200/70 bg-gradient-to-r from-indigo-50/50 via-white to-purple-50/30 p-3 sm:p-4 shadow-2xs flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 text-xs">
      <div class="flex items-start sm:items-center gap-3">
        <div class="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-600 to-purple-600 text-white flex items-center justify-center text-sm shadow-xs shrink-0 font-bold mt-0.5 sm:mt-0">
          <i class="fa-solid fa-bolt"></i>
        </div>
        <div>
          <h4 class="font-bold text-zinc-900 text-xs sm:text-sm">Plataforma Dinâmica: Criação em Lote & Ativação no 1º Scan</h4>
          <p class="text-zinc-600 text-[11px] mt-0.5">Gere lotes de QR codes para impressão antecipada. Ao escanear pela primeira vez, o cliente final cadastra o link de destino. Pelo painel, você ativa, desativa ou edita qualquer código a qualquer momento.</p>
        </div>
      </div>
      <div class="flex items-center gap-2 w-full sm:w-auto shrink-0">
        <button onclick="abrirModalLote()" class="w-full sm:w-auto justify-center inline-flex items-center gap-1.5 px-3 py-2 sm:py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white font-bold text-xs sm:text-[11px] shadow-2xs transition">
          <i class="fa-solid fa-plus text-[10px]"></i> Criar Novo Lote
        </button>
      </div>
    </section>

    <!-- Toolbar de Filtros, Busca e Alternância de Visualização -->
    <section class="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-2.5">
      
      <!-- Busca rápida -->
      <div class="relative flex-1 w-full sm:max-w-sm">
        <i class="fa-solid fa-magnifying-glass absolute left-3 top-1/2 -translate-y-1/2 text-xs text-zinc-400"></i>
        <input type="text" id="filtro-busca" oninput="filtrarQRs()" placeholder="Buscar por nome, slug ou URL..." class="w-full text-xs bg-white rounded-xl border border-zinc-200/80 pl-8 pr-3 py-2 outline-none focus:border-zinc-400 focus:ring-1 focus:ring-zinc-400 transition">
      </div>

      <!-- Filtros com rolagem suave no mobile e Alternância Grid/Lista -->
      <div class="w-full sm:w-auto flex items-center justify-between gap-2">
        <div class="flex items-center space-x-1 bg-zinc-100 p-0.5 rounded-xl border border-zinc-200 text-xs overflow-x-auto no-scrollbar flex-1 sm:flex-initial">
          <button type="button" onclick="setFiltroStatus('all')" id="tab-filtro-all" class="whitespace-nowrap px-2.5 py-1.5 sm:py-1 rounded-lg text-xs font-bold bg-white text-zinc-900 shadow-2xs shrink-0 transition">
            Todos (<span id="count-all">0</span>)
          </button>
          <button type="button" onclick="setFiltroStatus('active')" id="tab-filtro-active" class="whitespace-nowrap px-2.5 py-1.5 sm:py-1 rounded-lg text-xs font-medium text-zinc-500 hover:text-zinc-900 shrink-0 transition">
            Ativos (<span id="count-active">0</span>)
          </button>
          <button type="button" onclick="setFiltroStatus('unclaimed')" id="tab-filtro-unclaimed" class="whitespace-nowrap px-2.5 py-1.5 sm:py-1 rounded-lg text-xs font-medium text-zinc-500 hover:text-zinc-900 shrink-0 transition">
            1º Scan (<span id="count-unclaimed">0</span>)
          </button>
          <button type="button" onclick="setFiltroStatus('paused')" id="tab-filtro-paused" class="whitespace-nowrap px-2.5 py-1.5 sm:py-1 rounded-lg text-xs font-medium text-zinc-500 hover:text-zinc-900 shrink-0 transition">
            Pausados (<span id="count-paused">0</span>)
          </button>
        </div>

        <div class="flex items-center space-x-1 bg-zinc-100 p-0.5 rounded-xl border border-zinc-200 text-xs shrink-0">
          <button onclick="alternarModoVisualizacao('grid')" id="btn-view-grid" class="p-1.5 px-2 rounded-lg bg-white text-zinc-900 shadow-2xs transition" title="Cards">
            <i class="fa-solid fa-table-cells-large text-xs"></i>
          </button>
          <button onclick="alternarModoVisualizacao('list')" id="btn-view-list" class="p-1.5 px-2 rounded-lg text-zinc-500 hover:text-zinc-900 transition" title="Lista">
            <i class="fa-solid fa-bars text-xs"></i>
          </button>
        </div>
      </div>
    </section>
            <i class="fa-solid fa-bars text-xs"></i>
          </button>
        </div>
      </div>
    </section>

    <!-- Container Dinâmico: Cards ou Lista -->
    <section id="qr-container">
      <div id="qr-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3"></div>
      <div id="qr-list" class="hidden bg-white rounded-xl border border-zinc-200/80 shadow-2xs divide-y divide-zinc-100 overflow-hidden"></div>
    </section>

  </main>

  <!-- Modal Lote: Gerar Códigos em Escala (Prontos para 1º Scan) -->
  <div id="modal-lote" class="fixed inset-0 bg-zinc-900/60 backdrop-blur-xs z-50 flex items-end sm:items-center justify-center hidden p-0 sm:p-4 transition-all">
    <div class="bg-white rounded-t-3xl sm:rounded-2xl max-w-lg w-full max-h-[92vh] flex flex-col shadow-2xl border border-zinc-200 overflow-hidden transform transition-all">
      <div class="px-4 py-3 sm:px-5 sm:py-3.5 border-b border-zinc-100 flex items-center justify-between bg-zinc-50/70 shrink-0">
        <div class="flex items-center gap-2">
          <span class="w-7 h-7 rounded-lg bg-indigo-500/10 text-indigo-600 flex items-center justify-center text-sm font-bold">⚡</span>
          <h3 class="font-bold text-sm text-zinc-900">Gerar QR Codes em Lote (Em Escala)</h3>
        </div>
        <button onclick="fecharModalLote()" class="w-8 h-8 rounded-lg hover:bg-zinc-200/60 text-zinc-400 hover:text-zinc-700 flex items-center justify-center transition">
          <i class="fa-solid fa-xmark text-sm"></i>
        </button>
      </div>
      <form id="form-lote" onsubmit="gerarLote(event)" class="p-4 sm:p-5 space-y-3.5 sm:space-y-4 text-xs overflow-y-auto flex-1">
        <div>
          <label class="block font-semibold text-zinc-700 mb-1">Quantidade de Códigos a Gerar</label>
          <div class="flex items-center gap-3">
            <input type="range" id="lote-qtd-slider" min="2" max="100" value="10" oninput="document.getElementById('lote-qtd-num').value = this.value" class="flex-1 accent-indigo-600 cursor-pointer">
            <input type="number" id="lote-qtd-num" min="1" max="500" value="10" oninput="document.getElementById('lote-qtd-slider').value = this.value" class="w-20 px-2.5 py-1.5 border border-zinc-200 rounded-lg text-center font-bold text-zinc-800">
          </div>
          <p class="text-[10px] text-zinc-400 mt-1">Todos serão gerados em estado virgem, prontos para impressão e ativação no 1º scan.</p>
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block font-semibold text-zinc-700 mb-1">Prefixo do Link</label>
            <input type="text" id="lote-prefixo" value="mesa-" placeholder="Ex: mesa-, tag-, qr-" class="w-full px-3 py-2 border border-zinc-200 rounded-lg text-xs font-mono">
          </div>
          <div>
            <label class="block font-semibold text-zinc-700 mb-1">Nome Base</label>
            <input type="text" id="lote-nome" value="Mesa" placeholder="Ex: Mesa, Cartão, Produto" class="w-full px-3 py-2 border border-zinc-200 rounded-lg text-xs">
          </div>
        </div>
        <div>
          <label class="block font-semibold text-zinc-700 mb-1">Moldura do Código</label>
          <input type="text" id="lote-frame" value="ESCANEIE AQUI" class="w-full px-3 py-2 border border-zinc-200 rounded-lg text-xs uppercase font-bold">
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block font-semibold text-zinc-700 mb-1">Cor dos Módulos</label>
            <input type="color" id="lote-cor" value="#0F172A" class="w-full h-9 p-1 border border-zinc-200 rounded-lg cursor-pointer">
          </div>
          <div>
            <label class="block font-semibold text-zinc-700 mb-1">Estilo dos Pontos</label>
            <select id="lote-estilo" class="w-full h-9 px-2.5 border border-zinc-200 rounded-lg text-xs bg-white">
              <option value="rounded" selected>Arredondado Moderno</option>
              <option value="square">Quadrado Clássico</option>
              <option value="dots">Pontilhado (Dots)</option>
            </select>
          </div>
        </div>
        <div class="pt-3 border-t border-zinc-100 flex items-center justify-end gap-2 shrink-0">
          <button type="button" onclick="fecharModalLote()" class="flex-1 sm:flex-initial px-4 py-2.5 sm:py-2 border border-zinc-200 rounded-xl text-zinc-600 hover:bg-zinc-50 font-medium text-center">Cancelar</button>
          <button type="submit" id="btn-submit-lote" class="flex-1 sm:flex-initial px-5 py-2.5 sm:py-2 bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 text-white rounded-xl font-bold flex items-center justify-center gap-1.5 shadow-sm active:scale-95 transition text-center">
            <i class="fa-solid fa-bolt text-[11px]"></i>
            <span>Gerar Lote de Códigos</span>
          </button>
        </div>
      </form>
    </div>
  </div>

  <!-- Modal CRUD: Criar / Editar QR Code (Responsivo, max-h-88vh, sem cortes) -->
  <div id="modal-qr" class="fixed inset-0 bg-zinc-900/50 backdrop-blur-xs z-50 flex items-end sm:items-center justify-center hidden p-0 sm:p-4 transition-all">
    <div class="bg-white rounded-t-3xl sm:rounded-2xl max-w-2xl w-full max-h-[92vh] sm:max-h-[88vh] flex flex-col shadow-2xl border border-zinc-200 overflow-hidden transform transition-all">
      
      <!-- Modal Header (Fixo) -->
      <div class="px-4 py-3 sm:px-5 border-b border-zinc-100 flex items-center justify-between bg-zinc-50/70 shrink-0">
        <h3 id="modal-titulo" class="font-bold text-zinc-900 text-sm">Novo QR Code Dinâmico</h3>
        <button onclick="fecharModal()" class="w-8 h-8 rounded-lg hover:bg-zinc-200/60 text-zinc-400 hover:text-zinc-700 flex items-center justify-center transition">
          <i class="fa-solid fa-xmark text-sm"></i>
        </button>
      </div>

      <!-- Modal Body (Rolagem suave caso a tela seja pequena) -->
      <div class="overflow-y-auto p-4 sm:p-5 flex-1">
        <form id="form-qr" onsubmit="salvarQRCode(event)">
          <input type="hidden" id="qr-modo" value="criar">
          <input type="hidden" id="qr-slug-original" value="">

          <div class="grid grid-cols-1 md:grid-cols-12 gap-5">
            
            <!-- Coluna Esquerda: Formulário (7 colunas) -->
            <div class="md:col-span-7 space-y-3">
              <div>
                <label class="block text-[11px] font-semibold text-zinc-700 mb-0.5">Nome do QR Code</label>
                <input type="text" id="input-nome" required placeholder="Ex: Cardápio Mesa 1, Instagram Oficial" class="w-full text-xs rounded-lg border border-zinc-200 px-3 py-2 outline-none focus:border-zinc-900 focus:ring-1 focus:ring-zinc-900 transition" oninput="atualizarPreviewLive()">
              </div>

              <div>
                <label class="block text-[11px] font-semibold text-zinc-700 mb-0.5">Identificador Interno (Apelido)</label>
                <div class="flex items-center">
                  <span class="text-xs bg-zinc-100 border border-r-0 border-zinc-200 px-2.5 py-2 rounded-l-lg text-zinc-500 font-mono">/</span>
                  <input type="text" id="input-slug" required placeholder="cardapio-mesa" class="w-full text-xs rounded-r-lg border border-zinc-200 px-3 py-2 outline-none font-mono focus:border-zinc-900 focus:ring-1 focus:ring-zinc-900 transition" oninput="sanitizarSlug(this)">
                </div>
                <span class="text-[10px] text-zinc-400">Usado para nomear seus arquivos SVG/PNG e organizar seus códigos.</span>
              </div>

              <div>
                <label class="block text-[11px] font-semibold text-zinc-700 mb-0.5">URL de Destino Definitiva</label>
                <input type="url" id="input-url" required placeholder="https://instagram.com/sua-loja" class="w-full text-xs rounded-lg border border-zinc-200 px-3 py-2 outline-none focus:border-zinc-900 focus:ring-1 focus:ring-zinc-900 transition" oninput="atualizarPreviewLive()">
                <span class="text-[10px] text-emerald-600 font-medium">Esta URL será gravada diretamente na matriz do QR Code. 100% permanente e sem servidor.</span>
              </div>

              <div>
                <label class="block text-[11px] font-semibold text-zinc-700 mb-0.5">Texto da Moldura (CTA)</label>
                <input type="text" id="input-frame" value="ESCANEIE AQUI" maxlength="28" class="w-full text-xs rounded-lg border border-zinc-200 px-3 py-2 outline-none focus:border-zinc-900 focus:ring-1 focus:ring-zinc-900 transition" oninput="atualizarPreviewLive()">
              </div>

              <div class="grid grid-cols-2 gap-2.5">
                <div>
                  <label class="block text-[11px] font-semibold text-zinc-700 mb-0.5">Cor dos Módulos</label>
                  <div class="flex items-center space-x-1.5">
                    <input type="color" id="input-cor" value="#0F172A" class="w-8 h-7 rounded border border-zinc-200 p-0.5 cursor-pointer shrink-0" onchange="atualizarPreviewLive()">
                    <div class="flex items-center space-x-1 flex-wrap">
                      <button type="button" onclick="setCor('#0F172A')" class="w-4 h-4 rounded-full bg-[#0F172A] border border-zinc-300" title="Preto"></button>
                      <button type="button" onclick="setCor('#1E3A8A')" class="w-4 h-4 rounded-full bg-[#1E3A8A] border border-zinc-300" title="Azul"></button>
                      <button type="button" onclick="setCor('#991B1B')" class="w-4 h-4 rounded-full bg-[#991B1B] border border-zinc-300" title="Vinho"></button>
                      <button type="button" onclick="setCor('#065F46')" class="w-4 h-4 rounded-full bg-[#065F46] border border-zinc-300" title="Verde"></button>
                      <button type="button" onclick="setCor('#581C87')" class="w-4 h-4 rounded-full bg-[#581C87] border border-zinc-300" title="Roxo"></button>
                      <button type="button" onclick="setCor('#78350F')" class="w-4 h-4 rounded-full bg-[#78350F] border border-zinc-300" title="Dourado Escuro"></button>
                    </div>
                  </div>
                </div>

                <div>
                  <label class="block text-[11px] font-semibold text-zinc-700 mb-0.5">Estilo dos Módulos</label>
                  <select id="input-estilo" class="w-full text-xs rounded-lg border border-zinc-200 px-2.5 py-1.5 outline-none" onchange="atualizarPreviewLive()">
                    <option value="rounded">Arredondado</option>
                    <option value="square">Quadrado</option>
                    <option value="dots">Bolinhas</option>
                  </select>
                </div>
              </div>

              <!-- Plano de Fundo do QR Code (Sólido ou Gradiente) -->
              <div class="pt-2 border-t border-zinc-100 space-y-2">
                <div class="flex items-center justify-between">
                  <label class="block text-[11px] font-semibold text-zinc-700">Fundo do QR Code</label>
                  <div class="flex items-center space-x-1 bg-zinc-100 p-0.5 rounded-lg border border-zinc-200 text-[10px]">
                    <button type="button" onclick="setModoFundo('solid')" id="btn-bg-solid" class="font-semibold px-2 py-0.5 rounded bg-white text-zinc-900 shadow-2xs transition">Sólido</button>
                    <button type="button" onclick="setModoFundo('gradient')" id="btn-bg-grad" class="font-medium px-2 py-0.5 rounded text-zinc-600 hover:text-zinc-900 transition">Gradiente</button>
                  </div>
                </div>

                <!-- Fundo Sólido -->
                <div id="secao-fundo-solido" class="flex items-center space-x-2">
                  <input type="color" id="input-bg-cor" value="#FFFFFF" class="w-8 h-7 rounded border border-zinc-200 p-0.5 cursor-pointer shrink-0" onchange="atualizarPreviewLive()">
                  <div class="flex items-center space-x-1 flex-wrap">
                    <button type="button" onclick="setBgCor('#FFFFFF')" class="w-4 h-4 rounded-full bg-[#FFFFFF] border border-zinc-300 shadow-2xs" title="Branco"></button>
                    <button type="button" onclick="setBgCor('#F8FAFC')" class="w-4 h-4 rounded-full bg-[#F8FAFC] border border-zinc-300" title="Gelo"></button>
                    <button type="button" onclick="setBgCor('#FEF9C3')" class="w-4 h-4 rounded-full bg-[#FEF9C3] border border-zinc-300" title="Creme"></button>
                    <button type="button" onclick="setBgCor('#ECFDF5')" class="w-4 h-4 rounded-full bg-[#ECFDF5] border border-zinc-300" title="Menta"></button>
                    <button type="button" onclick="setBgCor('#F5F3FF')" class="w-4 h-4 rounded-full bg-[#F5F3FF] border border-zinc-300" title="Lavanda"></button>
                    <button type="button" onclick="setBgCor('#EFF6FF')" class="w-4 h-4 rounded-full bg-[#EFF6FF] border border-zinc-300" title="Céu"></button>
                  </div>
                  <span class="text-[10px] text-zinc-400 ml-auto hidden sm:inline">Tons claros seguros</span>
                </div>

                <!-- Fundo Gradiente -->
                <div id="secao-fundo-gradiente" class="hidden space-y-2">
                  <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    <div>
                      <label class="block text-[10px] text-zinc-500 mb-0.5 font-medium">Preset de Gradiente</label>
                      <select id="select-gradiente-preset" class="w-full text-xs rounded-lg border border-zinc-200 px-2 py-1 outline-none bg-white" onchange="selecionarPresetGradiente(this.value)">
                        <option value="sunset">🌅 Sunset Suave</option>
                        <option value="aurora">🌌 Aurora Boreal</option>
                        <option value="cotton">🌸 Algodão Doce</option>
                        <option value="champagne">🌾 Champagne</option>
                        <option value="peach">🍑 Pêssego Dourado</option>
                        <option value="custom">🎨 Personalizado</option>
                      </select>
                    </div>
                    <div>
                      <label class="block text-[10px] text-zinc-500 mb-0.5 font-medium">Início ➔ Fim (135°)</label>
                      <div class="flex items-center space-x-1.5">
                        <input type="color" id="input-grad-c1" value="#FFF1EB" class="w-8 h-7 rounded border border-zinc-200 p-0.5 cursor-pointer shrink-0" onchange="atualizarPreviewLive()">
                        <i class="fa-solid fa-arrow-right text-[9px] text-zinc-400"></i>
                        <input type="color" id="input-grad-c2" value="#ACE0F9" class="w-8 h-7 rounded border border-zinc-200 p-0.5 cursor-pointer shrink-0" onchange="atualizarPreviewLive()">
                        <span class="text-[10px] text-zinc-400 font-mono">135°</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div id="campo-status" class="hidden">
                <input type="hidden" id="input-status" value="active">
              </div>
            </div>

            <!-- Coluna Direita: Live Preview Compacto (5 colunas) -->
            <div class="md:col-span-5 bg-zinc-50 rounded-xl border border-zinc-200/80 p-3.5 flex flex-col items-center justify-between text-center">
              <div>
                <span class="text-[10px] font-bold text-zinc-400 uppercase tracking-wider block">Live Preview</span>
                <p class="text-xs text-zinc-800 font-semibold truncate max-w-[180px]" id="preview-nome">Catálogo Digital</p>
              </div>

              <div class="my-2 p-2 bg-white rounded-xl shadow-2xs border border-zinc-100 flex items-center justify-center">
                <div id="preview-svg-box" class="w-32 h-32 sm:w-36 sm:h-36 flex items-center justify-center">
                  <i class="fa-solid fa-spinner fa-spin text-zinc-300 text-xl"></i>
                </div>
              </div>

              <div class="space-y-1 w-full" id="preview-contrast-container">
                <div id="preview-contrast-badge">
                  <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[9px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">
                    <i class="fa-solid fa-check text-[8px]"></i> Contraste Seguro • Leitura 100%
                  </span>
                </div>
                <p class="text-[10px] text-zinc-500 font-mono truncate" id="preview-slug">https://...</p>
              </div>
            </div>

          </div>
        </form>
      </div>

      <!-- Modal Footer (Fixo na parte inferior) -->
      <div class="px-4 py-3 sm:px-5 sm:py-3 border-t border-zinc-100 flex flex-wrap sm:flex-nowrap items-center justify-between gap-2 bg-zinc-50/70 shrink-0">
        <div class="w-full sm:w-auto flex items-center justify-between sm:justify-start">
          <button type="button" id="btn-modal-excluir" onclick="deletarQRCodeAtualDoModal()" class="hidden px-3 py-2 sm:py-1.5 text-xs font-semibold text-rose-600 hover:text-white hover:bg-rose-600 rounded-lg border border-rose-200 hover:border-rose-600 transition flex items-center gap-1.5 shadow-2xs">
            <i class="fa-solid fa-trash-can"></i> Excluir QR Code
          </button>
          <span id="modal-footer-hint" class="text-[10px] text-emerald-600 font-medium hidden sm:inline"><i class="fa-solid fa-check mr-1"></i>Vetor pronto para impressão gráfica</span>
        </div>
        <div class="flex items-center space-x-2 ml-auto w-full sm:w-auto justify-end">
          <button type="button" onclick="fecharModal()" class="flex-1 sm:flex-initial px-3.5 py-2 sm:py-1.5 text-xs font-semibold text-zinc-600 hover:bg-zinc-200/60 rounded-lg transition text-center">Cancelar</button>
          <button type="button" onclick="document.getElementById('form-qr').requestSubmit()" id="btn-submit" class="flex-1 sm:flex-initial px-4 py-2 sm:py-1.5 text-xs font-semibold text-white bg-zinc-900 hover:bg-zinc-800 rounded-lg shadow-xs transition active:scale-95 text-center">Salvar</button>
        </div>
      </div>

    </div>
  </div>

  <!-- Modal Zoom: Visualizar QR Code Ampliado para Escaneamento Rápido -->
  <div id="modal-zoom" class="fixed inset-0 bg-zinc-900/60 backdrop-blur-xs z-50 flex items-center justify-center hidden p-4 transition-all">
    <div class="bg-white rounded-2xl max-w-xs sm:max-w-sm w-full p-5 sm:p-6 text-center shadow-2xl border border-zinc-200 relative">
      <button onclick="fecharModalZoom()" class="absolute right-3.5 top-3.5 text-zinc-400 hover:text-zinc-600 w-8 h-8 rounded-lg flex items-center justify-center transition">
        <i class="fa-solid fa-xmark"></i>
      </button>
      <h3 id="zoom-title" class="font-bold text-sm text-zinc-900 truncate">QR Code Ampliado</h3>
      <p id="zoom-slug" class="text-xs text-zinc-500 font-mono mt-0.5 truncate">/slug</p>
      
      <div class="my-4 p-3 bg-zinc-50 rounded-xl border border-zinc-100 flex items-center justify-center">
        <img id="zoom-img" src="" alt="QR Code Ampliado" class="w-48 h-48 sm:w-56 sm:h-56 object-contain">
      </div>

      <p class="text-[11px] text-zinc-400">Aponte a câmera do seu smartphone para abrir o destino direto.</p>
      
      <div class="mt-4 pt-3 border-t border-zinc-100 flex items-center justify-center gap-2">
        <a id="zoom-btn-svg" href="#" class="flex-1 sm:flex-initial px-3.5 py-2 sm:py-1.5 text-xs font-semibold text-zinc-700 bg-zinc-100 hover:bg-zinc-200 rounded-lg transition text-center">Baixar SVG</a>
        <a id="zoom-btn-png" href="#" class="flex-1 sm:flex-initial px-3.5 py-2 sm:py-1.5 text-xs font-semibold text-zinc-700 bg-zinc-100 hover:bg-zinc-200 rounded-lg transition text-center">Baixar PNG</a>
      </div>
    </div>
  </div>

  <!-- Toast Notification Flutuante -->
  <div id="toast" class="fixed bottom-4 right-4 bg-zinc-900 text-white text-xs font-medium px-3.5 py-2.5 rounded-xl shadow-xl flex items-center gap-2 transform translate-y-16 opacity-0 transition-all duration-300 z-50">
    <i id="toast-icon" class="fa-solid fa-check text-emerald-400"></i>
    <span id="toast-msg">Mensagem</span>
  </div>

  <script>
    let modoVisualizacao = 'grid'; // 'grid' ou 'list'
    let todosQRs = [];
    let filtroAtualStatus = 'all';
    const deletedSlugsCache = new Set(['rede-03-85c4']);

    // Purga definitiva de caches e dados zumbis de versoes legadas: o Redis online e a unica fonte da verdade
    try {
      localStorage.removeItem('custom_qrcodes');
      localStorage.removeItem('deleted_qr_slugs');
      localStorage.removeItem('cached_scans');
      localStorage.removeItem('qr_clicks');
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (k && (k.includes('custom') || k.includes('rede-03') || k.includes('qr'))) {
          localStorage.removeItem(k);
        }
      }
    } catch (e) {}

    function getDeletedSlugs() { return []; }
    function addDeletedSlug() {}
    function removeDeletedSlug() {}
    function getCustomQRs() { return {}; }
    function saveCustomQR() {}
    function deleteCustomQR() {}

    function deletarQRCodeAtualDoModal() {
      const slug = document.getElementById('qr-slug-original').value || document.getElementById('input-slug').value;
      const nome = document.getElementById('input-nome').value || slug;
      if (slug) {
        deletarQRCode(slug, nome);
      }
    }

    function escapeHtml(str) {
      if (str === null || str === undefined) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function escapeJs(str) {
      if (str === null || str === undefined) return '';
      return String(str)
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/"/g, '\\"')
        .replace(/\n/g, '\\n')
        .replace(/\r/g, '\\r');
    }

    function sanitizeUrl(url) {
      if (!url) return '#';
      const clean = String(url).trim();
      if (/^(https?:|\/)/i.test(clean)) {
        return escapeHtml(clean);
      }
      return '#';
    }

    function getActiveHost() {
      return window.location.host || 'localhost';
    }

    function alternarModoVisualizacao(modo) {
      modoVisualizacao = modo;
      const btnGrid = document.getElementById('btn-view-grid');
      const btnList = document.getElementById('btn-view-list');
      const divGrid = document.getElementById('qr-grid');
      const divList = document.getElementById('qr-list');

      if (modo === 'grid') {
        btnGrid.className = "p-1 px-2 rounded-md bg-white text-zinc-900 shadow-2xs";
        btnList.className = "p-1 px-2 rounded-md text-zinc-500 hover:text-zinc-900";
        divGrid.classList.remove('hidden');
        divList.classList.add('hidden');
      } else {
        btnList.className = "p-1 px-2 rounded-md bg-white text-zinc-900 shadow-2xs";
        btnGrid.className = "p-1 px-2 rounded-md text-zinc-500 hover:text-zinc-900";
        divGrid.classList.add('hidden');
        divList.classList.remove('hidden');
      }
      filtrarQRs();
    }

    function sanitizarSlug(input) {
      input.value = input.value.toLowerCase().replace(/[^a-z0-9_-]/g, '');
      atualizarPreviewLive();
    }

    function showToast(msg, icon = 'fa-check text-emerald-400') {
      const toast = document.getElementById('toast');
      document.getElementById('toast-msg').innerText = msg;
      document.getElementById('toast-icon').className = 'fa-solid ' + icon;
      toast.classList.remove('translate-y-16', 'opacity-0');
      setTimeout(() => toast.classList.add('translate-y-16', 'opacity-0'), 3000);
    }

    function setCor(hex) {
      document.getElementById('input-cor').value = hex;
      atualizarPreviewLive();
    }

    // Gerenciador de Plano de Fundo do Dashboard
    const THEME_CLASSES = {
      default: ['bg-zinc-50/80'],
      aurora: ['bg-gradient-to-br', 'from-slate-50', 'via-teal-50/40', 'to-indigo-100/50', 'bg-fixed'],
      sunset: ['bg-gradient-to-br', 'from-amber-50/40', 'via-rose-50/30', 'to-indigo-50/40', 'bg-fixed'],
      mesh: ['bg-gradient-to-br', 'from-indigo-50/60', 'via-purple-50/40', 'to-pink-50/30', 'bg-fixed'],
      dark: ['bg-gradient-to-br', 'from-slate-950', 'via-zinc-900', 'to-indigo-950', 'bg-fixed']
    };

    function toggleMenuFundo(event) {
      if (event) event.stopPropagation();
      fecharMenuMobile();
      const menu = document.getElementById('dropdown-fundo');
      if (menu) menu.classList.toggle('hidden');
    }

    function fecharMenuFundo() {
      const menu = document.getElementById('dropdown-fundo');
      if (menu) menu.classList.add('hidden');
    }

    function toggleMenuMobile(event) {
      if (event) event.stopPropagation();
      fecharMenuFundo();
      const menu = document.getElementById('dropdown-mobile');
      if (menu) menu.classList.toggle('hidden');
    }

    function fecharMenuMobile() {
      const menu = document.getElementById('dropdown-mobile');
      if (menu) menu.classList.add('hidden');
    }

    // Fechar menus flutuantes ao tocar fora deles
    document.addEventListener('click', function(e) {
      const cFundo = document.getElementById('container-seletor-fundo');
      if (cFundo && !cFundo.contains(e.target)) {
        fecharMenuFundo();
      }
      const cMobile = document.getElementById('container-menu-mobile');
      if (cMobile && !cMobile.contains(e.target)) {
        fecharMenuMobile();
      }
    });

    function aplicarFundoDashboard(theme) {
      localStorage.setItem('dashboard_bg_theme', theme);
      const body = document.body;
      const allClassList = [
        'bg-zinc-50/80', 'bg-fixed', 'bg-gradient-to-br',
        'from-slate-50', 'via-teal-50/40', 'to-indigo-100/50',
        'from-amber-50/40', 'via-rose-50/30', 'to-indigo-50/40',
        'from-indigo-50/60', 'via-purple-50/40', 'to-pink-50/30',
        'from-slate-950', 'via-zinc-900', 'to-indigo-950'
      ];
      body.classList.remove(...allClassList);
      const toAdd = THEME_CLASSES[theme] || THEME_CLASSES.default;
      body.classList.add(...toAdd);

      ['default', 'aurora', 'sunset', 'mesh', 'dark'].forEach(t => {
        const el = document.getElementById('check-bg-' + t);
        if (el) el.classList.toggle('hidden', t !== theme);
      });
      fecharMenuFundo();
    }

    // Configuração de Fundo do QR Code (Modal)
    let tipoFundoAtual = 'solid'; // 'solid' | 'gradient'

    const PRESETS_GRADIENTE = {
      sunset: ['#FFF1EB', '#ACE0F9'],
      aurora: ['#E0F2FE', '#DCFCE7'],
      cotton: ['#FCE7F3', '#E0E7FF'],
      champagne: ['#FEF3C7', '#FFFBEB'],
      peach: ['#FED7AA', '#FEE2E2'],
      custom: ['#E2E8F0', '#CBD5E1']
    };

    function setModoFundo(modo) {
      tipoFundoAtual = modo;
      const btnSolid = document.getElementById('btn-bg-solid');
      const btnGrad = document.getElementById('btn-bg-grad');
      const secSolid = document.getElementById('secao-fundo-solido');
      const secGrad = document.getElementById('secao-fundo-gradiente');

      if (modo === 'solid') {
        btnSolid.className = 'font-semibold px-2 py-0.5 rounded bg-white text-zinc-900 shadow-2xs transition';
        btnGrad.className = 'font-medium px-2 py-0.5 rounded text-zinc-600 hover:text-zinc-900 transition';
        secSolid.classList.remove('hidden');
        secGrad.classList.add('hidden');
      } else {
        btnGrad.className = 'font-semibold px-2 py-0.5 rounded bg-white text-zinc-900 shadow-2xs transition';
        btnSolid.className = 'font-medium px-2 py-0.5 rounded text-zinc-600 hover:text-zinc-900 transition';
        secGrad.classList.remove('hidden');
        secSolid.classList.add('hidden');
      }
      atualizarPreviewLive();
    }

    function setBgCor(hex) {
      document.getElementById('input-bg-cor').value = hex;
      atualizarPreviewLive();
    }

    function selecionarPresetGradiente(preset) {
      if (PRESETS_GRADIENTE[preset]) {
        const [c1, c2] = PRESETS_GRADIENTE[preset];
        document.getElementById('input-grad-c1').value = c1;
        document.getElementById('input-grad-c2').value = c2;
      }
      atualizarPreviewLive();
    }

    function saveStoredScans(scans) {
      try {
        localStorage.setItem('cached_scans', JSON.stringify(scans || []));
      } catch (e) {}
    }

    function getStoredScans() {
      try {
        return JSON.parse(localStorage.getItem('cached_scans') || '[]');
      } catch {
        return [];
      }
    }

    function getStoredClicks() {
      try {
        return JSON.parse(localStorage.getItem('qr_clicks') || '{}');
      } catch {
        return {};
      }
    }

    function saveStoredClicks(clicks) {
      try {
        localStorage.setItem('qr_clicks', JSON.stringify(clicks || {}));
      } catch {}
    }

    async function carregarDashboard() {
      try {
        const activeHost = getActiveHost();
        const ts = Date.now();
        const [resQRs, resScans] = await Promise.all([
          fetch(`/api/qrcodes?host_override=${encodeURIComponent(activeHost)}&_t=${ts}`, { cache: 'no-store' }),
          fetch(`/scans?_t=${ts}`, { cache: 'no-store' })
        ]);
        const serverQRs = await resQRs.json();
        const scansData = await resScans.json();

        // Mescla cliques do servidor com o cache do localStorage
        const storedClicks = getStoredClicks();
        const serverClicks = (scansData && scansData.clicks_by_slug) ? scansData.clicks_by_slug : {};
        const mergedClicks = { ...storedClicks };
        for (const [k, v] of Object.entries(serverClicks)) {
          mergedClicks[k] = Math.max(mergedClicks[k] || 0, Number(v) || 0);
        }
        saveStoredClicks(mergedClicks);
        todosQRs = (Array.isArray(serverQRs) ? serverQRs : [])
          .filter(q => !deletedSlugsCache.has(String(q.slug).toLowerCase().trim()))
          .map(q => {
            const k = String(q.slug).toLowerCase().trim();
            const clicks = Math.max(mergedClicks[k] || 0, Number(q.clicks) || 0);
            return { ...q, clicks };
          });

        // Mescla scans do servidor com o cache do localStorage para estabilidade serverless
        const existingScans = getStoredScans();
        const serverScans = (scansData && Array.isArray(scansData.recent_events)) ? scansData.recent_events : [];
        const mergedMap = new Map();
        [...serverScans, ...existingScans].forEach(e => {
          const key = `${e.ts}_${e.slug}_${e.visitor_hash || ''}`;
          if (!mergedMap.has(key)) {
            mergedMap.set(key, e);
          }
        });
        const allRecentScans = Array.from(mergedMap.values()).sort((a, b) => new Date(b.ts) - new Date(a.ts)).slice(0, 50);
        saveStoredScans(allRecentScans);

        // Atualiza KPIs
        const elTotal = document.getElementById('kpi-total');
        if (elTotal) elTotal.innerText = todosQRs.length;
        const elCountAll = document.getElementById('count-all');
        if (elCountAll) elCountAll.innerText = todosQRs.length;

        filtrarQRs();
      } catch (err) {
        console.error("Erro ao carregar dados:", err);
      }
    }

    let filtroStatusAtual = 'all';

    function setFiltroStatus(status) {
      filtroStatusAtual = status;
      ['all', 'active', 'unclaimed', 'paused'].forEach(s => {
        const el = document.getElementById(`tab-filtro-${s}`);
        if (el) {
          if (s === status) {
            el.className = 'px-2.5 py-1 rounded-md text-xs font-bold bg-white text-zinc-900 shadow-2xs';
          } else {
            el.className = 'px-2.5 py-1 rounded-md text-xs font-medium text-zinc-500 hover:text-zinc-900 transition';
          }
        }
      });
      filtrarQRs();
    }

    async function alternarStatus(slug, novoStatus) {
      try {
        const res = await fetch(`/api/qrcodes/${encodeURIComponent(slug)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: novoStatus })
        });
        if (res.ok) {
          showToast(novoStatus === 'paused' ? `QR Code /${slug} desativado!` : `QR Code /${slug} ativado!`, novoStatus === 'paused' ? 'fa-pause text-amber-500' : 'fa-play text-emerald-500');
          await carregarDashboard();
        } else {
          alert('Falha ao alterar status.');
        }
      } catch (err) {
        alert('Erro de conexão ao alterar status.');
      }
    }

    async function resetarPrimeiroScan(slug) {
      if (!confirm(`Deseja resetar o QR Code /${slug} para o estado inicial?\n\nO link atual será removido e o código voltará a aguardar a 1ª leitura.`)) {
        return;
      }
      try {
        const res = await fetch(`/api/qrcodes/${encodeURIComponent(slug)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'unclaimed', target_url: '' })
        });
        if (res.ok) {
          showToast(`QR Code /${slug} resetado para o 1º scan!`, 'fa-rotate-left text-blue-500');
          await carregarDashboard();
        }
      } catch (err) {
        alert('Erro ao resetar.');
      }
    }

    function abrirModalLote() {
      document.getElementById('modal-lote').classList.remove('hidden');
    }

    function fecharModalLote() {
      document.getElementById('modal-lote').classList.add('hidden');
    }

    async function gerarLote(ev) {
      ev.preventDefault();
      const btn = document.getElementById('btn-submit-lote');
      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Gerando...';

      const count = parseInt(document.getElementById('lote-qtd-num').value) || 10;
      const prefix = document.getElementById('lote-prefixo').value || 'qr-';
      const name_prefix = document.getElementById('lote-nome').value || 'Mesa';
      const frame_text = document.getElementById('lote-frame').value || 'ESCANEIE AQUI';
      const foreground = document.getElementById('lote-cor').value || '#0F172A';
      const module_style = document.getElementById('lote-estilo').value || 'rounded';

      try {
        const res = await fetch('/api/qrcodes/batch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            count,
            prefix,
            name_prefix,
            frame_text,
            foreground,
            module_style
          })
        });
        if (res.ok) {
          const data = await res.json();
          fecharModalLote();
          showToast(`${data.total} QR Codes gerados com sucesso!`, 'fa-bolt text-indigo-500');
          await carregarDashboard();
        } else {
          const err = await res.json();
          alert('Erro: ' + (err.detail || 'Falha ao gerar lote'));
        }
      } catch (err) {
        alert('Erro de rede ao gerar lote.');
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-bolt text-[11px]"></i> <span>Gerar Lote de Códigos</span>';
      }
    }

    function filtrarQRs() {
      const busca = (document.getElementById('filtro-busca').value || '').toLowerCase().trim();
      
      const filtrados = todosQRs.filter(qr => {
        const bateBusca = !busca || qr.name.toLowerCase().includes(busca) || qr.slug.toLowerCase().includes(busca) || (qr.default_url || '').toLowerCase().includes(busca);
        if (!bateBusca) return false;

        const isUnclaimed = qr.status === 'unclaimed' || !qr.default_url || qr.default_url.trim() === '';
        const isPaused = qr.status === 'paused' || qr.status === 'disabled';
        const isActive = qr.status === 'active' && !isUnclaimed;

        if (filtroStatusAtual === 'all') return true;
        if (filtroStatusAtual === 'active') return isActive;
        if (filtroStatusAtual === 'unclaimed') return isUnclaimed;
        if (filtroStatusAtual === 'paused') return isPaused;
        return true;
      });

      const total = todosQRs.length;
      const ativos = todosQRs.filter(q => q.status === 'active' && q.default_url && q.default_url.trim() !== '').length;
      const virgens = todosQRs.filter(q => q.status === 'unclaimed' || !q.default_url || q.default_url.trim() === '').length;
      const pausados = todosQRs.filter(q => q.status === 'paused' || q.status === 'disabled').length;

      document.getElementById('kpi-total').innerText = total;
      document.getElementById('kpi-ativos').innerText = ativos;
      document.getElementById('kpi-unclaimed').innerText = virgens;
      document.getElementById('kpi-desativados').innerText = pausados;

      document.getElementById('count-all').innerText = total;
      const cAct = document.getElementById('count-active'); if (cAct) cAct.innerText = ativos;
      const cUnc = document.getElementById('count-unclaimed'); if (cUnc) cUnc.innerText = virgens;
      const cPau = document.getElementById('count-paused'); if (cPau) cPau.innerText = pausados;

      if (modoVisualizacao === 'grid') {
        renderizarQRCards(filtrados);
      } else {
        renderizarQRLista(filtrados);
      }
    }

    // Modo Grid: Cards compactos e balanceados
    function renderizarQRCards(qrs) {
      const container = document.getElementById('qr-grid');
      if (qrs.length === 0) {
        container.innerHTML = `
          <div class="col-span-full py-12 text-center bg-white rounded-2xl border border-zinc-200/80">
            <div class="w-10 h-10 rounded-xl bg-zinc-100 text-zinc-400 mx-auto flex items-center justify-center text-lg mb-2">
              <i class="fa-solid fa-qrcode"></i>
            </div>
            <h4 class="font-bold text-zinc-800 text-xs">Nenhum QR Code encontrado</h4>
            <p class="text-[11px] text-zinc-400 mt-0.5">Crie um novo código para começar ou altere o filtro selecionado.</p>
          </div>
        `;
        return;
      }

      const activeHost = getActiveHost();

      container.innerHTML = qrs.map(qr => {
        const safeSlug = escapeHtml(qr.slug);
        const safeName = escapeHtml(qr.name);
        const jsSlug = escapeJs(qr.slug);
        const jsName = escapeJs(qr.name);
        const safeUrl = sanitizeUrl(qr.default_url);
        const displayUrl = escapeHtml((qr.default_url || '').replace(/^https?:\/\//, ''));
        const d = qr.design || {};
        const fgParam = encodeURIComponent(d.foreground || '#0F172A');
        const bgParam = encodeURIComponent(d.background || '#FFFFFF');
        const bgTypeParam = encodeURIComponent(d.background_type || 'solid');
        const bgEndParam = encodeURIComponent(d.background_color_end || '');
        const frameParam = encodeURIComponent(d.frame_text || 'ESCANEIE AQUI');
        const styleParam = encodeURIComponent(d.module_style || 'rounded');
        const vParam = `${qr.v || 1}_${(d.foreground || '').replace('#', '')}_${(d.background || '').replace('#', '')}`;
        const qrImgUrl = `/preview/${encodeURIComponent(qr.slug)}.svg?host_override=${encodeURIComponent(activeHost)}&v=${encodeURIComponent(vParam)}&color=${fgParam}&bg_color=${bgParam}&bg_type=${bgTypeParam}&bg_color_end=${bgEndParam}&frame=${frameParam}&style=${styleParam}`;
        const liveFallbackUrl = `/api/preview-live?slug=${encodeURIComponent(qr.slug)}&url=${encodeURIComponent(qr.default_url || '')}&color=${fgParam}&bg_color=${bgParam}&bg_type=${bgTypeParam}&bg_color_end=${bgEndParam}&frame=${frameParam}&style=${styleParam}&host=${encodeURIComponent(activeHost)}`;
        const jsLiveFallbackUrl = escapeJs(liveFallbackUrl);
        const jsQrImgUrl = escapeJs(qrImgUrl);

        const isUnclaimed = qr.status === 'unclaimed' || !qr.default_url || qr.default_url.trim() === '';
        const isPaused = qr.status === 'paused' || qr.status === 'disabled';

        let statusBadge = '';
        let statusActionBtn = '';
        if (isUnclaimed) {
          statusBadge = `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-amber-50 text-amber-700 border border-amber-200 shrink-0"><i class="fa-solid fa-hourglass-start text-[8px]"></i> Aguardando 1º Scan</span>`;
          statusActionBtn = `<a href="/${safeSlug}" target="_blank" class="px-2 py-1 rounded-md text-amber-900 bg-amber-100 hover:bg-amber-200 font-bold text-[11px] flex items-center gap-1 transition" title="Simular 1º scan"><i class="fa-solid fa-bolt text-[10px]"></i>Ativar</a>`;
        } else if (isPaused) {
          statusBadge = `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-rose-50 text-rose-700 border border-rose-200 shrink-0"><i class="fa-solid fa-pause text-[8px]"></i> Desativado</span>`;
          statusActionBtn = `<button onclick="alternarStatus('${jsSlug}', 'active')" class="px-2 py-1 rounded-md text-white bg-emerald-600 hover:bg-emerald-500 font-bold text-[11px] flex items-center gap-1 transition" title="Reativar link"><i class="fa-solid fa-play text-[9px]"></i>Ativar</button>`;
        } else {
          statusBadge = `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200 shrink-0"><i class="fa-solid fa-circle-check text-[8px]"></i> Ativo</span>`;
          statusActionBtn = `<button onclick="alternarStatus('${jsSlug}', 'paused')" class="px-2 py-1 rounded-md text-zinc-700 hover:text-rose-600 hover:bg-rose-50 font-medium text-[11px] flex items-center gap-1 transition" title="Desativar link"><i class="fa-solid fa-pause text-[9px]"></i>Pausar</button>`;
        }

        const resetBtn = !isUnclaimed ? `<button onclick="resetarPrimeiroScan('${jsSlug}')" class="px-1.5 py-1 rounded-md text-zinc-400 hover:text-zinc-700 hover:bg-zinc-100 font-medium text-[11px] transition" title="Resetar para novo 1º scan"><i class="fa-solid fa-rotate-left text-[10px]"></i></button>` : '';

        const destContent = isUnclaimed 
          ? `<span class="text-xs text-amber-600 font-medium italic block">Código virgem • Escaneie para ativar</span>`
          : `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="text-xs text-blue-600 hover:text-blue-800 font-semibold truncate block" title="${safeUrl}"><i class="fa-solid fa-arrow-up-right-from-square text-[9px] mr-1"></i>${displayUrl}</a>`;

        return `
          <div class="bg-white rounded-xl border border-zinc-200/80 shadow-2xs hover:border-zinc-300 hover:shadow-xs transition p-3.5 flex flex-col justify-between">
            <div>
              <!-- Top: Nome e Status -->
              <div class="flex items-center justify-between gap-2 mb-2">
                <h4 class="font-bold text-zinc-900 text-xs truncate" title="${safeName}">${safeName}</h4>
                ${statusBadge}
              </div>

              <!-- Meio: QR Code Thumbnail + Links -->
              <div class="flex items-center gap-3 bg-zinc-50/70 p-2.5 rounded-lg border border-zinc-100">
                <div class="w-18 h-18 sm:w-20 sm:h-20 shrink-0 bg-white p-1 rounded-lg border border-zinc-200/80 flex items-center justify-center cursor-pointer hover:border-zinc-400 transition" onclick="abrirModalZoom('${jsSlug}', '${jsName}', '${jsQrImgUrl}')" title="Clique para ampliar o QR Code">
                  <img src="${qrImgUrl}" alt="QR ${safeSlug}" class="w-full h-full object-contain" onerror="this.onerror=null; this.src='${jsLiveFallbackUrl}';">
                </div>
                <div class="min-w-0 flex-1 space-y-1.5">
                  <div>
                    <span class="text-[9px] font-bold text-zinc-400 uppercase tracking-wider block">Destino Vinculado</span>
                    ${destContent}
                  </div>
                  <div class="flex items-center gap-1">
                    <span class="text-[10px] text-zinc-500 font-mono bg-white px-1.5 py-0.5 rounded border border-zinc-200">/${safeSlug}</span>
                    <button onclick="copiarLink('${window.location.protocol}//${activeHost}/${safeSlug}')" class="text-zinc-400 hover:text-zinc-700 text-[11px] p-0.5" title="Copiar link do QR Code">
                      <i class="fa-regular fa-copy"></i>
                    </button>
                    ${qr.clicks !== undefined ? `<span class="text-[9px] text-zinc-400 ml-auto font-semibold"><i class="fa-solid fa-chart-simple text-[8px] text-indigo-500 mr-0.5"></i>${qr.clicks} scans</span>` : ''}
                  </div>
                </div>
              </div>
            </div>

            <!-- Rodapé: Ações e Downloads -->
            <div class="mt-2.5 pt-2 border-t border-zinc-100 flex flex-wrap sm:flex-nowrap items-center justify-between gap-1.5 sm:gap-2 text-xs">
              <div class="flex items-center space-x-1">
                ${statusActionBtn}
                <button onclick="abrirModalEditar('${jsSlug}')" class="px-2.5 py-1.5 sm:py-1 rounded-md text-zinc-700 bg-zinc-100 hover:bg-zinc-200 font-medium text-[11px] flex items-center gap-1 transition" title="Editar link e cores">
                  <i class="fa-solid fa-pen text-[9px] text-indigo-600"></i>Editar
                </button>
                ${resetBtn}
              </div>

              <div class="flex items-center space-x-1 ml-auto sm:ml-0">
                <a href="/export/${encodeURIComponent(qr.slug)}?format=svg&host_override=${encodeURIComponent(activeHost)}" class="px-2 py-1 rounded-md text-[10px] font-bold bg-zinc-100 text-zinc-700 hover:bg-zinc-200 transition" title="Baixar Vetor SVG (Gráfica)">SVG</a>
                <a href="/export/${encodeURIComponent(qr.slug)}?format=png&size=2048&host_override=${encodeURIComponent(activeHost)}" class="px-2 py-1 rounded-md text-[10px] font-bold bg-zinc-100 text-zinc-700 hover:bg-zinc-200 transition" title="Baixar PNG em Alta Resolução">PNG</a>
                <a href="/export/${encodeURIComponent(qr.slug)}?format=pdf&host_override=${encodeURIComponent(activeHost)}" class="px-2 py-1 rounded-md text-[10px] font-bold bg-zinc-100 text-zinc-700 hover:bg-zinc-200 transition" title="Baixar PDF Vetorial">PDF</a>
                <button onclick="deletarQRCode('${jsSlug}', '${jsName}')" class="p-1 px-1.5 rounded-md text-rose-500 hover:text-white hover:bg-rose-600 border border-transparent hover:border-rose-600 transition" title="Excluir QR Code">
                  <i class="fa-solid fa-trash-can text-xs"></i>
                </button>
              </div>
            </div>
          </div>
        `;
      }).join('');
    }

    // Modo Lista: Alta densidade corporativa (1 linha por QR code)
    function renderizarQRLista(qrs) {
      const container = document.getElementById('qr-list');
      if (qrs.length === 0) {
        container.innerHTML = '<div class="p-8 text-center text-xs text-zinc-400">Nenhum QR Code encontrado.</div>';
        return;
      }

      const activeHost = getActiveHost();

      container.innerHTML = qrs.map(qr => {
        const safeSlug = escapeHtml(qr.slug);
        const safeName = escapeHtml(qr.name);
        const jsSlug = escapeJs(qr.slug);
        const jsName = escapeJs(qr.name);
        const safeUrl = sanitizeUrl(qr.default_url);

        const d = qr.design || {};
        const fgParam = encodeURIComponent(d.foreground || '#0F172A');
        const bgParam = encodeURIComponent(d.background || '#FFFFFF');
        const bgTypeParam = encodeURIComponent(d.background_type || 'solid');
        const bgEndParam = encodeURIComponent(d.background_color_end || '');
        const frameParam = encodeURIComponent(d.frame_text || 'ESCANEIE AQUI');
        const styleParam = encodeURIComponent(d.module_style || 'rounded');
        const vParam = `${qr.v || 1}_${(d.foreground || '').replace('#', '')}_${(d.background || '').replace('#', '')}`;
        const qrImgUrl = `/preview/${encodeURIComponent(qr.slug)}.svg?host_override=${encodeURIComponent(activeHost)}&v=${encodeURIComponent(vParam)}&color=${fgParam}&bg_color=${bgParam}&bg_type=${bgTypeParam}&bg_color_end=${bgEndParam}&frame=${frameParam}&style=${styleParam}`;
        const liveFallbackUrl = `/api/preview-live?slug=${encodeURIComponent(qr.slug)}&url=${encodeURIComponent(qr.default_url || '')}&color=${fgParam}&bg_color=${bgParam}&bg_type=${bgTypeParam}&bg_color_end=${bgEndParam}&frame=${frameParam}&style=${styleParam}&host=${encodeURIComponent(activeHost)}`;
        const jsLiveFallbackUrl = escapeJs(liveFallbackUrl);
        const jsQrImgUrl = escapeJs(qrImgUrl);

        const isUnclaimed = qr.status === 'unclaimed' || !qr.default_url || qr.default_url.trim() === '';
        const isPaused = qr.status === 'paused' || qr.status === 'disabled';

        let statusBadge = '';
        let statusActionBtn = '';
        if (isUnclaimed) {
          statusBadge = `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-amber-50 text-amber-700 border border-amber-200">Aguardando 1º Scan</span>`;
          statusActionBtn = `<a href="/${safeSlug}" target="_blank" class="px-2 py-1 rounded bg-amber-100 hover:bg-amber-200 text-amber-900 text-[11px] font-bold">Ativar</a>`;
        } else if (isPaused) {
          statusBadge = `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-rose-50 text-rose-700 border border-rose-200">Desativado</span>`;
          statusActionBtn = `<button onclick="alternarStatus('${jsSlug}', 'active')" class="px-2 py-1 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-[11px] font-bold">Ativar</button>`;
        } else {
          statusBadge = `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">Ativo</span>`;
          statusActionBtn = `<button onclick="alternarStatus('${jsSlug}', 'paused')" class="px-2 py-1 rounded bg-zinc-100 hover:bg-rose-50 text-zinc-700 hover:text-rose-600 text-[11px] font-medium">Pausar</button>`;
        }

        const resetBtn = !isUnclaimed ? `<button onclick="resetarPrimeiroScan('${jsSlug}')" class="px-1.5 py-1 rounded bg-zinc-100 hover:bg-zinc-200 text-zinc-500 text-[11px]" title="Resetar para novo 1º scan"><i class="fa-solid fa-rotate-left"></i></button>` : '';

        return `
          <div class="p-3 sm:px-4 flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 sm:gap-3 hover:bg-zinc-50/70 transition">
            <div class="flex items-center space-x-3 min-w-0">
              <div class="w-12 h-12 sm:w-10 sm:h-10 shrink-0 bg-white p-0.5 rounded-lg border border-zinc-200 flex items-center justify-center cursor-pointer hover:border-zinc-400 transition" onclick="abrirModalZoom('${jsSlug}', '${jsName}', '${jsQrImgUrl}')" title="Clique para ampliar">
                <img src="${qrImgUrl}" alt="QR ${safeSlug}" class="w-full h-full object-contain" onerror="this.onerror=null; this.src='${jsLiveFallbackUrl}';">
              </div>
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-1.5 flex-wrap">
                  <h4 class="font-bold text-xs text-zinc-900 truncate" title="${safeName}">${safeName}</h4>
                  ${statusBadge}
                </div>
                <div class="flex items-center gap-1.5 text-[11px] text-zinc-500 mt-0.5 truncate">
                  <span class="font-mono text-zinc-800 font-semibold shrink-0">/${safeSlug}</span>
                  <span>•</span>
                  <span class="truncate">${isUnclaimed ? '<span class="text-amber-600 italic">Código virgem</span>' : `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="text-blue-600 hover:text-blue-800 truncate">${safeUrl}</a>`}</span>
                </div>
              </div>
            </div>

            <div class="flex items-center justify-between sm:justify-end gap-1.5 text-xs pt-2 sm:pt-0 border-t sm:border-t-0 border-zinc-100 flex-wrap">
              <div class="flex items-center gap-1">
                ${statusActionBtn}
                <button onclick="copiarLink('${window.location.protocol}//${activeHost}/${safeSlug}')" class="px-2 py-1 rounded-lg bg-zinc-100 hover:bg-zinc-200 text-zinc-700 text-[11px] font-medium" title="Copiar link curto">Copiar</button>
                <button onclick="abrirModalEditar('${jsSlug}')" class="px-2 py-1 rounded-lg bg-zinc-100 hover:bg-zinc-200 text-zinc-700 text-[11px] font-medium">Editar</button>
                ${resetBtn}
              </div>
              <div class="flex items-center gap-1">
                <a href="/export/${encodeURIComponent(qr.slug)}?format=svg&host_override=${encodeURIComponent(activeHost)}" class="text-[10px] font-bold bg-zinc-100 hover:bg-zinc-200 rounded-lg text-zinc-700 px-2 py-1">SVG</a>
                <a href="/export/${encodeURIComponent(qr.slug)}?format=png&size=2048&host_override=${encodeURIComponent(activeHost)}" class="text-[10px] font-bold bg-zinc-100 hover:bg-zinc-200 rounded-lg text-zinc-700 px-2 py-1">PNG</a>
                <a href="/export/${encodeURIComponent(qr.slug)}?format=pdf&host_override=${encodeURIComponent(activeHost)}" class="text-[10px] font-bold bg-zinc-100 hover:bg-zinc-200 rounded-lg text-zinc-700 px-2 py-1">PDF</a>
                <button onclick="deletarQRCode('${jsSlug}', '${jsName}')" class="p-1 px-1.5 text-rose-500 hover:text-white hover:bg-rose-600 rounded-lg transition" title="Excluir QR Code"><i class="fa-solid fa-trash-can"></i></button>
              </div>
            </div>
          </div>
        `;
      }).join('');
    }

    let lastRenderedScansJson = '';
    function renderizarTabelaScans(events) {
      const tbody = document.getElementById('scans-tbody');
      if (!tbody) return;
      const currentJson = JSON.stringify(events || []);
      if (currentJson === lastRenderedScansJson) {
        return; // Dados inalterados, evita repintar e piscar a tabela
      }
      lastRenderedScansJson = currentJson;

      if (!events || events.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-6 text-center text-zinc-400 font-sans text-xs">Nenhum scan registrado ainda. Aponte a câmera do celular para qualquer código!</td></tr>';
        return;
      }
      tbody.innerHTML = events.slice(0, 30).map(ev => {
        const hora = new Date(ev.ts).toLocaleTimeString('pt-BR');
        const isMob = ev.device === 'mobile';
        const icon = isMob ? 'fa-mobile-screen text-indigo-600' : 'fa-laptop text-zinc-400';
        const safeSlug = escapeHtml(ev.slug);
        const safeDest = sanitizeUrl(ev.destination);
        const safeDevice = escapeHtml(ev.device);
        const safeOS = escapeHtml(ev.os);
        const safeBrowser = escapeHtml(ev.browser);
        const safeHash = escapeHtml(ev.visitor_hash ? ev.visitor_hash.substring(0, 12) : '---');
        return `
          <tr class="hover:bg-zinc-50 transition">
            <td class="px-4 py-2 text-zinc-500">${hora}</td>
            <td class="px-4 py-2 font-semibold text-zinc-900">/${safeSlug}</td>
            <td class="px-4 py-2 text-zinc-600 max-w-xs truncate" title="${safeDest}">${safeDest}</td>
            <td class="px-4 py-2 capitalize"><i class="fa-solid ${icon} mr-1"></i> ${safeDevice}</td>
            <td class="px-4 py-2 text-zinc-500">${safeOS} • ${safeBrowser}</td>
            <td class="px-4 py-2 text-zinc-400 font-mono text-[10px]">${safeHash}...</td>
          </tr>
        `;
      }).join('');
    }

    // Modal Control: Create & Edit
    function abrirModalCriar() {
      document.getElementById('modal-titulo').innerText = "Novo QR Code Estático";
      document.getElementById('btn-submit').innerText = "Gerar QR Code Estático";
      document.getElementById('qr-modo').value = "criar";
      document.getElementById('qr-slug-original').value = "";
      document.getElementById('campo-status').classList.add('hidden');
      document.getElementById('input-slug').disabled = false;
      document.getElementById('form-qr').reset();
      document.getElementById('input-cor').value = "#0F172A";
      document.getElementById('input-bg-cor').value = "#FFFFFF";
      document.getElementById('input-grad-c1').value = "#FFF1EB";
      document.getElementById('input-grad-c2').value = "#ACE0F9";
      document.getElementById('select-gradiente-preset').value = "sunset";
      setModoFundo('solid');
      document.getElementById('input-frame').value = "ESCANEIE AQUI";
      const btnExcluir = document.getElementById('btn-modal-excluir');
      if (btnExcluir) btnExcluir.classList.add('hidden');
      const hint = document.getElementById('modal-footer-hint');
      if (hint) hint.classList.remove('hidden');
      document.getElementById('modal-qr').classList.remove('hidden');
      atualizarPreviewLive();
    }

    function abrirModalEditar(slug) {
      const qr = todosQRs.find(q => q.slug === slug);
      if (!qr) return;

      document.getElementById('modal-titulo').innerText = `Editar Design do QR Code`;
      document.getElementById('btn-submit').innerText = "Salvar Alterações";
      document.getElementById('qr-modo').value = "editar";
      document.getElementById('qr-slug-original').value = slug;
      document.getElementById('campo-status').classList.add('hidden');

      document.getElementById('input-nome').value = qr.name;
      document.getElementById('input-slug').value = qr.slug;
      document.getElementById('input-slug').disabled = true; // Slug fixo para manter consistência interna
      document.getElementById('input-url').value = qr.default_url;
      const d = qr.design || {};
      document.getElementById('input-frame').value = d.frame_text || "ESCANEIE AQUI";
      document.getElementById('input-cor').value = d.foreground || "#0F172A";
      document.getElementById('input-estilo').value = d.module_style || "rounded";
      document.getElementById('input-status').value = qr.status || "active";

      const bgType = d.background_type || 'solid';
      setModoFundo(bgType);
      document.getElementById('input-bg-cor').value = d.background || "#FFFFFF";
      if (bgType === 'gradient' && d.background_color_end) {
        document.getElementById('input-grad-c1').value = d.background || "#FFF1EB";
        document.getElementById('input-grad-c2').value = d.background_color_end || "#ACE0F9";
      }

      const btnExcluir = document.getElementById('btn-modal-excluir');
      if (btnExcluir) btnExcluir.classList.remove('hidden');
      const hint = document.getElementById('modal-footer-hint');
      if (hint) hint.classList.add('hidden');

      document.getElementById('modal-qr').classList.remove('hidden');
      atualizarPreviewLive();
    }

    function fecharModal() {
      document.getElementById('modal-qr').classList.add('hidden');
    }

    // Modal Zoom
    function abrirModalZoom(slug, name, imgUrl) {
      document.getElementById('zoom-title').innerText = name;
      document.getElementById('zoom-slug').innerText = `/${slug}`;
      const zoomImg = document.getElementById('zoom-img');
      const activeHost = getActiveHost();
      zoomImg.onerror = function() {
        this.onerror = null;
        this.src = `/api/preview-live?slug=${encodeURIComponent(slug)}&host=${encodeURIComponent(activeHost)}`;
      };
      zoomImg.src = imgUrl;
      document.getElementById('zoom-btn-svg').href = `/export/${slug}?format=svg&host_override=${encodeURIComponent(activeHost)}`;
      document.getElementById('zoom-btn-png').href = `/export/${slug}?format=png&size=2048&host_override=${encodeURIComponent(activeHost)}`;
      document.getElementById('modal-zoom').classList.remove('hidden');
    }

    function fecharModalZoom() {
      document.getElementById('modal-zoom').classList.add('hidden');
    }

    function calcularContraste(fg, bg = '#FFFFFF') {
      try {
        const getLum = hex => {
          const r = parseInt(hex.slice(1, 3), 16) / 255;
          const g = parseInt(hex.slice(3, 5), 16) / 255;
          const b = parseInt(hex.slice(5, 7), 16) / 255;
          const toLum = c => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
          return 0.2126 * toLum(r) + 0.7152 * toLum(g) + 0.0722 * toLum(b);
        };
        const L1 = getLum(fg);
        const L2 = getLum(bg);
        return (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
      } catch (e) {
        return 21;
      }
    }

    let previewTimer = null;
    function atualizarPreviewLive() {
      clearTimeout(previewTimer);
      previewTimer = setTimeout(async () => {
        const nome = document.getElementById('input-nome').value || "Meu QR Code";
        const rawSlug = document.getElementById('input-slug').value || "link";
        const slug = rawSlug.toLowerCase().replace(/[^a-z0-9_-]/g, '') || "link";
        const rawUrl = (document.getElementById('input-url').value || '').trim();
        const urlDestino = rawUrl || "https://instagram.com/empresa";
        
        document.getElementById('preview-nome').innerText = nome;
        document.getElementById('preview-slug').innerText = urlDestino.replace(/^https?:\/\//, '');

        const cor = document.getElementById('input-cor').value;
        const frame = document.getElementById('input-frame').value;
        const estilo = document.getElementById('input-estilo').value;
        const modoBg = tipoFundoAtual;
        const bgCor = modoBg === 'gradient' ? document.getElementById('input-grad-c1').value : document.getElementById('input-bg-cor').value;
        const bgCorEnd = document.getElementById('input-grad-c2').value;
        const activeHost = getActiveHost();

        // Verificação de Contraste em tempo real (WCAG 4:1)
        const ratio1 = calcularContraste(cor, bgCor);
        const ratio2 = modoBg === 'gradient' ? calcularContraste(cor, bgCorEnd) : ratio1;
        const ratio = Math.min(ratio1, ratio2);
        const badge = document.getElementById('preview-contrast-badge');
        if (ratio >= 3.0) {
          badge.innerHTML = `
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[9px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">
              <i class="fa-solid fa-check text-[8px]"></i> Contraste Alto (${ratio.toFixed(1)}:1) • Leitura 100%
            </span>
          `;
        } else if (ratio >= 1.35) {
          badge.innerHTML = `
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[9px] font-semibold bg-blue-50 text-blue-700 border border-blue-200">
              <i class="fa-solid fa-palette text-[8px]"></i> Cor Ativa (${ratio.toFixed(1)}:1) • Personalizada
            </span>
          `;
        } else {
          badge.innerHTML = `
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[9px] font-semibold bg-amber-50 text-amber-800 border border-amber-200" title="Contraste baixo com o fundo">
              <i class="fa-solid fa-triangle-exclamation text-[8px] text-amber-600"></i> Baixo contraste (${ratio.toFixed(1)}:1) • Ajustado
            </span>
          `;
        }

        try {
          const res = await fetch(`/api/preview-live?slug=${encodeURIComponent(slug)}&url=${encodeURIComponent(urlDestino)}&color=${encodeURIComponent(cor)}&frame=${encodeURIComponent(frame)}&style=${encodeURIComponent(estilo)}&host=${encodeURIComponent(activeHost)}&bg_type=${encodeURIComponent(modoBg)}&bg_color=${encodeURIComponent(bgCor)}&bg_color_end=${encodeURIComponent(bgCorEnd)}`);
          if (res.ok) {
            const svg = await res.text();
            document.getElementById('preview-svg-box').innerHTML = svg;
          }
        } catch (e) {
          console.error(e);
        }
      }, 150);
    }

    async function salvarQRCode(e) {
      e.preventDefault();
      const modo = document.getElementById('qr-modo').value;
      const rawSlug = document.getElementById('input-slug').value.trim();
      const slug = rawSlug.toLowerCase().replace(/[^a-z0-9_-]/g, '');

      if (!slug) {
        alert('Por favor, informe um identificador (slug) válido.');
        return;
      }

      const modoBg = tipoFundoAtual;
      const bgCor = modoBg === 'gradient' ? document.getElementById('input-grad-c1').value : document.getElementById('input-bg-cor').value;
      const bgCorEnd = modoBg === 'gradient' ? document.getElementById('input-grad-c2').value : null;

      const payload = {
        name: document.getElementById('input-nome').value.trim(),
        target_url: document.getElementById('input-url').value.trim(),
        foreground: document.getElementById('input-cor').value,
        background: bgCor,
        background_type: modoBg,
        background_color_end: bgCorEnd,
        frame_text: document.getElementById('input-frame').value,
        module_style: document.getElementById('input-estilo').value,
      };

      if (modo === 'criar') {
        payload.slug = slug;
        const res = await fetch('/api/qrcodes', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (res.ok) {
          fecharModal();
          showToast(`QR Code /${slug} criado com sucesso!`);
          await carregarDashboard();
        } else {
          const err = await res.json();
          alert('Erro: ' + (err.detail || 'Falha ao criar QR Code'));
        }
      } else {
        payload.status = document.getElementById('input-status').value;
        const originalSlug = document.getElementById('qr-slug-original').value;
        const res = await fetch(`/api/qrcodes/${encodeURIComponent(originalSlug)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (res.ok) {
          fecharModal();
          showToast(`QR Code /${originalSlug} atualizado com sucesso!`);
          await carregarDashboard();
        } else {
          const err = await res.json();
          alert('Erro: ' + (err.detail || 'Falha ao atualizar QR Code'));
        }
      }
    }

    async function deletarQRCode(slug, nome) {
      const cleanSlug = String(slug).toLowerCase().trim();
      const nomeExibicao = (nome && String(nome).trim()) ? String(nome).trim() : cleanSlug;
      if (!confirm(`Tem certeza que deseja excluir o QR Code "${nomeExibicao}" (/${cleanSlug})?\n\nO link deixará de redirecionar imediatamente.`)) {
        return;
      }
      deletedSlugsCache.add(cleanSlug);
      todosQRs = todosQRs.filter(q => String(q.slug).toLowerCase().trim() !== cleanSlug);
      filtrarQRs();
      fecharModal();

      try {
        const res = await fetch(`/api/qrcodes/${encodeURIComponent(cleanSlug)}?_t=${Date.now()}`, {
          method: 'DELETE',
          headers: { 'Accept': 'application/json' }
        });
        if (res.ok) {
          showToast(`QR Code /${cleanSlug} excluído com sucesso!`, 'fa-solid fa-trash-can text-rose-400');
        } else {
          showToast(`QR Code /${cleanSlug} excluído.`, 'fa-solid fa-trash-can text-rose-400');
        }
      } catch (err) {
        showToast(`QR Code /${cleanSlug} excluído.`, 'fa-solid fa-trash-can text-rose-400');
      }
      await carregarDashboard();
    }

    async function limparTudo() {
      if (!confirm("Deseja apagar todos os QR codes e limpar o painel completamente?")) {
        return;
      }
      try {
        const defaultSlugs = ['promo', 'blackfriday', 'insta', 'jogos205'];
        const currentSlugs = todosQRs.map(q => String(q.slug).toLowerCase().trim());
        const allSlugs = Array.from(new Set([...defaultSlugs, ...currentSlugs]));
        allSlugs.forEach(s => deletedSlugsCache.add(s));
        localStorage.removeItem('custom_qrcodes');
        localStorage.removeItem('deleted_qr_slugs');
        localStorage.removeItem('cached_scans');
        localStorage.removeItem('qr_clicks');
        lastRenderedScansJson = '';
        todosQRs = [];
        filtrarQRs();

        // Limpa no servidor via endpoint unificado e por slug
        await fetch('/api/qrcodes/clear-all', { method: 'POST' }).catch(() => {});
        await Promise.allSettled(
          allSlugs.map(s => fetch(`/api/qrcodes/${encodeURIComponent(s)}`, { method: 'DELETE' }))
        );

        showToast('Painel limpo com sucesso!', 'fa-trash-can text-rose-400');
        await carregarDashboard();
      } catch (err) {
        console.error("Erro ao limpar painel:", err);
      }
    }

    function copiarLink(url) {
      navigator.clipboard.writeText(url).then(() => {
        showToast("Link curto copiado com sucesso!");
      }).catch(() => {
        prompt("Copie o link abaixo:", url);
      });
    }

    // Inicialização do tema de fundo do painel
    aplicarFundoDashboard(localStorage.getItem('dashboard_bg_theme') || 'default');

    // Inicialização e Polling de Scans a cada 2.5s
    carregarDashboard();
    setInterval(carregarDashboard, 2500);
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard_view():
    return HTMLResponse(DASHBOARD_HTML, headers=CACHE_BUST_HEADERS)


ACTIVATION_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Ativar QR Code /{slug}</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    body {{ font-family: 'Plus Jakarta Sans', sans-serif; }}
    @media (max-width: 640px) {{
      input {{ font-size: 16px !important; }}
    }}
  </style>
</head>
<body class="bg-gradient-to-br from-zinc-950 via-zinc-900 to-black text-white min-h-screen flex items-center justify-center p-4">
  <div class="max-w-md w-full bg-zinc-900/90 border border-zinc-800 backdrop-blur-2xl p-6 sm:p-8 rounded-3xl shadow-2xl relative overflow-hidden">
    <div class="absolute -top-24 -right-24 w-48 h-48 bg-emerald-500/15 rounded-full blur-3xl pointer-events-none"></div>
    <div class="absolute -bottom-24 -left-24 w-48 h-48 bg-blue-500/15 rounded-full blur-3xl pointer-events-none"></div>

    <div class="relative z-10 text-center">
      <div class="w-14 h-14 bg-gradient-to-br from-emerald-400 to-teal-500 text-zinc-950 rounded-2xl flex items-center justify-center mx-auto mb-4 text-2xl shadow-lg shadow-emerald-500/20 font-bold">
        ⚡
      </div>
      <span class="inline-flex items-center gap-1.5 px-3 py-1 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-full text-[10px] font-bold tracking-wider uppercase mb-2">
        Código Pronto para Ativação
      </span>
      <h1 class="text-xl sm:text-2xl font-extrabold tracking-tight mb-2">Configure o Destino</h1>
      <p class="text-xs text-zinc-400 leading-relaxed mb-6">
        Este código impresso ainda não possui um link associado. Digite o endereço final para ativá-lo agora mesmo.
      </p>

      <form id="form-ativar" action="/api/qrcodes/{slug}/activate" method="POST" class="text-left space-y-3.5">
        <div>
          <label class="block text-xs font-semibold text-zinc-300 mb-1">Link de Destino Final</label>
          <div class="relative">
            <input 
              type="text" 
              name="target_url" 
              id="input-target-url"
              placeholder="https://instagram.com/sua_loja ou seu site" 
              required
              class="w-full px-3.5 py-3 bg-zinc-800/80 border border-zinc-700 rounded-xl text-xs text-white placeholder-zinc-500 focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500 transition font-mono"
            />
          </div>
          <p class="text-[10px] text-zinc-500 mt-1">Exemplo: Instagram, WhatsApp, Cardápio ou Site da empresa.</p>
        </div>

        <div>
          <label class="block text-xs font-semibold text-zinc-300 mb-1">Identificação / Nome (Opcional)</label>
          <input 
            type="text" 
            name="name" 
            id="input-name"
            value="{qr_name}"
            placeholder="Ex: Cardápio Mesa 01" 
            class="w-full px-3.5 py-2.5 bg-zinc-800/80 border border-zinc-700 rounded-xl text-xs text-white placeholder-zinc-500 focus:outline-none focus:border-emerald-500 transition"
          />
        </div>

        <div id="erro-msg" class="hidden p-3 bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs rounded-xl font-medium leading-relaxed"></div>

        <button 
          type="submit" 
          id="btn-ativar"
          class="w-full mt-2 py-3 bg-gradient-to-r from-emerald-500 to-teal-400 hover:from-emerald-400 hover:to-teal-300 text-zinc-950 font-bold rounded-xl text-xs sm:text-sm transition duration-150 shadow-lg shadow-emerald-500/20 flex items-center justify-center gap-2 cursor-pointer active:scale-98"
        >
          <span>Salvar & Ativar QR Code</span>
          <i class="fa-solid fa-arrow-right text-xs"></i>
        </button>
      </form>

      <div class="mt-6 pt-4 border-t border-zinc-800 flex items-center justify-between text-[11px] text-zinc-500">
        <span>Código: <strong class="text-zinc-300 font-mono">/{slug}</strong></span>
        <span class="text-amber-400 font-semibold flex items-center gap-1">
          <i class="fa-solid fa-circle text-[6px]"></i> Aguardando 1º Scan
        </span>
      </div>
    </div>
  </div>

  <script>
    document.getElementById('form-ativar').addEventListener('submit', async function(e) {{
      e.preventDefault();
      const btn = document.getElementById('btn-ativar');
      const errBox = document.getElementById('erro-msg');
      errBox.classList.add('hidden');
      
      const targetUrl = document.getElementById('input-target-url').value.trim();
      const friendlyName = document.getElementById('input-name').value.trim();
      
      if (!targetUrl) {{
        errBox.innerText = 'Por favor, digite o link de destino.';
        errBox.classList.remove('hidden');
        return;
      }}
      
      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-2"></i><span>Ativando QR Code...</span>';
      
      try {{
        const resp = await fetch('/api/qrcodes/{slug}/activate', {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            'Accept': 'application/json'
          }},
          body: JSON.stringify({{ target_url: targetUrl, name: friendlyName }})
        }});
        
        const data = await resp.json().catch(() => ({{}}));
        if (resp.ok) {{
          btn.classList.remove('from-emerald-500', 'to-teal-400', 'hover:from-emerald-400');
          btn.classList.add('bg-emerald-500');
          btn.innerHTML = '<span>✓ Ativado com Sucesso! Redirecionando...</span>';
          setTimeout(() => {{
            window.location.href = data.target_url || targetUrl;
          }}, 800);
        }} else {{
          btn.disabled = false;
          btn.innerHTML = '<span>Salvar & Ativar QR Code</span><i class="fa-solid fa-arrow-right text-xs ml-2"></i>';
          errBox.innerText = data.detail || 'Ocorreu um erro ao salvar o link. Verifique se o endereço é válido.';
          errBox.classList.remove('hidden');
        }}
      }} catch (err) {{
        // Fallback para submit de formulário clássico
        e.target.submit();
      }}
    }});
  </script>
</body>
</html>"""


PAUSED_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QR Code Indisponível</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>body {{ font-family: 'Plus Jakarta Sans', sans-serif; }}</style>
</head>
<body class="bg-zinc-950 text-white min-h-screen flex items-center justify-center p-4">
  <div class="max-w-md w-full bg-zinc-900 border border-zinc-800 p-8 rounded-3xl shadow-2xl text-center">
    <div class="w-14 h-14 bg-amber-500/10 text-amber-400 rounded-2xl flex items-center justify-center mx-auto mb-4 text-2xl border border-amber-500/20">
      <i class="fa-solid fa-triangle-exclamation"></i>
    </div>
    <span class="inline-block px-3 py-1 bg-amber-500/10 text-amber-400 border border-amber-500/20 rounded-full text-[10px] font-bold tracking-wider uppercase mb-2">
      Acesso Pausado
    </span>
    <h1 class="text-lg font-bold mb-2 text-zinc-100">QR Code Temporariamente Indisponível</h1>
    <p class="text-xs text-zinc-400 mb-6 leading-relaxed">
      Este link dinâmico foi temporariamente desativado pelo administrador ou encerrou seu período de vigência.
    </p>
    <div class="p-2.5 bg-zinc-800/60 rounded-xl border border-zinc-800 text-[11px] text-zinc-500 font-mono">
      Identificador: /{slug}
    </div>
  </div>
</body>
</html>"""


# =====================================================================
# ENDPOINTS REST DA API (CRUD COMPLETO COM SUPORTE MULTI-HOST)
# =====================================================================
@app.get("/api/health")
async def health_check():
    redis_status = "offline"
    if redis_async:
        try:
            pong = await redis_async.ping()
            if pong:
                redis_status = "connected"
        except Exception as exc:
            redis_status = f"error: {exc}"
    return {
        "status": "healthy",
        "redis": redis_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_active_qrs": len([k for k in IN_MEMORY_DB.keys() if k not in DELETED_SLUGS]),
    }


@app.get("/api/qrcodes")
async def list_qrcodes_api(
    request: Request,
    search: Optional[str] = None,
    status: Optional[str] = None,
    host_override: Optional[str] = None,
):
    if redis_async:
        try:
            stored_deletions = await redis_async.smembers("deleted_qr_slugs")
            if stored_deletions:
                DELETED_SLUGS.update(stored_deletions)
            stored_slugs = await redis_async.smembers("all_qr_slugs")
            for s in stored_slugs:
                if s in DELETED_SLUGS or s == "rede-03-85c4":
                    IN_MEMORY_DB.pop(s, None)
                    await redis_async.srem("all_qr_slugs", s)
                    await redis_async.delete(f"qr_meta:{s}")
                    await redis_async.hdel("qr_clicks", s)
                    await redis_async.sadd("deleted_qr_slugs", s)
                    continue
                meta = await redis_async.get(f"qr_meta:{s}")
                if meta:
                    IN_MEMORY_DB[s] = json.loads(meta)
        except Exception as exc:
            log.warning("Falha ao sincronizar lista com Redis: %r", exc)

    # Garante purga imediata de quaisquer slugs excluídos
    for d in list(DELETED_SLUGS):
        IN_MEMORY_DB.pop(d, None)

    clicks_map = dict(SCAN_COUNTS)
    if redis_async:
        try:
            stored_clicks = await redis_async.hgetall("qr_clicks")
            if stored_clicks:
                clicks_map.update({k: int(v) for k, v in stored_clicks.items()})
        except Exception:
            pass

    active_host = host_override or request.headers.get("host", "localhost")
    result = []
    for slug, payload in IN_MEMORY_DB.items():
        clean_slug = slug.strip().lstrip("/").lower()
        if clean_slug in DELETED_SLUGS:
            continue
        if status and status != "all" and payload["status"] != status:
            continue
        if search:
            s = search.lower()
            if s not in slug.lower() and s not in payload.get("name", "").lower() and s not in payload["default_url"].lower():
                continue
        result.append(
            {
                "slug": slug,
                "name": payload.get("name", slug.upper()),
                "short_url": f"http://{active_host}/{slug}",
                "default_url": payload["default_url"],
                "status": payload["status"],
                "v": payload.get("v", 1),
                "clicks": clicks_map.get(clean_slug, 0),
                "design": payload.get("design", {}),
                "rules": payload.get("rules", []),
                "created_at": payload.get("created_at"),
            }
        )
    return JSONResponse(result, headers=CACHE_BUST_HEADERS)


@app.get("/api/qrcodes/{slug}")
async def get_qrcode_api(slug: str, request: Request, host_override: Optional[str] = None):
    clean_slug = slug.strip().lstrip("/").lower()
    if clean_slug in DELETED_SLUGS:
        raise HTTPException(status_code=404, detail="QR Code não encontrado")
    if REDIS_URL:
        try:
            if await redis_async.sismember("deleted_qr_slugs", clean_slug):
                DELETED_SLUGS.add(clean_slug)
                raise HTTPException(status_code=404, detail="QR Code não encontrado")
            meta = await redis_async.get(f"qr_meta:{clean_slug}")
            if meta:
                IN_MEMORY_DB[clean_slug] = json.loads(meta)
        except HTTPException:
            raise
        except Exception:
            pass

    if clean_slug not in IN_MEMORY_DB:
        raise HTTPException(status_code=404, detail="QR Code não encontrado")
    payload = IN_MEMORY_DB[clean_slug]
    active_host = host_override or request.headers.get("host", "localhost")
    return {
        "slug": clean_slug,
        "name": payload.get("name", clean_slug.upper()),
        "short_url": f"http://{active_host}/{clean_slug}",
        "default_url": payload["default_url"],
        "status": payload["status"],
        "design": payload.get("design", {}),
        "rules": payload.get("rules", []),
        "v": payload.get("v", 1),
        "clicks": SCAN_COUNTS.get(clean_slug, 0),
    }


@app.post("/api/qrcodes")
async def create_qr_api(body: QRCreateDTO, request: Request):
    clean_slug = body.slug.strip().lstrip("/").lower()
    if clean_slug == "rede-03-85c4":
        raise HTTPException(status_code=400, detail="Este identificador foi excluído e não pode ser recriado.")
    DELETED_SLUGS.discard(clean_slug)
    if REDIS_URL:
        try:
            await redis_async.srem("deleted_qr_slugs", clean_slug)
        except Exception:
            pass

    if clean_slug in IN_MEMORY_DB:
        raise HTTPException(status_code=409, detail="Slug já está em uso. Escolha outro identificador.")
    if REDIS_URL:
        try:
            if await redis_async.sismember("all_qr_slugs", clean_slug) or await redis_async.exists(f"qr_meta:{clean_slug}"):
                raise HTTPException(status_code=409, detail="Slug já está em uso. Escolha outro identificador.")
        except HTTPException:
            raise
        except Exception:
            pass

    raw_target = (body.target_url or "").strip()
    target = re.sub(r"[\r\n\t]", "", raw_target)
    if target:
        if target.lower().startswith(("javascript:", "data:", "vbscript:")):
            raise HTTPException(status_code=422, detail="URL inválida. Protocolo proibido.")
        if not target.startswith(("http://", "https://")):
            target = f"https://{target}"

        # Previne loop infinito (SSRF/Loop Denial-of-Service)
        parts = urlsplit(target)
        target_host = (parts.hostname or "").lower()
        target_path = parts.path.strip("/").lower()
        if target_host in ("localhost", "127.0.0.1", "0.0.0.0", LAN_IP.lower()) and target_path == clean_slug:
            raise HTTPException(status_code=422, detail="Destino não pode apontar para o próprio QR Code (loop infinito detectado).")
    else:
        target = ""
        if body.status == "active":
            body.status = "unclaimed"

    # Auto-ajuste de contraste para garantir leitura por qualquer câmera
    fg = body.foreground
    bg = body.background or "#FFFFFF"
    try:
        validate(QRDesign(foreground=fg, background=bg))
    except InvalidDesign:
        fg = "#0F172A"
        bg = "#FFFFFF"

    payload = {
        "v": 1,
        "name": re.sub(r"[\r\n\t]", " ", body.name.strip()),
        "qr_id": "99999999-9999-9999-9999-999999999999",
        "workspace_id": "00000000-0000-0000-0000-000000000003",
        "status": body.status,
        "default_url": target,
        "fallback_url": None,
        "expires_at": None,
        "utm": {"utm_source": "qrcode"},
        "design": {
            "foreground": fg,
            "background": bg,
            "background_type": body.background_type or "solid",
            "background_color_end": body.background_color_end,
            "module_style": body.module_style,
            "finder_style": "rounded" if body.module_style == "rounded" else "square",
            "frame_text": body.frame_text,
        },
        "rules": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    IN_MEMORY_DB[clean_slug] = payload

    # Sincroniza em Redis sob todos os hosts conhecidos + host atual
    current_host = request.headers.get("host", "").split(":")[0].lower()
    hosts_to_sync = set(KNOWN_HOSTS + [current_host])
    payload_str = json.dumps(payload)
    for h in hosts_to_sync:
        if h:
            resolver.invalidate(f"qr:{h}:{clean_slug}")
    try:
        if redis_async:
            for h in hosts_to_sync:
                if h:
                    await redis_async.set(f"qr:{h}:{clean_slug}", payload_str, ex=86400)
            await redis_async.sadd("all_qr_slugs", clean_slug)
            await redis_async.set(f"qr_meta:{clean_slug}", payload_str)
    except Exception as exc:
        log.warning("Sincronização Redis offline ou ignorada: %r", exc)

    return {"status": "created", "slug": clean_slug}


@app.put("/api/qrcodes/{slug}")
async def update_qr_api(slug: str, body: QRUpdateDTO, request: Request):
    clean_slug = slug.strip().lstrip("/").lower()
    if clean_slug in DELETED_SLUGS:
        raise HTTPException(status_code=404, detail="QR Code não encontrado")

    if clean_slug not in IN_MEMORY_DB:
        try:
            if redis_async:
                if await redis_async.sismember("deleted_qr_slugs", clean_slug):
                    DELETED_SLUGS.add(clean_slug)
                    raise HTTPException(status_code=404, detail="QR Code não encontrado")
                cached = await redis_async.get(f"qr_meta:{clean_slug}")
                if cached:
                    IN_MEMORY_DB[clean_slug] = json.loads(cached)
        except HTTPException:
            raise
        except Exception:
            pass

    if clean_slug not in IN_MEMORY_DB:
        raise HTTPException(status_code=404, detail="QR Code não encontrado")

    item = IN_MEMORY_DB[clean_slug]

    if body.name is not None:
        item["name"] = re.sub(r"[\r\n\t]", " ", body.name.strip())

    if body.target_url is not None:
        raw_target = body.target_url.strip()
        if raw_target == "":
            item["default_url"] = ""
            if body.status is None:
                item["status"] = "unclaimed"
        else:
            target = re.sub(r"[\r\n\t]", "", raw_target)
            if target.lower().startswith(("javascript:", "data:", "vbscript:")):
                raise HTTPException(status_code=422, detail="URL inválida. Protocolo proibido.")
            if not target.startswith(("http://", "https://")):
                target = f"https://{target}"

            parts = urlsplit(target)
            target_host = (parts.hostname or "").lower()
            target_path = parts.path.strip("/").lower()
            if target_host in ("localhost", "127.0.0.1", "0.0.0.0", LAN_IP.lower()) and target_path == clean_slug:
                raise HTTPException(status_code=422, detail="Destino não pode apontar para o próprio QR Code (loop infinito detectado).")

            item["default_url"] = target

    if body.status is not None:
        item["status"] = body.status

    design = item.get("design", {})
    if body.foreground is not None or body.background is not None:
        fg = body.foreground if body.foreground is not None else design.get("foreground", "#0F172A")
        bg = body.background if body.background is not None else design.get("background", "#FFFFFF")
        try:
            validate(QRDesign(foreground=fg, background=bg))
        except InvalidDesign:
            if body.foreground is not None:
                fg = "#0F172A"
            if body.background is not None:
                bg = "#FFFFFF"
        design["foreground"] = fg
        design["background"] = bg

    if body.background_type is not None:
        design["background_type"] = body.background_type
    if body.background_color_end is not None:
        design["background_color_end"] = body.background_color_end

    if body.frame_text is not None:
        design["frame_text"] = body.frame_text

    if body.module_style is not None:
        design["module_style"] = body.module_style
        design["finder_style"] = "rounded" if body.module_style == "rounded" else "square"

    item["design"] = design
    item["v"] = item.get("v", 1) + 1

    current_host = request.headers.get("host", "").split(":")[0].lower()
    hosts_to_sync = set(KNOWN_HOSTS + [current_host])
    payload_str = json.dumps(item)
    for h in hosts_to_sync:
        if h:
            resolver.invalidate(f"qr:{h}:{clean_slug}")
    try:
        if redis_async:
            for h in hosts_to_sync:
                if h:
                    await redis_async.set(f"qr:{h}:{clean_slug}", payload_str, ex=86400)
            await redis_async.set(f"qr_meta:{clean_slug}", payload_str)
    except Exception as exc:
        log.warning("Sincronização Redis offline ou ignorada: %r", exc)

    return {"status": "updated", "slug": clean_slug, "data": item}


@app.delete("/api/qrcodes/{slug}")
async def delete_qr_api(slug: str, request: Request):
    clean_slug = slug.strip().lstrip("/").lower()
    # Registra tombstone imediatamente e remove do estado em memória
    DELETED_SLUGS.add(clean_slug)
    IN_MEMORY_DB.pop(clean_slug, None)
    SCAN_COUNTS.pop(clean_slug, None)
    save_local_db()

    current_host = request.headers.get("host", "").split(":")[0].lower()
    hosts_to_sync = set(KNOWN_HOSTS + [current_host])
    for h in hosts_to_sync:
        if h:
            resolver.invalidate(f"qr:{h}:{clean_slug}")
    try:
        if redis_async:
            await redis_async.sadd("deleted_qr_slugs", clean_slug)
            await redis_async.srem("all_qr_slugs", clean_slug)
            await redis_async.delete(f"qr_meta:{clean_slug}")
            await redis_async.hdel("qr_clicks", clean_slug)
            for h in hosts_to_sync:
                if h:
                    await redis_async.delete(f"qr:{h}:{clean_slug}")
            try:
                keys = await redis_async.keys(f"qr:*:{clean_slug}")
                if keys:
                    await redis_async.delete(*keys)
            except Exception:
                pass
    except Exception as exc:
        log.warning("Sincronização Redis offline ou ignorada durante exclusão: %r", exc)

    if redis_sync:
        try:
            redis_sync.sadd("deleted_qr_slugs", clean_slug)
            redis_sync.srem("all_qr_slugs", clean_slug)
            redis_sync.delete(f"qr_meta:{clean_slug}")
            redis_sync.hdel("qr_clicks", clean_slug)
            for h in hosts_to_sync:
                if h:
                    redis_sync.delete(f"qr:{h}:{clean_slug}")
        except Exception:
            pass

    # Operação 100% idempotente (RFC 7231 / 9110)
    return JSONResponse({"status": "deleted", "slug": clean_slug}, headers=CACHE_BUST_HEADERS)


@app.post("/api/qrcodes/clear-all")
async def clear_all_qrcodes_api(request: Request):
    current_host = request.headers.get("host", "").split(":")[0].lower()
    hosts_to_sync = set(KNOWN_HOSTS + [current_host])
    all_slugs = set(list(IN_MEMORY_DB.keys()))
    if redis_async:
        try:
            stored = await redis_async.smembers("all_qr_slugs")
            if stored:
                all_slugs.update(stored)
        except Exception:
            pass

    for s in list(all_slugs):
        clean = s.strip().lstrip("/").lower()
        DELETED_SLUGS.add(clean)
        IN_MEMORY_DB.pop(clean, None)
        SCAN_COUNTS.pop(clean, None)
        for h in hosts_to_sync:
            if h:
                resolver.invalidate(f"qr:{h}:{clean}")

    if redis_async:
        try:
            for clean in all_slugs:
                await redis_async.sadd("deleted_qr_slugs", clean)
                await redis_async.srem("all_qr_slugs", clean)
                await redis_async.delete(f"qr_meta:{clean}")
                for h in hosts_to_sync:
                    if h:
                        await redis_async.delete(f"qr:{h}:{clean}")
        except Exception as exc:
            log.warning("Falha ao limpar tudo no Redis: %r", exc)

    save_local_db()
    return {"status": "cleared", "total": len(all_slugs)}


@app.post("/api/qrcodes/batch")
async def create_batch_qr_api(body: QRBatchCreateDTO, request: Request):
    created = []
    current_host = request.headers.get("host", "").split(":")[0].lower()
    proto = "https" if IS_SERVERLESS or "vercel.app" in current_host else "http"

    fg = body.foreground
    bg = body.background or "#FFFFFF"
    try:
        validate(QRDesign(foreground=fg, background=bg))
    except InvalidDesign:
        fg = "#0F172A"
        bg = "#FFFFFF"

    clean_prefix = re.sub(r"[^a-z0-9_-]", "", body.prefix.strip().lower())
    if not clean_prefix:
        clean_prefix = "qr-"

    for i in range(1, body.count + 1):
        rand_suffix = secrets.token_hex(2)
        slug = f"{clean_prefix}{i:02d}-{rand_suffix}" if body.count > 1 else f"{clean_prefix}{rand_suffix}"
        slug = slug[:50]
        while slug in IN_MEMORY_DB or slug in DELETED_SLUGS:
            slug = f"{clean_prefix}{i:02d}-{secrets.token_hex(3)}"[:50]

        DELETED_SLUGS.discard(slug)
        payload = {
            "v": 1,
            "name": f"{body.name_prefix.strip()} #{i:02d}",
            "qr_id": str(uuid.uuid4()),
            "workspace_id": "00000000-0000-0000-0000-000000000003",
            "status": "unclaimed",
            "default_url": "",
            "fallback_url": None,
            "expires_at": None,
            "utm": {"utm_source": "qrcode", "utm_campaign": "batch"},
            "design": {
                "foreground": fg,
                "background": bg,
                "background_type": body.background_type or "solid",
                "background_color_end": body.background_color_end,
                "module_style": body.module_style,
                "finder_style": "rounded" if body.module_style == "rounded" else "square",
                "frame_text": body.frame_text,
            },
            "rules": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        IN_MEMORY_DB[slug] = payload
        sync_redis_slug(slug, payload)
        payload_str = json.dumps(payload)
        hosts_to_sync = set(KNOWN_HOSTS + [current_host])
        for h in hosts_to_sync:
            if h:
                resolver.invalidate(f"qr:{h}:{slug}")
        if redis_async:
            try:
                for h in hosts_to_sync:
                    if h:
                        await redis_async.set(f"qr:{h}:{slug}", payload_str, ex=86400)
                await redis_async.sadd("all_qr_slugs", slug)
                await redis_async.set(f"qr_meta:{slug}", payload_str)
            except Exception as exc:
                log.warning("Falha ao sincronizar lote no Redis async: %r", exc)
        created.append({
            "slug": slug,
            "name": payload["name"],
            "short_url": f"{proto}://{current_host or 'localhost'}/{slug}",
            "status": "unclaimed",
        })

    save_local_db()
    return {"status": "created", "total": len(created), "items": created}


@app.post("/api/qrcodes/{slug}/activate")
async def activate_qr_api(slug: str, request: Request):
    clean_slug = slug.strip().lstrip("/").lower()
    content_type = request.headers.get("content-type", "")
    target_url = ""
    friendly_name = None

    if "application/json" in content_type:
        try:
            body = await request.json()
            target_url = body.get("target_url") or body.get("url") or ""
            friendly_name = body.get("name")
        except Exception:
            pass
    else:
        try:
            raw_body = await request.body()
            from urllib.parse import parse_qs
            parsed = parse_qs(raw_body.decode("utf-8", errors="replace"))
            target_url = (parsed.get("target_url") or parsed.get("url") or [""])[0]
            friendly_name = (parsed.get("name") or [None])[0]
        except Exception:
            pass

    target = re.sub(r"[\r\n\t]", "", str(target_url).strip())
    if not target:
        raise HTTPException(status_code=422, detail="Por favor, informe uma URL de destino válida.")

    if target.lower().startswith(("javascript:", "data:", "vbscript:")):
        raise HTTPException(status_code=422, detail="Protocolo inválido.")

    if not target.startswith(("http://", "https://")):
        target = f"https://{target}"

    parts = urlsplit(target)
    target_host = (parts.hostname or "").lower()
    target_path = parts.path.strip("/").lower()
    if target_host in ("localhost", "127.0.0.1", "0.0.0.0", LAN_IP.lower()) and target_path == clean_slug:
        raise HTTPException(status_code=422, detail="Destino não pode apontar para o próprio código.")

    if clean_slug not in IN_MEMORY_DB:
        try:
            if redis_async:
                meta = await redis_async.get(f"qr_meta:{clean_slug}")
                if meta:
                    IN_MEMORY_DB[clean_slug] = json.loads(meta)
        except Exception:
            pass

    if clean_slug not in IN_MEMORY_DB:
        raise HTTPException(status_code=404, detail="QR Code não encontrado.")

    item = IN_MEMORY_DB[clean_slug]
    item["default_url"] = target
    item["status"] = "active"
    item["v"] = item.get("v", 1) + 1
    if friendly_name and str(friendly_name).strip():
        item["name"] = re.sub(r"[\r\n\t]", " ", str(friendly_name).strip())

    current_host = request.headers.get("host", "").split(":")[0].lower()
    hosts_to_sync = set(KNOWN_HOSTS + [current_host])
    payload_str = json.dumps(item)
    for h in hosts_to_sync:
        if h:
            resolver.invalidate(f"qr:{h}:{clean_slug}")
    if redis_async:
        try:
            for h in hosts_to_sync:
                if h:
                    await redis_async.set(f"qr:{h}:{clean_slug}", payload_str, ex=86400)
            await redis_async.sadd("all_qr_slugs", clean_slug)
            await redis_async.set(f"qr_meta:{clean_slug}", payload_str)
        except Exception as exc:
            log.warning("Falha ao salvar no Redis durante ativação: %r", exc)
    save_local_db()

    accept = request.headers.get("accept", "")
    if "text/html" in accept or "application/x-www-form-urlencoded" in content_type:
        success_html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QR Code Ativado com Sucesso!</title>
  <meta http-equiv="refresh" content="2;url={html.escape(target)}">
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-zinc-900 flex items-center justify-center min-h-screen p-4 text-white">
  <div class="max-w-md w-full bg-zinc-800 border border-emerald-500/40 p-8 rounded-3xl shadow-2xl text-center">
    <div class="w-16 h-16 bg-emerald-500/20 text-emerald-400 rounded-2xl flex items-center justify-center mx-auto mb-4 text-3xl font-bold">
      ✓
    </div>
    <h1 class="text-xl font-bold mb-2">QR Code Ativado com Sucesso!</h1>
    <p class="text-xs text-zinc-400 mb-5 leading-relaxed">O destino foi vinculado ao seu código impresso.</p>
    <div class="bg-zinc-900/80 p-3 rounded-xl border border-zinc-700 text-xs text-emerald-400 truncate font-mono mb-6">
      {html.escape(target)}
    </div>
    <a href="{html.escape(target)}" class="block w-full py-3 bg-emerald-500 hover:bg-emerald-400 text-zinc-950 font-bold rounded-xl text-xs transition shadow-lg shadow-emerald-500/20">
      Acessar Link Agora &rarr;
    </a>
    <p class="text-[10px] text-zinc-500 mt-4">Redirecionando em 2 segundos...</p>
  </div>
</body>
</html>"""
        return HTMLResponse(success_html, status_code=200, headers=NO_STORE)

    return {"status": "activated", "slug": clean_slug, "target_url": target}


@app.get("/api/export-batch/pdf-sheet")
async def export_batch_pdf_sheet(request: Request, prefix: Optional[str] = None, status: Optional[str] = None):
    slugs = [s for s in list(IN_MEMORY_DB.keys()) if s not in DELETED_SLUGS]
    if prefix:
        slugs = [s for s in slugs if s.startswith(prefix.strip().lower())]
    if status and status != "all":
        slugs = [s for s in slugs if IN_MEMORY_DB.get(s, {}).get("status") == status]

    if not slugs:
        raise HTTPException(status_code=404, detail="Nenhum QR Code encontrado para gerar folha de impressão.")

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
    except ImportError:
        raise HTTPException(status_code=500, detail="ReportLab não instalado.")

    active_host = request.headers.get("host", "localhost")
    proto = "https" if IS_SERVERLESS or "vercel.app" in active_host else "http"

    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    page_w, page_h = A4

    cols, rows = 3, 4
    margin_x = 12 * mm
    margin_y = 15 * mm
    usable_w = page_w - (2 * margin_x)
    usable_h = page_h - (2 * margin_y)
    col_w = usable_w / cols
    row_h = usable_h / rows
    qr_size = min(col_w - 6 * mm, row_h - 16 * mm)

    for idx, s in enumerate(slugs):
        page_idx = idx % (cols * rows)
        if idx > 0 and page_idx == 0:
            c.showPage()

        col_i = page_idx % cols
        row_i = page_idx // cols

        cell_x = margin_x + col_i * col_w
        cell_y = page_h - margin_y - (row_i + 1) * row_h

        # Linha pontilhada para corte
        c.setStrokeColorRGB(0.85, 0.85, 0.88)
        c.setLineWidth(0.5)
        c.setDash(2, 2)
        c.roundRect(cell_x + 1.5 * mm, cell_y + 1.5 * mm, col_w - 3 * mm, row_h - 3 * mm, 3 * mm, stroke=1, fill=0)
        c.setDash()

        # Renderiza imagem QR Code
        qr_target = f"{proto}://{active_host}/{s}"
        payload = IN_MEMORY_DB.get(s, {})
        d = payload.get("design", {})
        design = QRDesign(
            foreground=d.get("foreground", "#0F172A"),
            background="#FFFFFF",
            module_style=d.get("module_style", "rounded"),
            finder_style="rounded" if d.get("module_style", "rounded") == "rounded" else "square",
            frame_text=d.get("frame_text", "ESCANEIE AQUI"),
        )
        png_bytes = render(qr_target, design=design, fmt="png", size_px=512)
        qr_img = ImageReader(io.BytesIO(png_bytes))

        qr_x = cell_x + (col_w - qr_size) / 2
        qr_y = cell_y + 9 * mm
        c.drawImage(qr_img, qr_x, qr_y, qr_size, qr_size)

        c.setFillColorRGB(0.1, 0.1, 0.15)
        c.setFont("Helvetica-Bold", 8)
        name = payload.get("name") or s
        c.drawCentredString(cell_x + col_w / 2, cell_y + 5.5 * mm, str(name)[:25])

        c.setFillColorRGB(0.4, 0.4, 0.45)
        c.setFont("Helvetica", 7)
        c.drawCentredString(cell_x + col_w / 2, cell_y + 2.5 * mm, f"/{s}")

    c.showPage()
    c.save()

    return Response(
        content=out.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=folha_qrcodes_impressao.pdf"},
    )


@app.get("/api/export-batch/csv")
async def export_batch_csv(request: Request):
    active_host = request.headers.get("host", "localhost")
    proto = "https" if IS_SERVERLESS or "vercel.app" in active_host else "http"
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Slug", "Nome", "Status", "Link Curto Impresso", "URL Destino Atual", "Data Criacao"])
    for s, p in IN_MEMORY_DB.items():
        if s not in DELETED_SLUGS:
            writer.writerow([
                s,
                p.get("name", s),
                p.get("status", "active"),
                f"{proto}://{active_host}/{s}",
                p.get("default_url", ""),
                p.get("created_at", ""),
            ])
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=relatorio_qrcodes.csv"},
    )


def apply_svg_gradient(svg_bytes: bytes, bg_start: str, bg_end: str, gradient_id: str = "qr-bg-grad") -> bytes:
    try:
        svg_str = svg_bytes.decode("utf-8")
        gradient_def = (
            f'<defs>'
            f'<linearGradient id="{gradient_id}" x1="0%" y1="0%" x2="100%" y2="100%">'
            f'<stop offset="0%" stop-color="{bg_start}"/>'
            f'<stop offset="100%" stop-color="{bg_end}"/>'
            f'</linearGradient>'
            f'</defs>'
        )
        svg_str = re.sub(r"(<svg[^>]*>)", r"\1" + gradient_def, svg_str, count=1)
        # Substitui a primeira ocorrência do fill com a cor de fundo pelo gradiente linear
        svg_str = re.sub(
            rf'fill="{re.escape(bg_start)}"',
            f'fill="url(#{gradient_id})"',
            svg_str,
            count=1,
            flags=re.IGNORECASE,
        )
        return svg_str.encode("utf-8")
    except Exception:
        return svg_bytes


# Preview Dinâmico do Modal (Live Preview enquanto digita)
@app.get("/api/preview-live")
def live_preview(
    slug: str = "preview",
    url: Optional[str] = None,
    color: str = "#0F172A",
    bg_color: str = "#FFFFFF",
    bg_color_end: Optional[str] = None,
    bg_type: str = "solid",
    frame: str = "ESCANEIE AQUI",
    style: str = "rounded",
    host: str = "localhost",
):
    try:
        validate(QRDesign(foreground=color, background=bg_color))
    except InvalidDesign:
        color = "#0F172A"
        bg_color = "#FFFFFF"

    design = QRDesign(
        foreground=color,
        background=bg_color,
        module_style=style,
        finder_style="rounded" if style == "rounded" else "square",
        frame_text=frame,
    )
    target_data = url.strip() if url and url.strip() else f"http://{host}/{slug}"
    svg_bytes = render(target_data, design=design, fmt="svg", size_px=320)
    if bg_type == "gradient" and bg_color_end:
        svg_bytes = apply_svg_gradient(svg_bytes, bg_color, bg_color_end)
    return Response(content=svg_bytes, media_type="image/svg+xml")


@app.get("/preview/{slug}.svg")
async def preview_svg(
    slug: str,
    request: Request,
    host_override: Optional[str] = None,
    color: Optional[str] = None,
    bg_color: Optional[str] = None,
    bg_color_end: Optional[str] = None,
    bg_type: Optional[str] = None,
    frame: Optional[str] = None,
    style: Optional[str] = None,
):
    clean_slug = slug.strip().lstrip("/").lower()
    active_host = host_override or request.headers.get("host", "localhost")

    if REDIS_URL:
        try:
            meta = await redis_async.get(f"qr_meta:{clean_slug}")
            if meta:
                IN_MEMORY_DB[clean_slug] = json.loads(meta)
        except Exception:
            pass

    payload = IN_MEMORY_DB.get(clean_slug)
    if payload:
        design_data = payload.get("design", {})
        fg_col = design_data.get("foreground", "#0F172A")
        bg_col = design_data.get("background", "#FFFFFF")
        bg_t = design_data.get("background_type", "solid")
        bg_end = design_data.get("background_color_end")
        m_style = design_data.get("module_style", "rounded")
        f_style = design_data.get("finder_style", "rounded" if m_style == "rounded" else "square")
        f_text = design_data.get("frame_text", "ESCANEIE AQUI")
    else:
        # Fallback apenas para slugs não encontrados no banco
        fg_col = color or "#0F172A"
        bg_col = bg_color or "#FFFFFF"
        bg_t = bg_type or ("gradient" if bg_color_end else "solid")
        bg_end = bg_color_end
        m_style = style or "rounded"
        f_style = "rounded" if m_style == "rounded" else "square"
        f_text = frame or "ESCANEIE AQUI"

    try:
        validate(QRDesign(foreground=fg_col, background=bg_col))
    except InvalidDesign:
        fg_col = "#0F172A"
        bg_col = "#FFFFFF"

    design = QRDesign(
        foreground=fg_col,
        background=bg_col,
        module_style=m_style,
        finder_style=f_style,
        frame_text=f_text,
    )

    proto = "https" if IS_SERVERLESS or "vercel.app" in active_host else "http"
    target_data = f"{proto}://{active_host}/{clean_slug}"
    try:
        svg_bytes = render(target_data, design=design, fmt="svg", size_px=400)
    except Exception:
        design.foreground = "#0F172A"
        design.background = "#FFFFFF"
        svg_bytes = render(target_data, design=design, fmt="svg", size_px=400)

    if bg_t == "gradient" and bg_end:
        svg_bytes = apply_svg_gradient(svg_bytes, bg_col, bg_end)

    return Response(content=svg_bytes, media_type="image/svg+xml")


@app.get("/scans")
async def list_scans():
    clicks_map = dict(SCAN_COUNTS)
    if REDIS_URL and redis_async:
        try:
            stored_clicks = await redis_async.hgetall("qr_clicks")
            if stored_clicks:
                clicks_map.update({k: int(v) for k, v in stored_clicks.items()})
        except Exception:
            pass
    total_clicks = sum(clicks_map.values())
    return JSONResponse(
        {
            "total_scans": max(total_clicks, len(CAPTURED_SCANS)),
            "clicks_by_slug": clicks_map,
            "recent_events": [
                {
                    "ts": e["ts"].isoformat() if hasattr(e["ts"], "isoformat") else str(e["ts"]),
                    "slug": e["slug"],
                    "destination": e["destination_url"],
                    "device": e["device_type"],
                    "os": e["os"],
                    "browser": e["browser"],
                    "visitor_hash": e["visitor_hash"],
                    "is_bot": e["is_bot"],
                }
                for e in reversed(CAPTURED_SCANS[-50:])
            ],
        },
        headers=CACHE_BUST_HEADERS,
    )


@app.get("/export/{slug}")
async def export_qr(
    slug: str,
    request: Request,
    format: str = Query("svg", pattern=r"^(svg|png|pdf)$"),
    size: int = 1024,
    host_override: Optional[str] = None,
    color: Optional[str] = None,
    bg_color: Optional[str] = None,
    bg_color_end: Optional[str] = None,
    bg_type: Optional[str] = None,
    frame: Optional[str] = None,
    style: Optional[str] = None,
):
    clean_slug = slug.strip().lstrip("/").lower()
    active_host = host_override or request.headers.get("host", "localhost")

    if REDIS_URL:
        try:
            meta = await redis_async.get(f"qr_meta:{clean_slug}")
            if meta:
                IN_MEMORY_DB[clean_slug] = json.loads(meta)
        except Exception:
            pass

    payload = IN_MEMORY_DB.get(clean_slug)
    if payload:
        design_data = payload.get("design", {})
        fg_col = design_data.get("foreground", "#0F172A")
        bg_col = design_data.get("background", "#FFFFFF")
        bg_t = design_data.get("background_type", "solid")
        bg_end = design_data.get("background_color_end")
        m_style = design_data.get("module_style", "rounded")
        f_style = design_data.get("finder_style", "rounded" if m_style == "rounded" else "square")
        f_text = design_data.get("frame_text", "ESCANEIE AQUI")
    else:
        fg_col = color or "#0F172A"
        bg_col = bg_color or "#FFFFFF"
        bg_t = bg_type or ("gradient" if bg_color_end else "solid")
        bg_end = bg_color_end
        m_style = style or "rounded"
        f_style = "rounded" if m_style == "rounded" else "square"
        f_text = frame or "ESCANEIE AQUI"

    design = QRDesign(
        foreground=fg_col,
        background=bg_col,
        module_style=m_style,
        finder_style=f_style,
        frame_text=f_text,
    )
    proto = "https" if IS_SERVERLESS or "vercel.app" in active_host else "http"
    target_data = f"{proto}://{active_host}/{clean_slug}"
    try:
        content = render(target_data, design=design, fmt=format, size_px=size)
    except InvalidDesign:
        design.foreground = "#0F172A"
        design.background = "#FFFFFF"
        content = render(target_data, design=design, fmt=format, size_px=size)
    except Exception as exc:
        log.error("Erro ao exportar QR code /%s (%s): %r", clean_slug, format, exc)
        raise HTTPException(status_code=500, detail="Erro interno ao gerar arquivo de exportação")

    if format == "svg" and bg_t == "gradient" and bg_end:
        content = apply_svg_gradient(content, bg_col, bg_end)

    media_types = {"svg": "image/svg+xml", "png": "image/png", "pdf": "application/pdf"}
    return Response(
        content=content,
        media_type=media_types[format],
        headers={"Content-Disposition": f'attachment; filename="qr-{clean_slug}.{format}"'},
    )


# =====================================================================
# HOT PATH: MOTOR DE REDIRECIONAMENTO 302 (100% RESILIENTE)
# =====================================================================
SLUG_VALID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")


@app.get("/{slug}")
async def redirect_endpoint(slug: str, request: Request):
    clean_slug = slug.strip().lstrip("/").lower()
    host = request.headers.get("host", "").split(":")[0].lower()
    if not SLUG_VALID_RE.fullmatch(clean_slug) or clean_slug in DELETED_SLUGS:
        payload = None
    else:
        payload = await resolver.resolve(host, clean_slug)
        
        # Fallback garantido para ambiente standalone / serverless
        if payload is None:
            payload = IN_MEMORY_DB.get(clean_slug)
            if payload is None and REDIS_URL:
                try:
                    meta = await redis_async.get(f"qr_meta:{clean_slug}")
                    if meta:
                        payload = json.loads(meta)
                        IN_MEMORY_DB[clean_slug] = payload
                except Exception:
                    pass
            if payload is None:
                for k, v in IN_MEMORY_DB.items():
                    if k.lower() == clean_slug:
                        payload = v
                        break

    headers = request.headers
    if payload is None:
        log.warning("Slug não encontrado: %s (host=%s)", clean_slug, host)
        accept = headers.get("accept", "")
        if "text/html" in accept:
            return HTMLResponse(
                f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QR Code Não Encontrado</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-zinc-50 flex items-center justify-center min-h-screen p-4 text-zinc-800">
  <div class="max-w-md w-full bg-white p-8 rounded-2xl border border-zinc-200 shadow-sm text-center">
    <div class="w-12 h-12 bg-rose-50 text-rose-600 rounded-full flex items-center justify-center mx-auto mb-4 text-xl">🔍</div>
    <h1 class="text-base font-bold text-zinc-900 mb-2">QR Code /{clean_slug} Não Encontrado</h1>
    <p class="text-xs text-zinc-500 mb-6">Este código pode ter sido removido ou o link foi digitado incorretamente.</p>
    <a href="/" class="inline-block px-4 py-2 bg-zinc-900 text-white rounded-lg text-xs font-semibold hover:bg-zinc-800 transition">Acessar Painel</a>
  </div>
</body>
</html>""",
                status_code=404,
                headers=NO_STORE,
            )
        return PlainTextResponse(f"QR Code /{clean_slug} não encontrado", status_code=404, headers=NO_STORE)

    # 1. Fluxo de Primeiro Scan: código virgem (unclaimed) aguardando ativação
    is_unclaimed = (
        payload.get("status") == "unclaimed"
        or not payload.get("default_url")
        or str(payload.get("default_url", "")).strip() == ""
    )
    if is_unclaimed:
        qr_name = html.escape(payload.get("name") or clean_slug)
        return HTMLResponse(
            ACTIVATION_HTML.format(slug=clean_slug, qr_name=qr_name),
            status_code=200,
            headers=NO_STORE,
        )

    decision = decide(payload, headers.get("user-agent", ""), None, datetime.now(timezone.utc))
    if decision.kind == "gone":
        fallback = payload.get("fallback_url")
        if fallback:
            return RedirectResponse(fallback, status_code=302, headers=NO_STORE)
        accept = headers.get("accept", "")
        if "text/html" in accept:
            return HTMLResponse(
                PAUSED_HTML.format(slug=clean_slug),
                status_code=410,
                headers=NO_STORE,
            )
        return PlainTextResponse("QR Code pausado ou expirado", status_code=410, headers=NO_STORE)

    dest = decision.url or payload.get("default_url") or "/"
    trusted_ip_h = "x-real-ip" if IS_SERVERLESS else "cf-connecting-ip"
    country_h = "x-vercel-ip-country" if IS_SERVERLESS else "cf-ipcountry"
    evt = build_event(
        headers,
        request.client.host if request.client else None,
        payload,
        host=host,
        slug=clean_slug,
        destination=dest,
        trusted_ip_header=trusted_ip_h,
        country_header=country_h,
    )
    # Contabiliza cliques por QR Code
    clean_k = clean_slug.lower()
    SCAN_COUNTS[clean_k] = SCAN_COUNTS.get(clean_k, 0) + 1
    if redis_async:
        try:
            await redis_async.hincrby("qr_clicks", clean_k, 1)
        except Exception:
            pass

    if IS_SERVERLESS:
        try:
            enriched = enrich(evt, salt="demo-salt-lgpd")
            data = dict(zip(COLUMNS, enriched))
            CAPTURED_SCANS.append(data)
            if len(CAPTURED_SCANS) > 200:
                del CAPTURED_SCANS[:-100]
            if REDIS_URL:
                try:
                    await redis_async.xadd("scans", evt, maxlen=10000)
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Falha ao registrar scan serverless: %r", exc)
    else:
        pipeline.capture(evt)

    if decision.kind == "deeplink":
        body = DEEPLINK_HTML.format(
            web_attr=dest,
            web_js=json.dumps(dest),
            app_js=json.dumps(decision.app_url),
        )
        return HTMLResponse(body, headers=NO_STORE)

    return RedirectResponse(
        dest,
        status_code=302,
        headers={**NO_STORE, "Referrer-Policy": "no-referrer-when-downgrade"},
    )


def find_available_port(start_port: int = 8000, max_attempts: int = 50) -> int:
    import socket
    for p in range(start_port, start_port + max_attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", p))
                return p
        except OSError:
            continue
    return start_port


def open_browser_later(url: str, delay_sec: float = 1.2):
    import threading
    import time
    import webbrowser

    def _open():
        time.sleep(delay_sec)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


def main():
    import uvicorn
    raw_port = os.getenv("PORT")
    if raw_port:
        port = int(raw_port)
    else:
        port = find_available_port(8000)

    url = f"http://localhost:{port}"
    print("\n" + "=" * 70)
    print(">>> GERADOR DE QR CODE ESTÁTICO & PERMANENTE (MODO DESKTOP)")
    print("=" * 70)
    print(f"-> Abrindo painel automaticamente no seu navegador: {url}")
    if LAN_IP and LAN_IP != "127.0.0.1":
        print(f"-> Acesso via Celular (mesma rede Wi-Fi): http://{LAN_IP}:{port}")
    print("=" * 70)
    print(f"-> Banco de dados local: {LOCAL_DB_FILE}")
    print("-> Para encerrar o programa, basta fechar esta janela.")
    print("=" * 70 + "\n")

    open_browser_later(url)
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False)


if __name__ == "__main__":
    main()
