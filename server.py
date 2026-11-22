"""Backend FastAPI do História ao Vivo — 100% Google (Gemini Decision 1 + Cloud Run).

Usa a Gemini Decisions API (gemini-decision-1) para texto (/api/evaluate)
e áudio direto (/api/evaluate-audio).

Melhorias chave:
- Mapeia audio/mp4 -> audio/m4a (enum oficial da Gemini Decisions API) além de suportar audio/wav e audio/webm.
- Usa perguntas Choice de 3 estados (ativar / desativar / sem_mudanca) para todos os elementos,
  evitando que personagens/clima/voo desliguem sozinhos entre uma frase e outra.
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
MAX_AUDIO_BYTES = 5 * 1024 * 1024

# Enum oficial aceito pela Gemini Decisions API para blocos `audio`
GEMINI_AUDIO_MIME_TYPES = {
    "audio/wav",
    "audio/mp3",
    "audio/mpeg",
    "audio/aiff",
    "audio/aac",
    "audio/ogg",
    "audio/flac",
    "audio/m4a",
    "audio/opus",
    "audio/webm",
    "audio/l16",
    "audio/alaw",
    "audio/mulaw",
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


def toggle_question(elemento: str, ativar_desc: str, desativar_desc: str) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": (
            f"O trecho falado menciona alguma mudança sobre {elemento}? "
            "Escolha sem_mudanca se o trecho não falar sobre isso."
        ),
        "criteria": {
            "ativar": ativar_desc,
            "desativar": desativar_desc,
            "sem_mudanca": f"O trecho não menciona {elemento}; manter o estado anterior.",
        },
    }


def build_gemini_questions() -> dict[str, Any]:
    """Perguntas enxutas (~900 tokens) com `sem_mudanca` explícito para estabilidade total."""
    return {
        "cenario": {
            "type": "choice",
            "instructions": "O trecho menciona algum destes lugares/cenários? Se não mencionar lugar, escolha sem_mudanca.",
            "criteria": {
                "quarto": "Quarto, casa, cama, sala ou ambiente interno.",
                "floresta": "Floresta, mata, bosque, árvores ou selva.",
                "cidade": "Cidade, prédios, ruas, avenida ou ambiente urbano.",
                "praia": "Praia, mar, oceano, areia, ondas ou ilha.",
                "castelo": "Castelo, reino, palácio ou torre medieval.",
                "espaco": "Espaço sideral, planetas, lua, foguete ou galáxia.",
                "parque": "Parque, jardim, praça, campo ou flores.",
                "sem_mudanca": "Não menciona lugar ou cenário.",
            },
        },
        "periodo": {
            "type": "choice",
            "instructions": "O trecho menciona horário do dia ou iluminação do céu? Se não mencionar, escolha sem_mudanca.",
            "criteria": {
                "dia": "Manhã, dia claro, sol brilhando ou amanheceu.",
                "entardecer": "Pôr do sol, fim de tarde, entardecendo ou céu alaranjado.",
                "noite": "Noite, anoiteceu, escuro, lua, estrelas ou madrugada.",
                "sem_mudanca": "Não menciona horário nem luz do céu.",
            },
        },
        "protagonista": {
            "type": "choice",
            "instructions": "O trecho identifica se a criança protagonista é menino ou menina? Se não mencionar, escolha sem_mudanca.",
            "criteria": {
                "menino": "Menino, garoto, filho ou príncipe.",
                "menina": "Menina, garota, filha ou princesa.",
                "sem_mudanca": "Não menciona ou não muda o protagonista.",
            },
        },
        "emocao": {
            "type": "choice",
            "instructions": "Qual emoção da criança aparece no trecho? Se não houver emoção explícita, escolha sem_mudanca.",
            "criteria": {
                "neutro": "Calmo, tranquilo ou normal.",
                "feliz": "Feliz, alegre, sorrindo, rindo, empolgado ou animado.",
                "triste": "Triste, chorando, solitário, cabisbaixo ou chateado.",
                "surpreso": "Surpreso, espantado, maravilhado, assustado ou admirado.",
                "sem_mudanca": "Não expressa mudança de emoção.",
            },
        },
        "heroi_presente": toggle_question(
            "super-herói",
            "Um super-herói ou heroína chegou, apareceu ou está atuando na cena.",
            "O super-herói foi embora, sumiu, se despediu ou a criança ficou sozinha sem ele.",
        ),
        "animal_presente": toggle_question(
            "cachorro ou animal amigo",
            "Um cachorrinho, cão, gato ou animal amigo apareceu ou está junto na cena.",
            "O animal amigo foi embora, sumiu ou saiu da cena.",
        ),
        "dragao_presente": toggle_question(
            "dragão ou criatura mágica",
            "Um dragão, dinossauro ou criatura mágica apareceu ou está na cena.",
            "O dragão/criatura foi embora, sumiu ou saiu da cena.",
        ),
        "personagens_voando": toggle_question(
            "voar ou flutuar",
            "Os personagens começaram a voar, continuaram voando, subiram ao céu ou estão flutuando.",
            "Os personagens pousaram, desceram ao chão, aterrissaram, deitaram ou voltaram a andar no chão.",
        ),
        "chuva": toggle_question(
            "chuva ou tempestade",
            "Começou a chover, está chovendo ou há tempestade.",
            "A chuva parou, o tempo limpou ou abriu sol sem chuva.",
        ),
        "neve": toggle_question(
            "neve ou flocos de gelo",
            "Começou a nevar, está nevando ou há flocos de neve caindo.",
            "A neve parou ou o tempo esquentou sem neve.",
        ),
        "arco_iris": toggle_question(
            "arco-íris",
            "Apareceu um arco-íris colorido no céu.",
            "O arco-íris sumiu ou desapareceu.",
        ),
    }


TOGGLE_KEYS = {
    "heroi_presente",
    "animal_presente",
    "dragao_presente",
    "personagens_voando",
    "chuva",
    "neve",
    "arco_iris",
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
        err_msg = ""
        try:
            err_body = resp.json()
            err_msg = err_body.get("error", {}).get("message", "")
        except Exception:
            err_msg = resp.text[:160]
        raise HTTPException(
            status_code=502,
            detail=f"Gemini Decisions API HTTP {resp.status_code}: {err_msg}",
        )

    data = resp.json()
    answers = data.get("answers", {})
    elapsed_ms = round((time.perf_counter() - t0) * 1000)

    choices: dict[str, Any] = {}
    toggles: dict[str, Any] = {}
    for key, ans in answers.items():
        choice_val = ans.get("choice")
        conf = float(ans.get("confidence", 0.0))
        probs = ans.get("probabilities", {})
        if key in TOGGLE_KEYS:
            toggles[key] = {
                "action": choice_val or "sem_mudanca",
                "confidence": conf,
                "p_ativar": float(probs.get("ativar", 0.0)),
                "p_desativar": float(probs.get("desativar", 0.0)),
            }
        else:
            if choice_val == "sem_mudanca" or not choice_val:
                choices[key] = {"choice": None, "confidence": 0.0, "keep_current": True}
            else:
                choices[key] = {"choice": choice_val, "confidence": conf}

    return {
        "mode": "gemini",
        "model": GEMINI_DECISION_MODEL,
        "server_ms": elapsed_ms,
        "usage": data.get("usage"),
        "choices": choices,
        "toggles": toggles,
    }


def sanitize_audio_mime(raw_mime: str | None) -> str:
    base = (raw_mime or "").split(";")[0].strip().lower()
    mapping = {
        "audio/mp4": "audio/m4a",  # Safari/iOS grava MP4/AAC; Gemini enum usa audio/m4a
        "audio/x-m4a": "audio/m4a",
        "audio/x-wav": "audio/wav",
        "audio/wave": "audio/wav",
        "audio/vnd.wave": "audio/wav",
    }
    normalized = mapping.get(base, base)
    if normalized in GEMINI_AUDIO_MIME_TYPES:
        return normalized
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
    if not gemini_key:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY não configurada no servidor.",
        )

    blocks = [
        {
            "type": "text",
            "text": f"Estado atual: {json.dumps(req.estado_atual, ensure_ascii=False)}\n",
        },
        {
            "type": "text",
            "text": f"Trecho falado (pt-BR): {req.fala_recente}",
        },
    ]
    return await call_gemini_decisions(blocks, gemini_key)


@app.post("/api/evaluate-audio")
async def evaluate_story_audio(
    audio: UploadFile = File(...),
    estado_atual: str = Form(default="{}"),
) -> dict[str, Any]:
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY não configurada — áudio direto requer Gemini Decision 1.",
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
                f"Estado atual: {json.dumps(parsed_state, ensure_ascii=False)}\n"
                "Ouça o trecho falado em português do Brasil abaixo e classifique apenas o que mudou:\n"
            ),
        },
        {
            "type": "audio",
            "data": b64_audio,
            "mime_type": mime_type,
        },
    ]
    return await call_gemini_decisions(blocks, gemini_key)
