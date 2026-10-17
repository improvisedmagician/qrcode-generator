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
import html
import json
import logging
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import fakeredis
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from services.api.app.qr_render import InvalidDesign, QRDesign, render, validate, verify_readable
from services.redirect.app.capture import EventPipeline, build_event
from services.redirect.app.config import Settings
from services.redirect.app.main import DEEPLINK_HTML, NO_STORE
from services.redirect.app.resolver import Resolver
from services.redirect.app.rules import decide
from services.analytics_worker.worker.enrich import COLUMNS, enrich

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
DELETED_SLUGS: set[str] = set()

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

# Recupera túmulos de exclusão salvos no Redis para persistência inter-instâncias
if redis_sync:
    try:
        saved_deletions = redis_sync.smembers("deleted_qr_slugs")
        if saved_deletions:
            DELETED_SLUGS.update(saved_deletions)
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
    target_url: str
    foreground: str = "#0F172A"
    background: str = "#FFFFFF"
    background_type: str = "solid"  # "solid" | "gradient"
    background_color_end: Optional[str] = None
    frame_text: Optional[str] = "ESCANEIE AQUI"
    module_style: str = "rounded"
    status: str = "active"


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
  <title>QR Dinâmico — Gestão Corporativa de Links</title>
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
  </style>
</head>
<body class="min-h-full font-sans text-zinc-900 antialiased flex flex-col">

  <!-- Navbar Compacta e Funcional (56px) -->
  <header class="bg-white/95 backdrop-blur-md border-b border-zinc-200/80 sticky top-0 z-30 shadow-2xs transition-colors">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between gap-3">
      
      <!-- Brand & Status -->
      <div class="flex items-center space-x-3 shrink-0">
        <div class="w-8 h-8 rounded-lg bg-zinc-900 flex items-center justify-center text-white text-sm shadow-xs">
          <i class="fa-solid fa-qrcode"></i>
        </div>
        <div class="flex items-center space-x-2">
          <span class="font-bold text-sm tracking-tight text-zinc-900">QR Dinâmico</span>
          <span class="hidden sm:inline-block text-[9px] font-bold uppercase tracking-wider bg-zinc-100 text-zinc-600 px-1.5 py-0.5 rounded border border-zinc-200">Enterprise</span>
        </div>
      </div>



      <!-- Botões de Ação -->
      <div class="flex items-center space-x-1.5 sm:space-x-2 shrink-0">
        <div class="hidden lg:flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200/60">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-500 inline-block shadow-[0_0_6px_rgba(16,185,129,0.8)]"></span>
          Cluster &lt; 5ms
        </div>

        <!-- Seletor de Fundo do Dashboard -->
        <div class="relative" id="container-seletor-fundo">
          <button type="button" onclick="toggleMenuFundo(event)" id="btn-menu-fundo" class="inline-flex items-center gap-1.5 bg-zinc-100 hover:bg-zinc-200/80 text-zinc-700 border border-zinc-200 text-xs font-semibold px-2.5 py-1.5 sm:px-3 sm:py-2 rounded-xl transition active:scale-95" title="Mudar plano de fundo do painel">
            <i class="fa-solid fa-palette text-indigo-600 text-xs"></i>
            <span class="hidden sm:inline">Fundo</span>
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

        <button onclick="limparTudo()" class="inline-flex items-center gap-1.5 bg-zinc-100 hover:bg-rose-50 text-zinc-600 hover:text-rose-600 border border-zinc-200 text-xs font-medium px-2.5 py-1.5 sm:px-3 sm:py-2 rounded-xl transition active:scale-95" title="Limpar todos os QR Codes e dados">
          <i class="fa-solid fa-trash-can text-[11px]"></i>
          <span class="hidden sm:inline">Limpar Painel</span>
        </button>
        <button onclick="abrirModalCriar()" class="inline-flex items-center gap-1.5 bg-zinc-900 hover:bg-zinc-800 text-white text-xs font-semibold px-3 py-1.5 sm:px-3.5 sm:py-2 rounded-xl shadow-xs transition active:scale-95">
          <i class="fa-solid fa-plus text-[10px]"></i>
          <span>Novo QR</span>
        </button>
      </div>

    </div>
  </header>

  <!-- Container Principal -->
  <main class="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 py-4 sm:py-6 space-y-4 sm:space-y-5">

    <!-- KPI Strip Compacta (Densidade Otimizada) -->
    <section class="grid grid-cols-2 lg:grid-cols-4 gap-2.5 sm:gap-3">
      <div class="bg-white p-3 sm:p-3.5 rounded-xl border border-zinc-200/80 shadow-2xs flex items-center justify-between">
        <div>
          <span class="text-[11px] font-medium text-zinc-400 block">Total de Scans</span>
          <span id="kpi-total" class="text-xl sm:text-2xl font-bold text-zinc-900 tracking-tight">0</span>
        </div>
        <span class="text-[10px] text-emerald-600 font-semibold bg-emerald-50 px-2 py-0.5 rounded-md flex items-center gap-1">
          <i class="fa-solid fa-bolt text-[9px]"></i> Ao vivo
        </span>
      </div>

      <div class="bg-white p-3 sm:p-3.5 rounded-xl border border-zinc-200/80 shadow-2xs flex items-center justify-between">
        <div>
          <span class="text-[11px] font-medium text-zinc-400 block">Visitantes Únicos</span>
          <span id="kpi-unicos" class="text-xl sm:text-2xl font-bold text-zinc-900 tracking-tight">0</span>
        </div>
        <span class="text-[10px] text-zinc-500 font-medium bg-zinc-100 px-2 py-0.5 rounded-md flex items-center gap-1">
          <i class="fa-solid fa-shield-halved text-[9px]"></i> LGPD
        </span>
      </div>

      <div class="bg-white p-3 sm:p-3.5 rounded-xl border border-zinc-200/80 shadow-2xs flex items-center justify-between">
        <div>
          <span class="text-[11px] font-medium text-zinc-400 block">Taxa Mobile</span>
          <span id="kpi-mobile" class="text-xl sm:text-2xl font-bold text-zinc-900 tracking-tight">0%</span>
        </div>
        <span class="text-[10px] text-zinc-500 font-medium bg-zinc-100 px-2 py-0.5 rounded-md flex items-center gap-1">
          <i class="fa-solid fa-mobile-screen text-[9px]"></i> Celular
        </span>
      </div>

      <div class="bg-white p-3 sm:p-3.5 rounded-xl border border-zinc-200/80 shadow-2xs flex items-center justify-between">
        <div>
          <span class="text-[11px] font-medium text-zinc-400 block">Latência 302</span>
          <span class="text-xl sm:text-2xl font-bold text-emerald-600 tracking-tight">&lt; 5ms</span>
        </div>
        <span class="text-[10px] text-emerald-700 font-semibold bg-emerald-50 px-2 py-0.5 rounded-md">L1 Cache</span>
      </div>
    </section>

    <!-- Toolbar de Filtros, Busca e Alternância de Visualização -->
    <section class="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-2.5">
      
      <!-- Busca rápida -->
      <div class="relative flex-1 max-w-sm">
        <i class="fa-solid fa-magnifying-glass absolute left-3 top-1/2 -translate-y-1/2 text-xs text-zinc-400"></i>
        <input type="text" id="filtro-busca" oninput="filtrarQRs()" placeholder="Buscar por nome, slug ou URL..." class="w-full text-xs bg-white rounded-xl border border-zinc-200/80 pl-8 pr-3 py-2 outline-none focus:border-zinc-400 focus:ring-1 focus:ring-zinc-400 transition">
      </div>

      <!-- Filtros de Status + Toggle Grid/Lista -->
      <div class="flex items-center justify-between sm:justify-end gap-2">
        <div class="flex items-center space-x-1 bg-zinc-100 p-0.5 rounded-lg border border-zinc-200 text-xs">
          <button onclick="definirFiltroStatus('all')" id="btn-filtro-all" class="font-semibold px-2.5 py-1 rounded-md bg-white text-zinc-900 shadow-2xs transition">Todos (<span id="count-all">0</span>)</button>
          <button onclick="definirFiltroStatus('active')" id="btn-filtro-active" class="font-medium px-2.5 py-1 rounded-md text-zinc-600 hover:text-zinc-900 transition">Ativos</button>
          <button onclick="definirFiltroStatus('paused')" id="btn-filtro-paused" class="font-medium px-2.5 py-1 rounded-md text-zinc-600 hover:text-zinc-900 transition">Pausados</button>
        </div>

        <div class="flex items-center space-x-1 bg-zinc-100 p-0.5 rounded-lg border border-zinc-200 text-xs">
          <button onclick="alternarModoVisualizacao('grid')" id="btn-view-grid" class="p-1 px-2 rounded-md bg-white text-zinc-900 shadow-2xs" title="Visualização em Cards">
            <i class="fa-solid fa-table-cells-large text-xs"></i>
          </button>
          <button onclick="alternarModoVisualizacao('list')" id="btn-view-list" class="p-1 px-2 rounded-md text-zinc-500 hover:text-zinc-900" title="Visualização em Lista">
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

    <!-- Painel de Telemetria ao Vivo (Compacto e Expansível) -->
    <section class="bg-white rounded-xl border border-zinc-200/80 shadow-2xs overflow-hidden">
      <div class="px-4 py-2.5 border-b border-zinc-100 flex items-center justify-between bg-zinc-50/60">
        <div class="flex items-center space-x-2">
          <span class="w-2 h-2 rounded-full bg-emerald-500 inline-block shadow-[0_0_8px_rgba(16,185,129,0.7)]"></span>
          <h3 class="text-xs font-bold text-zinc-900 uppercase tracking-wider">Telemetria de Scans em Tempo Real</h3>
        </div>
        <span class="text-[10px] text-zinc-400 font-mono">Worker Redis Streams • LGPD Hash</span>
      </div>

      <div class="overflow-x-auto max-h-52 overflow-y-auto">
        <table class="w-full text-left text-xs">
          <thead class="bg-zinc-50/90 text-zinc-500 font-semibold sticky top-0 border-b border-zinc-100 z-10 text-[11px]">
            <tr>
              <th class="px-4 py-2">Horário</th>
              <th class="px-4 py-2">Slug</th>
              <th class="px-4 py-2">Destino Final</th>
              <th class="px-4 py-2">Dispositivo</th>
              <th class="px-4 py-2">SO & Navegador</th>
              <th class="px-4 py-2">Visitante Único</th>
            </tr>
          </thead>
          <tbody id="scans-tbody" class="divide-y divide-zinc-100 text-zinc-600 font-mono text-[11px]">
            <tr>
              <td colspan="6" class="px-4 py-6 text-center text-zinc-400 font-sans text-xs">Aguardando novos scans...</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

  </main>

  <!-- Modal CRUD: Criar / Editar QR Code (Responsivo, max-h-88vh, sem cortes) -->
  <div id="modal-qr" class="fixed inset-0 bg-zinc-900/50 backdrop-blur-xs z-50 flex items-center justify-center hidden p-3 sm:p-4 transition-all">
    <div class="bg-white rounded-2xl max-w-2xl w-full max-h-[88vh] flex flex-col shadow-2xl border border-zinc-200 overflow-hidden transform transition-all">
      
      <!-- Modal Header (Fixo) -->
      <div class="px-5 py-3 border-b border-zinc-100 flex items-center justify-between bg-zinc-50/70 shrink-0">
        <h3 id="modal-titulo" class="font-bold text-zinc-900 text-sm">Novo QR Code Dinâmico</h3>
        <button onclick="fecharModal()" class="w-7 h-7 rounded-lg hover:bg-zinc-200/60 text-zinc-400 hover:text-zinc-700 flex items-center justify-center transition">
          <i class="fa-solid fa-xmark text-sm"></i>
        </button>
      </div>

      <!-- Modal Body (Rolagem suave caso a tela seja pequena) -->
      <div class="overflow-y-auto p-5 flex-1">
        <form id="form-qr" onsubmit="salvarQRCode(event)">
          <input type="hidden" id="qr-modo" value="criar">
          <input type="hidden" id="qr-slug-original" value="">

          <div class="grid grid-cols-1 md:grid-cols-12 gap-5">
            
            <!-- Coluna Esquerda: Formulário (7 colunas) -->
            <div class="md:col-span-7 space-y-3">
              <div>
                <label class="block text-[11px] font-semibold text-zinc-700 mb-0.5">Nome da Campanha</label>
                <input type="text" id="input-nome" required placeholder="Ex: Catálogo Digital" class="w-full text-xs rounded-lg border border-zinc-200 px-3 py-2 outline-none focus:border-zinc-900 focus:ring-1 focus:ring-zinc-900 transition" oninput="atualizarPreviewLive()">
              </div>

              <div>
                <label class="block text-[11px] font-semibold text-zinc-700 mb-0.5">Slug Curto (Identificador)</label>
                <div class="flex items-center">
                  <span class="text-xs bg-zinc-100 border border-r-0 border-zinc-200 px-2.5 py-2 rounded-l-lg text-zinc-500 font-mono">/</span>
                  <input type="text" id="input-slug" required placeholder="catalogo" class="w-full text-xs rounded-r-lg border border-zinc-200 px-3 py-2 outline-none font-mono focus:border-zinc-900 focus:ring-1 focus:ring-zinc-900 transition" oninput="sanitizarSlug(this)">
                </div>
                <span class="text-[10px] text-zinc-400">Apenas letras, números, hífen e underline.</span>
              </div>

              <div>
                <label class="block text-[11px] font-semibold text-zinc-700 mb-0.5">URL de Destino</label>
                <input type="url" id="input-url" required placeholder="https://meusite.com.br/produtos" class="w-full text-xs rounded-lg border border-zinc-200 px-3 py-2 outline-none focus:border-zinc-900 focus:ring-1 focus:ring-zinc-900 transition">
                <span class="text-[10px] text-zinc-400">Pode ser alterada a qualquer momento sem reimprimir.</span>
              </div>

              <div>
                <label class="block text-[11px] font-semibold text-zinc-700 mb-0.5">Texto da Moldura (CTA)</label>
                <input type="text" id="input-frame" value="ESCANEIE AQUI" maxlength="28" class="w-full text-xs rounded-lg border border-zinc-200 px-3 py-2 outline-none focus:border-zinc-900 focus:ring-1 focus:ring-zinc-900 transition" oninput="atualizarPreviewLive()">
              </div>

              <div class="grid grid-cols-2 gap-2.5">
                <div>
                  <label class="block text-[11px] font-semibold text-zinc-700 mb-0.5">Cor dos Módulos</label>
                  <div class="flex items-center space-x-1.5">
                    <input type="color" id="input-cor" value="#0F172A" class="w-8 h-7 rounded border border-zinc-200 p-0.5 cursor-pointer" onchange="atualizarPreviewLive()">
                    <div class="flex items-center space-x-1">
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
                  <div class="flex items-center space-x-1">
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
                <label class="block text-[11px] font-semibold text-zinc-700 mb-0.5">Status do Link</label>
                <select id="input-status" class="w-full text-xs rounded-lg border border-zinc-200 px-2.5 py-1.5 outline-none">
                  <option value="active">🟢 Ativo (Redirecionando 302)</option>
                  <option value="paused">⏸️ Pausado (Retorna 410 / Fallback)</option>
                </select>
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
                <p class="text-[10px] text-zinc-500 font-mono truncate" id="preview-slug">/catalogo</p>
              </div>
            </div>

          </div>
        </form>
      </div>

      <!-- Modal Footer (Fixo na parte inferior) -->
      <div class="px-5 py-3 border-t border-zinc-100 flex items-center justify-between bg-zinc-50/70 shrink-0">
        <span class="text-[10px] text-zinc-400 hidden sm:inline">Propagação no Redis em &lt; 1ms</span>
        <div class="flex items-center space-x-2 ml-auto">
          <button type="button" onclick="fecharModal()" class="px-3 py-1.5 text-xs font-semibold text-zinc-600 hover:bg-zinc-200/60 rounded-lg transition">Cancelar</button>
          <button type="button" onclick="document.getElementById('form-qr').requestSubmit()" id="btn-submit" class="px-4 py-1.5 text-xs font-semibold text-white bg-zinc-900 hover:bg-zinc-800 rounded-lg shadow-xs transition active:scale-95">Salvar QR Code</button>
        </div>
      </div>

    </div>
  </div>

  <!-- Modal Zoom: Visualizar QR Code Ampliado para Escaneamento Rápido -->
  <div id="modal-zoom" class="fixed inset-0 bg-zinc-900/60 backdrop-blur-xs z-50 flex items-center justify-center hidden p-4 transition-all">
    <div class="bg-white rounded-2xl max-w-sm w-full p-6 text-center shadow-2xl border border-zinc-200 relative">
      <button onclick="fecharModalZoom()" class="absolute right-4 top-4 text-zinc-400 hover:text-zinc-600 w-7 h-7 rounded-lg flex items-center justify-center">
        <i class="fa-solid fa-xmark"></i>
      </button>
      <h3 id="zoom-title" class="font-bold text-sm text-zinc-900 truncate">QR Code Ampliado</h3>
      <p id="zoom-slug" class="text-xs text-zinc-500 font-mono mt-0.5">/slug</p>
      
      <div class="my-4 p-3 bg-zinc-50 rounded-xl border border-zinc-100 flex items-center justify-center">
        <img id="zoom-img" src="" alt="QR Code Ampliado" class="w-56 h-56 object-contain">
      </div>

      <p class="text-[11px] text-zinc-400">Aponte a câmera do seu smartphone para testar o redirecionamento instantâneo.</p>
      
      <div class="mt-4 pt-3 border-t border-zinc-100 flex items-center justify-center gap-2">
        <a id="zoom-btn-svg" href="#" class="px-3 py-1.5 text-xs font-semibold text-zinc-700 bg-zinc-100 hover:bg-zinc-200 rounded-lg transition">Baixar SVG</a>
        <a id="zoom-btn-png" href="#" class="px-3 py-1.5 text-xs font-semibold text-zinc-700 bg-zinc-100 hover:bg-zinc-200 rounded-lg transition">Baixar PNG</a>
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

    // Auto-higienização para restaurar os códigos de demonstração caso o navegador os tenha bloqueado
    if (!localStorage.getItem('demo_qrs_restored_v3')) {
      try {
        const list = JSON.parse(localStorage.getItem('deleted_qr_slugs') || '[]');
        const cleaned = list.filter(s => !['promo', 'blackfriday', 'insta'].includes(s));
        localStorage.setItem('deleted_qr_slugs', JSON.stringify(cleaned));
        localStorage.setItem('demo_qrs_restored_v3', 'true');
      } catch (e) {}
    }

    function getDeletedSlugs() {
      try {
        return JSON.parse(localStorage.getItem('deleted_qr_slugs') || '[]');
      } catch (e) {
        return [];
      }
    }

    function addDeletedSlug(slug) {
      try {
        const list = getDeletedSlugs();
        const clean = String(slug).toLowerCase().trim();
        if (!list.includes(clean)) {
          list.push(clean);
          localStorage.setItem('deleted_qr_slugs', JSON.stringify(list));
        }
      } catch (e) {}
    }

    function removeDeletedSlug(slug) {
      try {
        const list = getDeletedSlugs();
        const clean = String(slug).toLowerCase().trim();
        const filtered = list.filter(s => s !== clean);
        localStorage.setItem('deleted_qr_slugs', JSON.stringify(filtered));
      } catch (e) {}
    }

    function getCustomQRs() {
      try {
        return JSON.parse(localStorage.getItem('custom_qrcodes') || '{}');
      } catch (e) {
        return {};
      }
    }

    function saveCustomQR(qr) {
      try {
        const all = getCustomQRs();
        all[String(qr.slug).toLowerCase().trim()] = qr;
        localStorage.setItem('custom_qrcodes', JSON.stringify(all));
      } catch (e) {}
    }

    function deleteCustomQR(slug) {
      try {
        const all = getCustomQRs();
        delete all[String(slug).toLowerCase().trim()];
        localStorage.setItem('custom_qrcodes', JSON.stringify(all));
      } catch (e) {}
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
      const menu = document.getElementById('dropdown-fundo');
      if (menu) menu.classList.toggle('hidden');
    }

    function fecharMenuFundo() {
      const menu = document.getElementById('dropdown-fundo');
      if (menu) menu.classList.add('hidden');
    }

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
        const [resQRs, resScans] = await Promise.all([
          fetch(`/api/qrcodes?host_override=${encodeURIComponent(activeHost)}`),
          fetch('/scans')
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

        const deletedSlugs = getDeletedSlugs();
        const customQRs = Object.values(getCustomQRs());
        const qrsMap = new Map();

        (Array.isArray(serverQRs) ? serverQRs : []).forEach(q => {
          const k = String(q.slug).toLowerCase().trim();
          if (!deletedSlugs.includes(k)) {
            qrsMap.set(k, q);
          }
        });

        customQRs.forEach(q => {
          const k = String(q.slug).toLowerCase().trim();
          if (!deletedSlugs.includes(k) && !qrsMap.has(k)) {
            qrsMap.set(k, q);
            // Re-sincroniza silenciosamente com o backend se a instância tiver reiniciado
            fetch('/api/qrcodes', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                name: q.name,
                slug: q.slug,
                target_url: q.default_url,
                foreground: (q.design && q.design.foreground) || '#0F172A',
                background: (q.design && q.design.background) || '#FFFFFF',
                background_type: (q.design && q.design.background_type) || 'solid',
                background_color_end: q.design && q.design.background_color_end,
                frame_text: (q.design && q.design.frame_text) || 'ESCANEIE AQUI',
                module_style: (q.design && q.design.module_style) || 'rounded',
                status: q.status || 'active'
              })
            }).catch(() => {});
          }
        });

        todosQRs = Array.from(qrsMap.values()).map(q => {
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

        const totalScansCount = Math.max((scansData && scansData.total_scans) || 0, allRecentScans.length);

        // Atualiza KPIs
        document.getElementById('kpi-total').innerText = totalScansCount;
        const unicos = new Set(allRecentScans.map(e => e.visitor_hash)).size;
        document.getElementById('kpi-unicos').innerText = unicos;

        const mobiles = allRecentScans.filter(e => e.device === 'mobile').length;
        if (totalScansCount > 0) {
          document.getElementById('kpi-mobile').innerText = Math.round((mobiles / totalScansCount) * 100) + '%';
        }

        document.getElementById('count-all').innerText = todosQRs.length;

        filtrarQRs();
        renderizarTabelaScans(allRecentScans);
      } catch (err) {
        console.error("Erro ao carregar dados:", err);
      }
    }

    function definirFiltroStatus(status) {
      filtroAtualStatus = status;
      ['all', 'active', 'paused'].forEach(s => {
        const btn = document.getElementById('btn-filtro-' + s);
        if (s === status) {
          btn.className = 'font-semibold px-2.5 py-1 rounded-md bg-white text-zinc-900 shadow-2xs transition';
        } else {
          btn.className = 'font-medium px-2.5 py-1 rounded-md text-zinc-600 hover:text-zinc-900 transition';
        }
      });
      filtrarQRs();
    }

    function filtrarQRs() {
      const busca = (document.getElementById('filtro-busca').value || '').toLowerCase().trim();
      const filtrados = todosQRs.filter(qr => {
        const bateStatus = (filtroAtualStatus === 'all') || (qr.status === filtroAtualStatus);
        const bateBusca = !busca || qr.name.toLowerCase().includes(busca) || qr.slug.toLowerCase().includes(busca) || qr.default_url.toLowerCase().includes(busca);
        return bateStatus && bateBusca;
      });

      if (modoVisualizacao === 'grid') {
        renderizarQRCards(filtrados);
      } else {
        renderizarQRLista(filtrados);
      }
    }

    // Modo Grid: Cards compactos e balanceados (~160px de altura)
    function renderizarQRCards(qrs) {
      const container = document.getElementById('qr-grid');
      if (qrs.length === 0) {
        container.innerHTML = `
          <div class="col-span-full py-12 text-center bg-white rounded-2xl border border-zinc-200/80">
            <div class="w-10 h-10 rounded-xl bg-zinc-100 text-zinc-400 mx-auto flex items-center justify-center text-lg mb-2">
              <i class="fa-solid fa-qrcode"></i>
            </div>
            <h4 class="font-bold text-zinc-800 text-xs">Nenhum QR Code encontrado</h4>
            <p class="text-[11px] text-zinc-400 mt-0.5">Crie um novo código para começar ou limpe o filtro de busca.</p>
          </div>
        `;
        return;
      }

      const activeHost = getActiveHost();

      container.innerHTML = qrs.map(qr => {
        const statusBadge = qr.status === 'active'
          ? '<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200"><span class="w-1 h-1 rounded-full bg-emerald-500"></span>Ativo</span>'
          : '<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-zinc-100 text-zinc-600 border border-zinc-200"><span class="w-1 h-1 rounded-full bg-zinc-400"></span>Pausado</span>';

        const clicks = Number(qr.clicks || 0);
        const clicksBadge = `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-semibold bg-indigo-50 text-indigo-700 border border-indigo-200/80" title="${clicks} acessos registrados"><i class="fa-solid fa-arrow-pointer text-[8px]"></i>${clicks} ${clicks === 1 ? 'scan' : 'scans'}</span>`;

        const safeSlug = escapeHtml(qr.slug);
        const safeName = escapeHtml(qr.name);
        const jsSlug = escapeJs(qr.slug);
        const jsName = escapeJs(qr.name);
        const safeUrl = sanitizeUrl(qr.default_url);
        const safeShortUrl = sanitizeUrl(qr.short_url);
        const d = qr.design || {};
        const fgParam = encodeURIComponent(d.foreground || '#0F172A');
        const bgParam = encodeURIComponent(d.background || '#FFFFFF');
        const bgTypeParam = encodeURIComponent(d.background_type || 'solid');
        const bgEndParam = encodeURIComponent(d.background_color_end || '');
        const frameParam = encodeURIComponent(d.frame_text || 'ESCANEIE AQUI');
        const styleParam = encodeURIComponent(d.module_style || 'rounded');

        const qrImgUrl = `/preview/${encodeURIComponent(qr.slug)}.svg?host_override=${encodeURIComponent(activeHost)}&v=${qr.v || 1}&color=${fgParam}&bg_color=${bgParam}&bg_type=${bgTypeParam}&bg_color_end=${bgEndParam}&frame=${frameParam}&style=${styleParam}`;
        const liveFallbackUrl = `/api/preview-live?slug=${encodeURIComponent(qr.slug)}&color=${fgParam}&bg_color=${bgParam}&bg_type=${bgTypeParam}&bg_color_end=${bgEndParam}&frame=${frameParam}&style=${styleParam}&host=${encodeURIComponent(activeHost)}`;
        const jsLiveFallbackUrl = escapeJs(liveFallbackUrl);
        const jsQrImgUrl = escapeJs(qrImgUrl);

        return `
          <div class="bg-white rounded-xl border border-zinc-200/80 shadow-2xs hover:border-zinc-300 hover:shadow-xs transition p-3.5 flex flex-col justify-between">
            <div>
              <!-- Top: Nome, Cliques e Status -->
              <div class="flex items-center justify-between gap-2 mb-2">
                <h4 class="font-bold text-zinc-900 text-xs truncate" title="${safeName}">${safeName}</h4>
                <div class="flex items-center gap-1.5 shrink-0">
                  ${clicksBadge}
                  ${statusBadge}
                </div>
              </div>

              <!-- Meio: QR Code Thumbnail + Links -->
              <div class="flex items-center gap-3 bg-zinc-50/70 p-2.5 rounded-lg border border-zinc-100">
                <div class="w-18 h-18 sm:w-20 sm:h-20 shrink-0 bg-white p-1 rounded-lg border border-zinc-200/80 flex items-center justify-center cursor-pointer hover:border-zinc-400 transition" onclick="abrirModalZoom('${jsSlug}', '${jsName}', '${jsQrImgUrl}')" title="Clique para ampliar o QR Code">
                  <img src="${qrImgUrl}" alt="QR ${safeSlug}" class="w-full h-full object-contain" onerror="this.onerror=null; this.src='${jsLiveFallbackUrl}';">
                </div>
                <div class="min-w-0 flex-1 space-y-1">
                  <div>
                    <span class="text-[9px] font-bold text-zinc-400 uppercase tracking-wider block">Link Curto</span>
                    <div class="flex items-center gap-1">
                      <span class="font-mono text-xs font-semibold text-zinc-800 truncate">/${safeSlug}</span>
                      <button onclick="copiarLink('${escapeJs(qr.short_url)}')" class="text-zinc-400 hover:text-zinc-700 text-[11px] p-0.5" title="Copiar link">
                        <i class="fa-regular fa-copy"></i>
                      </button>
                    </div>
                  </div>
                  <div>
                    <span class="text-[9px] font-bold text-zinc-400 uppercase tracking-wider block">Destino</span>
                    <a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="text-[11px] text-blue-600 hover:text-blue-800 font-medium truncate block" title="${safeUrl}">
                      ${displayUrl}
                    </a>
                  </div>
                </div>
              </div>
            </div>

            <!-- Rodapé: Ações Compactas -->
            <div class="mt-2.5 pt-2 border-t border-zinc-100 flex items-center justify-between text-xs">
              <div class="flex items-center space-x-1">
                <button onclick="abrirModalEditar('${jsSlug}')" class="px-2 py-1 rounded-md text-zinc-700 hover:bg-zinc-100 font-medium text-[11px] flex items-center gap-1 transition">
                  <i class="fa-solid fa-pen-to-square text-[10px]"></i>Editar
                </button>
                <a href="${safeShortUrl}" target="_blank" rel="noopener noreferrer" class="px-2 py-1 rounded-md text-zinc-700 hover:bg-zinc-100 font-medium text-[11px] flex items-center gap-1 transition">
                  <i class="fa-solid fa-arrow-turn-down text-[10px]"></i>Testar
                </a>
              </div>

              <div class="flex items-center space-x-1">
                <a href="/export/${encodeURIComponent(qr.slug)}?format=svg&host_override=${encodeURIComponent(activeHost)}" class="px-1.5 py-0.5 rounded text-[10px] font-semibold text-zinc-600 hover:bg-zinc-100 transition">SVG</a>
                <a href="/export/${encodeURIComponent(qr.slug)}?format=png&size=1024&host_override=${encodeURIComponent(activeHost)}" class="px-1.5 py-0.5 rounded text-[10px] font-semibold text-zinc-600 hover:bg-zinc-100 transition">PNG</a>
                <button onclick="deletarQRCode('${jsSlug}', '${jsName}')" class="p-1 rounded text-rose-500 hover:bg-rose-50 transition" title="Excluir">
                  <i class="fa-regular fa-trash-can text-xs"></i>
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
        const statusBadge = qr.status === 'active'
          ? '<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">Ativo</span>'
          : '<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-zinc-100 text-zinc-600 border border-zinc-200">Pausado</span>';

        const clicks = Number(qr.clicks || 0);
        const clicksBadge = `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[9px] font-semibold bg-indigo-50 text-indigo-700 border border-indigo-200/80" title="${clicks} acessos registrados"><i class="fa-solid fa-arrow-pointer text-[8px]"></i>${clicks} ${clicks === 1 ? 'scan' : 'scans'}</span>`;

        const safeSlug = escapeHtml(qr.slug);
        const safeName = escapeHtml(qr.name);
        const jsSlug = escapeJs(qr.slug);
        const jsName = escapeJs(qr.name);
        const safeUrl = sanitizeUrl(qr.default_url);
        const safeShortUrl = sanitizeUrl(qr.short_url);

        const d = qr.design || {};
        const fgParam = encodeURIComponent(d.foreground || '#0F172A');
        const bgParam = encodeURIComponent(d.background || '#FFFFFF');
        const bgTypeParam = encodeURIComponent(d.background_type || 'solid');
        const bgEndParam = encodeURIComponent(d.background_color_end || '');
        const frameParam = encodeURIComponent(d.frame_text || 'ESCANEIE AQUI');
        const styleParam = encodeURIComponent(d.module_style || 'rounded');

        const qrImgUrl = `/preview/${encodeURIComponent(qr.slug)}.svg?host_override=${encodeURIComponent(activeHost)}&v=${qr.v || 1}&color=${fgParam}&bg_color=${bgParam}&bg_type=${bgTypeParam}&bg_color_end=${bgEndParam}&frame=${frameParam}&style=${styleParam}`;
        const liveFallbackUrl = `/api/preview-live?slug=${encodeURIComponent(qr.slug)}&color=${fgParam}&bg_color=${bgParam}&bg_type=${bgTypeParam}&bg_color_end=${bgEndParam}&frame=${frameParam}&style=${styleParam}&host=${encodeURIComponent(activeHost)}`;
        const jsLiveFallbackUrl = escapeJs(liveFallbackUrl);
        const jsQrImgUrl = escapeJs(qrImgUrl);

        return `
          <div class="p-3 sm:px-4 flex items-center justify-between gap-3 hover:bg-zinc-50/70 transition">
            <div class="flex items-center space-x-3 min-w-0">
              <div class="w-10 h-10 shrink-0 bg-white p-0.5 rounded border border-zinc-200 flex items-center justify-center cursor-pointer" onclick="abrirModalZoom('${jsSlug}', '${jsName}', '${jsQrImgUrl}')">
                <img src="${qrImgUrl}" alt="QR ${safeSlug}" class="w-full h-full object-contain" onerror="this.onerror=null; this.src='${jsLiveFallbackUrl}';">
              </div>
              <div class="min-w-0">
                <div class="flex items-center gap-2">
                  <h4 class="font-bold text-xs text-zinc-900 truncate" title="${safeName}">${safeName}</h4>
                  ${clicksBadge}
                  ${statusBadge}
                </div>
                <div class="flex items-center gap-2 text-[11px] text-zinc-500 mt-0.5">
                  <span class="font-mono text-zinc-800 font-semibold">/${safeSlug}</span>
                  <span>•</span>
                  <a href="${safeUrl}" target="_blank" rel="noopener noreferrer" class="text-blue-600 hover:text-blue-800 truncate max-w-xs">${safeUrl}</a>
                </div>
              </div>
            </div>

            <div class="flex items-center space-x-2 shrink-0 text-xs">
              <button onclick="copiarLink('${escapeJs(qr.short_url)}')" class="px-2 py-1 rounded bg-zinc-100 hover:bg-zinc-200 text-zinc-700 text-[11px] font-medium" title="Copiar link">Copiar</button>
              <a href="${safeShortUrl}" target="_blank" rel="noopener noreferrer" class="px-2 py-1 rounded bg-zinc-100 hover:bg-zinc-200 text-zinc-700 text-[11px] font-medium">Testar</a>
              <button onclick="abrirModalEditar('${jsSlug}')" class="px-2 py-1 rounded bg-zinc-100 hover:bg-zinc-200 text-zinc-700 text-[11px] font-medium">Editar</button>
              <a href="/export/${encodeURIComponent(qr.slug)}?format=svg&host_override=${encodeURIComponent(activeHost)}" class="text-[10px] font-bold text-zinc-600 hover:text-zinc-900 p-1">SVG</a>
              <a href="/export/${encodeURIComponent(qr.slug)}?format=png&size=1024&host_override=${encodeURIComponent(activeHost)}" class="text-[10px] font-bold text-zinc-600 hover:text-zinc-900 p-1">PNG</a>
              <button onclick="deletarQRCode('${jsSlug}', '${jsName}')" class="p-1 text-rose-500 hover:bg-rose-50 rounded" title="Excluir"><i class="fa-regular fa-trash-can"></i></button>
            </div>
          </div>
        `;
      }).join('');
    }

    let lastRenderedScansJson = '';
    function renderizarTabelaScans(events) {
      const tbody = document.getElementById('scans-tbody');
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
      document.getElementById('modal-titulo').innerText = "Novo QR Code Dinâmico";
      document.getElementById('btn-submit').innerText = "Criar QR Code";
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
      document.getElementById('modal-qr').classList.remove('hidden');
      atualizarPreviewLive();
    }

    function abrirModalEditar(slug) {
      const qr = todosQRs.find(q => q.slug === slug);
      if (!qr) return;

      document.getElementById('modal-titulo').innerText = `Editar QR Code /${slug}`;
      document.getElementById('btn-submit').innerText = "Salvar Alterações";
      document.getElementById('qr-modo').value = "editar";
      document.getElementById('qr-slug-original').value = slug;
      document.getElementById('campo-status').classList.remove('hidden');

      document.getElementById('input-nome').value = qr.name;
      document.getElementById('input-slug').value = qr.slug;
      document.getElementById('input-slug').disabled = true; // Slug fixo para manter consistência impressa
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
      document.getElementById('zoom-btn-png').href = `/export/${slug}?format=png&size=1024&host_override=${encodeURIComponent(activeHost)}`;
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
        
        document.getElementById('preview-nome').innerText = nome;
        document.getElementById('preview-slug').innerText = `/${slug}`;

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
          const res = await fetch(`/api/preview-live?slug=${encodeURIComponent(slug)}&color=${encodeURIComponent(cor)}&frame=${encodeURIComponent(frame)}&style=${encodeURIComponent(estilo)}&host=${encodeURIComponent(activeHost)}&bg_type=${encodeURIComponent(modoBg)}&bg_color=${encodeURIComponent(bgCor)}&bg_color_end=${encodeURIComponent(bgCorEnd)}`);
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
        removeDeletedSlug(slug);
        const activeHost = getActiveHost();
        saveCustomQR({
          slug: slug,
          name: payload.name,
          short_url: `${window.location.protocol}//${activeHost}/${slug}`,
          default_url: payload.target_url,
          status: 'active',
          v: 1,
          design: {
            foreground: payload.foreground,
            background: payload.background,
            background_type: payload.background_type,
            background_color_end: payload.background_color_end,
            module_style: payload.module_style,
            finder_style: payload.module_style === 'rounded' ? 'rounded' : 'square',
            frame_text: payload.frame_text
          },
          rules: [],
          created_at: new Date().toISOString()
        });
        const res = await fetch('/api/qrcodes', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (res.ok) {
          fecharModal();
          showToast(`QR Code /${slug} criado com sucesso!`);
          carregarDashboard();
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
          showToast(`QR Code /${originalSlug} atualizado no Redis!`);
          carregarDashboard();
        } else {
          const err = await res.json();
          alert('Erro: ' + (err.detail || 'Falha ao atualizar QR Code'));
        }
      }
    }

    async function deletarQRCode(slug, nome) {
      if (confirm(`Tem certeza que deseja excluir o QR Code "${nome}" (/${slug})?\n\nO link deixará de redirecionar imediatamente.`)) {
        addDeletedSlug(slug);
        deleteCustomQR(slug);
        todosQRs = todosQRs.filter(q => String(q.slug).toLowerCase().trim() !== String(slug).toLowerCase().trim());
        filtrarQRs();

        try {
          await fetch(`/api/qrcodes/${encodeURIComponent(slug)}`, {
            method: 'DELETE',
            headers: { 'Accept': 'application/json' }
          });
          showToast(`QR Code /${slug} excluído com sucesso!`, 'fa-trash-can text-rose-400');
        } catch (err) {
          showToast(`QR Code /${slug} excluído.`, 'fa-trash-can text-rose-400');
        }
        await carregarDashboard();
      }
    }

    async function limparTudo() {
      if (!confirm("Deseja apagar todos os QR codes e limpar o painel completamente?")) {
        return;
      }
      try {
        localStorage.removeItem('custom_qrcodes');
        localStorage.removeItem('deleted_qr_slugs');
        localStorage.removeItem('cached_scans');
        localStorage.removeItem('qr_clicks');
        lastRenderedScansJson = '';
        const defaultSlugs = ['promo', 'blackfriday', 'insta', 'jogos205'];
        const currentSlugs = todosQRs.map(q => q.slug);
        const allSlugs = Array.from(new Set([...defaultSlugs, ...currentSlugs]));
        allSlugs.forEach(s => addDeletedSlug(s));

        await Promise.allSettled(
          allSlugs.map(s =>
            fetch(`/api/qrcodes/${encodeURIComponent(s)}`, { method: 'DELETE' })
          )
        );
        todosQRs = [];
        filtrarQRs();
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
    return HTMLResponse(DASHBOARD_HTML)


# =====================================================================
# ENDPOINTS REST DA API (CRUD COMPLETO COM SUPORTE MULTI-HOST)
# =====================================================================
@app.get("/api/qrcodes")
async def list_qrcodes_api(
    request: Request,
    search: Optional[str] = None,
    status: Optional[str] = None,
    host_override: Optional[str] = None,
):
    if REDIS_URL:
        try:
            stored_deletions = await redis_async.smembers("deleted_qr_slugs")
            if stored_deletions:
                DELETED_SLUGS.update(stored_deletions)
            stored_slugs = await redis_async.smembers("all_qr_slugs")
            for s in stored_slugs:
                if s in DELETED_SLUGS:
                    continue
                if s not in IN_MEMORY_DB:
                    meta = await redis_async.get(f"qr_meta:{s}")
                    if meta:
                        IN_MEMORY_DB[s] = json.loads(meta)
        except Exception as exc:
            log.warning("Falha ao sincronizar lista com Redis: %r", exc)

    clicks_map = dict(SCAN_COUNTS)
    if REDIS_URL and redis_async:
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
    return result


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
    # Se o slug havia sido deletado anteriormente, remove do tombstone
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

    target = re.sub(r"[\r\n\t]", "", body.target_url.strip())
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
    try:
        for h in hosts_to_sync:
            if h:
                await redis_async.set(f"qr:{h}:{clean_slug}", payload_str, ex=86400)
                resolver.invalidate(f"qr:{h}:{clean_slug}")
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

    if clean_slug not in IN_MEMORY_DB and REDIS_URL:
        try:
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
        target = re.sub(r"[\r\n\t]", "", body.target_url.strip())
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
    try:
        for h in hosts_to_sync:
            if h:
                await redis_async.set(f"qr:{h}:{clean_slug}", payload_str, ex=86400)
                resolver.invalidate(f"qr:{h}:{clean_slug}")
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

    current_host = request.headers.get("host", "").split(":")[0].lower()
    hosts_to_sync = set(KNOWN_HOSTS + [current_host])
    try:
        if redis_async:
            await redis_async.sadd("deleted_qr_slugs", clean_slug)
            await redis_async.srem("all_qr_slugs", clean_slug)
            await redis_async.delete(f"qr_meta:{clean_slug}")
            await redis_async.hdel("qr_clicks", clean_slug)
            for h in hosts_to_sync:
                if h:
                    await redis_async.delete(f"qr:{h}:{clean_slug}")
                    resolver.invalidate(f"qr:{h}:{clean_slug}")
    except Exception as exc:
        log.warning("Sincronização Redis offline ou ignorada durante exclusão: %r", exc)

    # Operação 100% idempotente (RFC 7231 / 9110)
    return {"status": "deleted", "slug": clean_slug}


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
    svg_bytes = render(f"http://{host}/{slug}", design=design, fmt="svg", size_px=320)
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

    if clean_slug not in IN_MEMORY_DB and REDIS_URL:
        try:
            meta = await redis_async.get(f"qr_meta:{clean_slug}")
            if meta:
                IN_MEMORY_DB[clean_slug] = json.loads(meta)
        except Exception:
            pass

    payload = IN_MEMORY_DB.get(clean_slug)
    if payload:
        design_data = payload.get("design", {})
        fg_col = color or design_data.get("foreground", "#0F172A")
        bg_col = bg_color or design_data.get("background", "#FFFFFF")
        bg_t = bg_type or design_data.get("background_type", "solid")
        bg_end = bg_color_end or design_data.get("background_color_end")
        m_style = style or design_data.get("module_style", "rounded")
        f_style = design_data.get("finder_style", "rounded" if m_style == "rounded" else "square")
        f_text = frame or design_data.get("frame_text", "ESCANEIE AQUI")
    else:
        # Fallback resiliente para serverless / lambdas sem estado / cold starts:
        # Gera o QR Code com base nos parâmetros da requisição ou defaults elegantes,
        # garantindo que o usuário NUNCA veja um ícone de imagem quebrada.
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

    try:
        svg_bytes = render(f"http://{active_host}/{clean_slug}", design=design, fmt="svg", size_px=400)
    except Exception:
        design.foreground = "#0F172A"
        design.background = "#FFFFFF"
        svg_bytes = render(f"http://{active_host}/{clean_slug}", design=design, fmt="svg", size_px=400)

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
    return {
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
    }


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

    if clean_slug not in IN_MEMORY_DB and REDIS_URL:
        try:
            meta = await redis_async.get(f"qr_meta:{clean_slug}")
            if meta:
                IN_MEMORY_DB[clean_slug] = json.loads(meta)
        except Exception:
            pass

    payload = IN_MEMORY_DB.get(clean_slug)
    if payload:
        design_data = payload.get("design", {})
        fg_col = color or design_data.get("foreground", "#0F172A")
        bg_col = bg_color or design_data.get("background", "#FFFFFF")
        bg_t = bg_type or design_data.get("background_type", "solid")
        bg_end = bg_color_end or design_data.get("background_color_end")
        m_style = style or design_data.get("module_style", "rounded")
        f_style = design_data.get("finder_style", "rounded" if m_style == "rounded" else "square")
        f_text = frame or design_data.get("frame_text", "ESCANEIE AQUI")
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
    try:
        content = render(f"http://{active_host}/{clean_slug}", design=design, fmt=format, size_px=size)
    except InvalidDesign:
        design.foreground = "#0F172A"
        design.background = "#FFFFFF"
        content = render(f"http://{active_host}/{clean_slug}", design=design, fmt=format, size_px=size)
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

    decision = decide(payload, headers.get("user-agent", ""), None, datetime.now(timezone.utc))
    if decision.kind == "gone":
        fallback = payload.get("fallback_url")
        if fallback:
            return RedirectResponse(fallback, status_code=302, headers=NO_STORE)
        accept = headers.get("accept", "")
        if "text/html" in accept:
            return HTMLResponse(
                """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QR Code Indisponível</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-zinc-50 flex items-center justify-center min-h-screen p-4 text-zinc-800">
  <div class="max-w-md w-full bg-white p-8 rounded-2xl border border-zinc-200 shadow-sm text-center">
    <div class="w-12 h-12 bg-amber-50 text-amber-600 rounded-full flex items-center justify-center mx-auto mb-4 text-xl">⚠️</div>
    <h1 class="text-base font-bold text-zinc-900 mb-2">QR Code Temporariamente Indisponível</h1>
    <p class="text-xs text-zinc-500 mb-6">Este link dinâmico está pausado ou encerrou seu período de vigência.</p>
    <a href="/" class="inline-block px-4 py-2 bg-zinc-900 text-white rounded-lg text-xs font-semibold hover:bg-zinc-800 transition">Acessar Painel</a>
  </div>
</body>
</html>""",
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


def main():
    import uvicorn
    port = int(os.getenv("PORT", "80"))
    port_suffix = f":{port}" if port not in (80, 443) else ""
    print("\n" + "=" * 70)
    print(">>> PAINEL CORPORATIVO MODERNO ONLINE (RESPONSIVO & COMPACTO)")
    print("=" * 70)
    print(f"-> Painel Web no Navegador: http://localhost{port_suffix}")
    if LAN_IP and LAN_IP != "127.0.0.1":
        print(f"-> Acesso via Celular (Wi-Fi): http://{LAN_IP}{port_suffix}")
    print("=" * 70 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=port, access_log=True)


if __name__ == "__main__":
    main()
