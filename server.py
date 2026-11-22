"""Backend FastAPI do História ao Vivo — 100% Google (Gemini Decision 1 + Cloud Run).

Suporta:
- Gemini Decisions API (gemini-decision-1) via GEMINI_API_KEY (texto E áudio direto!)
- Opcionalmente TypeSafe Jev (jev-latest) via TYPESAFE_API_KEY (texto)

Execução local / Cloud Run:
    export GEMINI_API_KEY="sua-chave-do-google-ai-studio"
    uvicorn server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
GEMINI_DECISIONS_URL = "https://generativelanguage.googleapis.com/v1beta/decisions"
GEMINI_DECISION_MODEL = "gemini-decision-1"
MAX_AUDIO_BYTES = 5 * 1024 * 1024  # 5 MB por trecho curto de voz

ALLOWED_AUDIO_MIME_TYPES = {
    "audio/webm",
    "audio/mp4",
    "audio/mpeg",
    "audio/mp3",
    "audio/ogg",
    "audio/wav",
    "audio/x-wav",
    "audio/aac",
    "audio/m4a",
    "audio/x-m4a",
}

app = FastAPI(title="História ao Vivo — Gemini Decision 1 Backend")

allowed_origins = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()
]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )


class EvaluateRequest(BaseModel):
    fala_recente: str = Field(..., min_length=1, max_length=2000)
    estado_atual: dict[str, Any] = Field(default_factory=dict)


def build_gemini_questions() -> dict[str, Any]:
    """Esquema de perguntas fechadas (choice + noul) para gemini-decision-1.

    Inclui opção `sem_mudanca` nos `choice` conforme boa prática da Decisions API,
    evitando trocas aleatórias quando a fala não mencionar aquele atributo.
    """
    return {
        "cenario": {
            "type": "choice",
            "instructions": (
                "A fala recente indica que a história está acontecendo em algum destes cenários? "
                "Escolha sem_mudanca se a fala não mencionar lugar ou mudança de ambiente."
            ),
            "criteria": {
                "quarto": "Quarto, casa, cama, sala ou ambiente interno doméstico.",
                "floresta": "Floresta, mata, bosque, árvores, selva ou natureza selvagem.",
                "cidade": "Cidade, prédios, ruas, avenida, carros ou ambiente urbano.",
                "praia": "Praia, mar, oceano, areia, ondas, ilha ou litoral.",
                "castelo": "Castelo, reino, palácio, torre medieval ou fortaleza.",
                "espaco": "Espaço sideral, planetas, lua, estrelas, galáxia ou foguete.",
                "parque": "Parque, jardim, praça, campo aberto, gramado ou flores.",
                "sem_mudanca": "A fala não menciona nenhum cenário novo ou lugar específico.",
            },
        },
        "periodo": {
            "type": "choice",
            "instructions": (
                "A fala recente menciona horário do dia ou iluminação do céu? "
                "Escolha sem_mudanca se não houver referência a dia, pôr do sol ou noite."
            ),
            "criteria": {
                "dia": "Manhã, dia claro, sol brilhando ou céu azul.",
                "entardecer": "Pôr do sol, fim de tarde, entardecendo ou céu alaranjado.",
                "noite": "Noite, anoiteceu, escuro, lua, estrelas ou madrugada.",
                "sem_mudanca": "A fala não menciona horário nem iluminação.",
            },
        },
        "protagonista": {
            "type": "choice",
            "instructions": (
                "A fala recente identifica claramente se a criança protagonista é menino ou menina? "
                "Escolha sem_mudanca se não mencionar."
            ),
            "criteria": {
                "menino": "Menino, garoto, filho, príncipe ou pronome masculino para a criança.",
                "menina": "Menina, garota, filha, princesa ou pronome feminino para a criança.",
                "sem_mudanca": "Não identifica ou não muda quem é a criança protagonista.",
            },
        },
        "emocao": {
            "type": "choice",
            "instructions": (
                "Qual emoção da criança protagonista aparece na fala recente? "
                "Escolha sem_mudanca se nenhuma emoção for expressa."
            ),
            "criteria": {
                "neutro": "Calma, tranquila ou explicitamente normal.",
                "feliz": "Alegre, sorrindo, rindo, animada, empolgada ou comemorando.",
                "triste": "Triste, chorando, solitária, cabisbaixa ou chateada.",
                "surpreso": "Surpresa, admirada, maravilhada, espantada ou assustada.",
                "sem_mudanca": "A fala não expressa mudança emocional.",
            },
        },
        "heroi_presente": {
            "type": "noul",
            "instructions": "Considerando o estado atual e a fala recente, há um super-herói ou heroína presente na cena agora?",
            "criteria": {
                "true_description": "Um super-herói chegou, está acompanhando ou já estava presente e não foi embora.",
                "false_description": "Não há super-herói na cena ou ele acabou de ir embora/desaparecer.",
            },
        },
        "animal_presente": {
            "type": "noul",
            "instructions": "Considerando o estado atual e a fala recente, há um cachorrinho, gato ou animal amigo presente na cena agora?",
            "criteria": {
                "true_description": "Um cachorrinho/animal amigo apareceu ou continua na cena.",
                "false_description": "Não há animal amigo na cena ou ele saiu.",
            },
        },
        "dragao_presente": {
            "type": "noul",
            "instructions": "Considerando o estado atual e a fala recente, há um dragão, dinossauro ou criatura mágica presente na cena agora?",
            "criteria": {
                "true_description": "Um dragão, dinossauro ou criatura mágica apareceu ou continua na cena.",
                "false_description": "Não há dragão/criatura mágica ou ele foi embora.",
            },
        },
        "personagens_voando": {
            "type": "noul",
            "instructions": "Considerando o estado atual e a fala recente, os personagens estão voando ou flutuando no ar agora?",
            "criteria": {
                "true_description": "Estão voando, subindo ao céu, nas nuvens ou flutuando.",
                "false_description": "Estão no chão, pousaram, caminhando, sentados ou deitados.",
            },
        },
        "chuva": {
            "type": "noul",
            "instructions": "Considerando o estado atual e a fala recente, está chovendo ou há tempestade na cena agora?",
            "criteria": {
                "true_description": "Começou a chover, está chovendo ou há tempestade.",
                "false_description": "Não está chovendo ou a chuva parou.",
            },
        },
        "neve": {
            "type": "noul",
            "instructions": "Considerando o estado atual e a fala recente, está nevando ou há neve/gelo caindo na cena agora?",
            "criteria": {
                "true_description": "Está nevando, flocos de neve ou cenário de neve ativa.",
                "false_description": "Não está nevando ou parou de nevar.",
            },
        },
        "arco_iris": {
            "type": "noul",
            "instructions": "Considerando o estado atual e a fala recente, há um arco-íris colorido no céu agora?",
            "criteria": {
                "true_description": "Um arco-íris apareceu ou continua visível no céu.",
                "false_description": "Não há arco-íris ou ele desapareceu.",
            },
        },
    }


async def call_gemini_decisions(
    input_blocks: list[dict[str, Any]], api_key: str
) -> dict[str, Any]:
    t0 = time.perf_counter()
    payload = {
        "model": GEMINI_DECISION_MODEL,
        "input": input_blocks,
        "questions": build_gemini_questions(),
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            GEMINI_DECISIONS_URL,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Gemini Decisions API retornou HTTP {resp.status_code}.",
        )

    data = resp.json()
    answers = data.get("answers", {})
    elapsed_ms = round((time.perf_counter() - t0) * 1000)

    choices: dict[str, Any] = {}
    nouls: dict[str, Any] = {}
    for key, ans in answers.items():
        qtype = ans.get("type")
        if qtype == "choice":
            choice_val = ans.get("choice")
            conf = float(ans.get("confidence", 0.0))
            if choice_val == "sem_mudanca":
                choices[key] = {"choice": None, "confidence": 0.0, "keep_current": True}
            else:
                choices[key] = {"choice": choice_val, "confidence": conf}
        elif qtype == "noul":
            nouls[key] = {
                "noul": float(ans.get("noul", 0.5)),
                "confidence": float(ans.get("confidence", 0.5)),
            }

    return {
        "mode": "gemini",
        "model": GEMINI_DECISION_MODEL,
        "server_ms": elapsed_ms,
        "usage": data.get("usage"),
        "choices": choices,
        "nouls": nouls,
    }


def sanitize_audio_mime(raw_mime: str | None) -> str:
    base = (raw_mime or "").split(";")[0].strip().lower()
    if base == "audio/x-m4a" or base == "audio/m4a":
        return "audio/mp4"
    if base == "audio/x-wav":
        return "audio/wav"
    if base == "audio/mp3":
        return "audio/mpeg"
    if base in ALLOWED_AUDIO_MIME_TYPES:
        return base
    raise HTTPException(
        status_code=400,
        detail=f"Formato de áudio não suportado: {base or 'vazio'}.",
    )


@app.get("/")
@app.get("/index.html")
async def serve_index() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


@app.get("/manifest.json")
async def serve_manifest() -> FileResponse:
    return FileResponse(
        BASE_DIR / "manifest.json", media_type="application/manifest+json"
    )


@app.get("/icon.svg")
async def serve_icon() -> FileResponse:
    return FileResponse(BASE_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    has_gemini = bool(os.getenv("GEMINI_API_KEY"))
    has_jev = bool(os.getenv("TYPESAFE_API_KEY"))
    provider = "gemini" if has_gemini else ("jev" if has_jev else "simulator")
    return {
        "ok": True,
        "provider": provider,
        "gemini_configured": has_gemini,
        "jev_configured": has_jev,
        "audio_supported": has_gemini,
    }


@app.post("/api/evaluate")
async def evaluate_story(req: EvaluateRequest) -> dict[str, Any]:
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        blocks = [
            {
                "type": "text",
                "text": f"Estado atual da história (JSON): {json.dumps(req.estado_atual, ensure_ascii=False)}\n",
            },
            {
                "type": "text",
                "text": f"Fala recente do narrador (português do Brasil): {req.fala_recente}",
            },
        ]
        return await call_gemini_decisions(blocks, gemini_key)

    raise HTTPException(
        status_code=503,
        detail="GEMINI_API_KEY não configurada nas variáveis de ambiente do servidor.",
    )


@app.post("/api/evaluate-audio")
async def evaluate_story_audio(
    audio: UploadFile = File(...),
    estado_atual: str = Form(default="{}"),
) -> dict[str, Any]:
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY não configurada — envio direto de áudio requer Gemini Decision 1.",
        )

    mime_type = sanitize_audio_mime(audio.content_type)
    audio_bytes = await audio.read(MAX_AUDIO_BYTES + 1)
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Arquivo de áudio vazio.")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Trecho de áudio excede 5 MB.")

    try:
        parsed_state = json.loads(estado_atual)
        if not isinstance(parsed_state, dict):
            parsed_state = {}
    except json.JSONDecodeError:
        parsed_state = {}

    b64_audio = base64.b64encode(audio_bytes).decode("ascii")
    blocks = [
        {
            "type": "text",
            "text": (
                f"Estado atual da história (JSON): {json.dumps(parsed_state, ensure_ascii=False)}\n"
                "Ouça o trecho de narração infantil em português do Brasil a seguir e responda às perguntas sobre o estado atual da cena.\n"
            ),
        },
        {
            "type": "audio",
            "data": b64_audio,
            "mime_type": mime_type,
        },
    ]
    return await call_gemini_decisions(blocks, gemini_key)
