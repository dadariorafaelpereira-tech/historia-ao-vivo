"""Backend FastAPI do História ao Vivo — TypeSafe Jev (jev-latest) + Gemini Decision 1.

Suporta dois motores System One intercambiáveis em tempo real:
- `jev`: TypeSafe AI (`jev-latest` em https://api.typesafe.ai/v1/systemone).
  Como o Jev aceita texto/JSON (e não áudio bruto), em `/api/evaluate-audio`
  o trecho WAV é transcrito primeiro pelo Gemini 3.5 Flash-Lite (`GEMINI_API_KEY`)
  e a transcrição é avaliada imediatamente pelo Jev (`TYPESAFE_API_KEY`).
- `gemini`: Google Gemini Decisions API (`gemini-decision-1`), que avalia texto
  e áudio direto em uma única chamada.

Quando `provider="auto"` (padrão), usa Jev automaticamente se `TYPESAFE_API_KEY`
estiver configurada; caso contrário usa Gemini Decision 1.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import io
import json
import math
import os
import re
import struct
import time
import wave
from pathlib import Path
from typing import Any, Literal

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
GEMINI_STT_MODEL = os.getenv("GEMINI_STT_MODEL", "gemini-3.5-flash-lite")
TYPESAFE_SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = os.getenv("JEV_MODEL", "jev-latest")
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MIN_WAV_RMS = 0.004  # Abaixo disso é silêncio digital/ambiente; pula STT e modelo (<1 ms)
MAX_REF_IMAGE_B64 = 1_200_000  # ~900 KB decodificado

IMAGE_MODELS: dict[str, str] = {
    "nb2-lite": "gemini-3.1-flash-lite-image",
    "nb21": "gemini-nano-banana-2.1",
}

IMAGE_STYLES: dict[str, str] = {
    "livro": (
        "Ilustração encantadora de livro infantil moderno, cores vibrantes, "
        "iluminação mágica e expressões acolhedoras, acabamento cinematográfico."
    ),
    "aquarela": (
        "Ilustração delicada em aquarela artesanal de livro de histórias infantis, "
        "pinceladas suaves, luz quente e atmosfera poética."
    ),
    "3d": (
        "Animação 3D estilizada de filme infantil (estilo Pixar/Disney), "
        "iluminação volumétrica suave, personagens muito expressivos e cenário detalhado."
    ),
    "anime": (
        "Ilustração estilo Studio Ghibli de conto infantil mágico, "
        "céu luminoso, traço delicado e paisagem cheia de vida."
    ),
}

ALLOWED_REF_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}

ProviderOption = Literal["auto", "gemini", "jev"]
ImageModelOption = Literal["nb2-lite", "nb21"]
ImageStyleOption = Literal["livro", "aquarela", "3d", "anime"]

# Enum oficial aceito pela Gemini API para blocos `audio` / `inline_data`
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

_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=20.0,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )
    try:
        yield
    finally:
        await _http_client.aclose()
        _http_client = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=20.0,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _http_client


app = FastAPI(
    title="História ao Vivo — Jev + Gemini + Nano Banana Backend",
    lifespan=lifespan,
)

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
    provider: ProviderOption = "auto"


class GenerateImageRequest(BaseModel):
    fala_atual: str = Field(..., min_length=1, max_length=1200)
    historico: list[str] = Field(default_factory=list, max_length=8)
    estado_atual: dict[str, Any] = Field(default_factory=dict)
    image_model: ImageModelOption = "nb2-lite"
    estilo: ImageStyleOption = "livro"
    reference_image_b64: str | None = Field(default=None, max_length=MAX_REF_IMAGE_B64)
    reference_mime_type: str | None = Field(default="image/jpeg", max_length=40)


def toggle_question(elemento: str, ativar_desc: str, desativar_desc: str) -> dict[str, Any]:
    return {
        "type": "choice",
        "instructions": (
            f"O trecho falado cita {elemento} (presente/acontecendo ou saindo/parando)? "
            "Se não citar, escolha sem_mudanca."
        ),
        "criteria": {
            "ativar": ativar_desc,
            "desativar": desativar_desc,
            "sem_mudanca": f"Não cita {elemento}; manter estado anterior.",
        },
    }


def build_questions() -> dict[str, Any]:
    """Perguntas Choice enxutas com `sem_mudanca` explícito (compatível com Jev e Gemini)."""
    return {
        "nova_cena": {
            "type": "choice",
            "instructions": (
                "O trecho falado descreve algo visual para ilustrar na história "
                "(qualquer personagem como menino, menina, papai, mamãe, animal, dragão; "
                "qualquer lugar como lua, foguete, casa, praia, floresta; ação, objeto ou clima)?"
            ),
            "criteria": {
                "sim": "Tem conteúdo visual, personagem, lugar, objeto, destino ou ação para desenhar.",
                "nao": "Apenas ruído, hesitação, saudação ou frase vazia sem nada visual novo.",
            },
        },
        "cenario": {
            "type": "choice",
            "instructions": "O trecho menciona algum destes cenários? Se não mencionar lugar, escolha sem_mudanca.",
            "criteria": {
                "quarto": "Quarto, casa, cama, sala ou ambiente interno.",
                "floresta": "Floresta, mata, bosque, árvores ou selva.",
                "cidade": "Cidade, prédios, ruas, avenida ou ambiente urbano.",
                "praia": "Praia, mar, oceano, areia, ondas ou ilha.",
                "castelo": "Castelo, reino, palácio ou torre medieval.",
                "espaco": "Espaço sideral, planetas, lua, foguete ou galáxia.",
                "parque": "Parque, jardim, praça, campo ou flores.",
                "sem_mudanca": "Não menciona cenário.",
            },
        },
        "periodo": {
            "type": "choice",
            "instructions": "O trecho menciona horário do dia ou luz do céu? Se não mencionar, escolha sem_mudanca.",
            "criteria": {
                "dia": "Manhã, dia claro, sol brilhando ou amanheceu.",
                "entardecer": "Pôr do sol, fim de tarde, entardecendo ou céu alaranjado.",
                "noite": "Noite, anoiteceu, escuro, lua, estrelas ou madrugada.",
                "sem_mudanca": "Não menciona horário.",
            },
        },
        "protagonista": {
            "type": "choice",
            "instructions": "O trecho nomeia explicitamente se a criança é menino ou menina? Se não, escolha sem_mudanca.",
            "criteria": {
                "menino": "Diz menino, garoto, filho ou príncipe.",
                "menina": "Diz menina, garota, filha ou princesa.",
                "sem_mudanca": "Não diz menino nem menina.",
            },
        },
        "emocao": {
            "type": "choice",
            "instructions": "Qual emoção da criança aparece no trecho? Se nenhuma, escolha sem_mudanca.",
            "criteria": {
                "neutro": "Calmo, tranquilo ou normal.",
                "feliz": "Feliz, alegre, sorrindo, rindo, empolgado ou animado.",
                "triste": "Triste, chorando, solitário, cabisbaixo ou chateado.",
                "surpreso": "Surpreso, espantado, maravilhado, assustado ou admirado.",
                "sem_mudanca": "Não expressa emoção.",
            },
        },
        "heroi_presente": toggle_question(
            "super-herói",
            "Super-herói ou heroína chegou, apareceu ou está na cena.",
            "Super-herói foi embora, sumiu ou saiu da cena.",
        ),
        "animal_presente": toggle_question(
            "cachorrinho/animal",
            "Cachorrinho, cão, gato ou animal amigo apareceu ou está na cena.",
            "Animal amigo foi embora, sumiu ou saiu da cena.",
        ),
        "dragao_presente": toggle_question(
            "dragão/criatura",
            "Dragão, dinossauro ou criatura mágica apareceu ou está na cena.",
            "Dragão/criatura foi embora, sumiu ou saiu da cena.",
        ),
        "personagens_voando": toggle_question(
            "voar/flutuar",
            "Estão voando, começaram a voar, subiram ao céu ou flutuando.",
            "Pousaram, desceram ao chão, aterrissaram ou voltaram a andar no chão.",
        ),
        "chuva": toggle_question(
            "chuva/tempestade",
            "Está chovendo, começou a chover ou há chuva/tempestade.",
            "A chuva parou, o tempo limpou ou abriu sol sem chuva.",
        ),
        "neve": toggle_question(
            "neve/gelo",
            "Está nevando, começou a nevar ou há flocos de neve caindo.",
            "A neve parou ou o tempo esquentou sem neve.",
        ),
        "arco_iris": toggle_question(
            "arco-íris",
            "Há ou apareceu um arco-íris no céu.",
            "O arco-íris sumiu ou desapareceu.",
        ),
    }


CHOICE_KEYS = ("cenario", "periodo", "protagonista", "emocao")
TOGGLE_KEYS = {
    "heroi_presente",
    "animal_presente",
    "dragao_presente",
    "personagens_voando",
    "chuva",
    "neve",
    "arco_iris",
}


def extract_error_message(resp: httpx.Response) -> str:
    try:
        err_body = resp.json()
        if isinstance(err_body, dict):
            err = err_body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])[:220]
            if isinstance(err, str) and err:
                return err[:220]
            if err_body.get("detail"):
                return str(err_body["detail"])[:220]
            if err_body.get("message"):
                return str(err_body["message"])[:220]
    except Exception:
        pass
    return resp.text[:180]


def parse_answers(
    answers: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    choices: dict[str, Any] = {}
    toggles: dict[str, Any] = {}
    scene_change: dict[str, Any] = {"should_generate": True, "confidence": 0.5}
    for key, ans in answers.items():
        if not isinstance(ans, dict):
            continue
        choice_val = ans.get("choice")
        conf = float(ans.get("confidence", 0.0))
        probs = ans.get("probabilities", {}) or {}
        if key == "nova_cena":
            p_sim = float(probs.get("sim", conf if choice_val == "sim" else 1.0 - conf))
            scene_change = {
                "should_generate": choice_val == "sim" or p_sim >= 0.55,
                "confidence": conf,
                "p_sim": round(p_sim, 3),
            }
        elif key in TOGGLE_KEYS:
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
    return choices, toggles, scene_change


def build_no_change_result(mode: str, model: str, stt_ms: int = 0) -> dict[str, Any]:
    return {
        "mode": mode,
        "model": model,
        "server_ms": 0,
        "stt_ms": stt_ms,
        "stt_model": GEMINI_STT_MODEL,
        "transcript": "",
        "skipped": True,
        "usage": None,
        "choices": {
            key: {"choice": None, "confidence": 0.0, "keep_current": True}
            for key in CHOICE_KEYS
        },
        "toggles": {
            key: {"action": "sem_mudanca", "confidence": 1.0, "p_ativar": 0.0, "p_desativar": 0.0}
            for key in TOGGLE_KEYS
        },
    }


def resolve_default_provider() -> str:
    has_jev = bool(os.getenv("TYPESAFE_API_KEY"))
    has_gemini = bool(os.getenv("GEMINI_API_KEY"))
    env_default = os.getenv("DEFAULT_PROVIDER", "auto").strip().lower()
    if env_default == "gemini" and has_gemini:
        return "gemini"
    if env_default == "jev" and has_jev:
        return "jev"
    if has_jev:
        return "jev"
    if has_gemini:
        return "gemini"
    return "simulator"


def resolve_provider(requested: str) -> str:
    req = (requested or "auto").strip().lower()
    if req not in {"auto", "gemini", "jev"}:
        raise HTTPException(
            status_code=400,
            detail="Parâmetro provider inválido. Use 'auto', 'jev' ou 'gemini'.",
        )
    if req == "auto":
        resolved = resolve_default_provider()
        if resolved == "simulator":
            raise HTTPException(
                status_code=503,
                detail="Nenhuma chave de API configurada (TYPESAFE_API_KEY ou GEMINI_API_KEY).",
            )
        return resolved
    return req


async def call_gemini_decisions(
    input_blocks: list[dict[str, Any]], api_key: str
) -> dict[str, Any]:
    t0 = time.perf_counter()
    payload = {
        "model": GEMINI_DECISION_MODEL,
        "input": input_blocks,
        "questions": build_questions(),
    }
    client = get_http_client()
    try:
        resp = await client.post(
            GEMINI_DECISIONS_URL,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha de rede na Gemini Decisions API: {exc.__class__.__name__}",
        ) from exc
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Gemini Decisions API HTTP {resp.status_code}: {extract_error_message(resp)}",
        )

    data = resp.json()
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    choices, toggles, scene_change = parse_answers(data.get("answers", {}))

    return {
        "mode": "gemini",
        "model": data.get("model") or GEMINI_DECISION_MODEL,
        "server_ms": elapsed_ms,
        "usage": data.get("usage"),
        "choices": choices,
        "toggles": toggles,
        "scene_visual_change": scene_change,
    }


async def call_jev_system_one(
    fala_recente: str, estado_atual: dict[str, Any], api_key: str
) -> dict[str, Any]:
    t0 = time.perf_counter()
    payload = {
        "model": JEV_MODEL,
        "state": {
            "estado_atual": estado_atual,
            "trecho_falado_pt_br": fala_recente,
        },
        "questions": build_questions(),
    }
    client = get_http_client()
    try:
        resp = await client.post(
            TYPESAFE_SYSTEM_ONE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha de rede na TypeSafe Jev API: {exc.__class__.__name__}",
        ) from exc
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"TypeSafe Jev API HTTP {resp.status_code}: {extract_error_message(resp)}",
        )

    data = resp.json()
    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    choices, toggles, scene_change = parse_answers(data.get("answers", {}))

    return {
        "mode": "jev",
        "model": data.get("model") or JEV_MODEL,
        "server_ms": elapsed_ms,
        "usage": data.get("usage"),
        "choices": choices,
        "toggles": toggles,
        "scene_visual_change": scene_change,
    }


async def call_nano_banana_image(
    req: GenerateImageRequest, api_key: str
) -> dict[str, Any]:
    model_id = IMAGE_MODELS[req.image_model]
    style_desc = IMAGE_STYLES[req.estilo]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"

    history_clean = [h.strip() for h in req.historico[-5:] if isinstance(h, str) and h.strip()]
    context_block = ""
    if history_clean:
        context_block = (
            "Contexto anterior da história (mantenha os personagens e elementos que ainda fazem sentido na cena):\n- "
            + "\n- ".join(history_clean)
            + "\n\n"
        )

    use_ref = bool(
        req.image_model == "nb21"
        and req.reference_image_b64
        and req.reference_mime_type in ALLOWED_REF_IMAGE_MIMES
    )
    ref_instruction = (
        "Use a imagem de referência anexada para manter exatamente a mesma aparência, "
        "rosto e estilo visual dos personagens já apresentados, mudando a cena e ação conforme o novo momento da história.\n"
        if use_ref
        else ""
    )

    prompt = (
        f"{style_desc}\n"
        f"{ref_instruction}"
        f"{context_block}"
        f"Cena atual a ilustrar agora (em destaque principal):\n{req.fala_atual.strip()}\n\n"
        "Regras visuais obrigatórias:\n"
        "- Enquadramento amplo cinematográfico 16:9, adequado para crianças.\n"
        "- Mostre claramente TODOS os personagens, lugares e objetos citados na cena atual e no contexto ativo.\n"
        "- NÃO escreva nenhum texto, letra, legenda, placa escrita ou balão de fala na imagem.\n"
    )

    parts: list[dict[str, Any]] = []
    if use_ref and req.reference_image_b64:
        parts.append(
            {
                "inlineData": {
                    "mimeType": req.reference_mime_type,
                    "data": req.reference_image_b64,
                }
            }
        )
    parts.append({"text": prompt})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {
                "aspectRatio": "16:9",
                "imageSize": "1K",
            },
        },
    }

    t0 = time.perf_counter()
    client = get_http_client()
    try:
        resp = await client.post(
            url,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=45.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha de rede no {model_id}: {exc.__class__.__name__}",
        ) from exc

    if resp.status_code >= 400:
        msg = extract_error_message(resp)
        if resp.status_code in (403, 429):
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"{model_id} indisponível (HTTP {resp.status_code}): {msg}",
            )
        raise HTTPException(
            status_code=502,
            detail=f"{model_id} HTTP {resp.status_code}: {msg}",
        )

    data = resp.json()
    image_ms = round((time.perf_counter() - t0) * 1000)

    img_b64: str | None = None
    img_mime = "image/png"
    for cand in data.get("candidates", []):
        content = cand.get("content") or {}
        for part in content.get("parts", []):
            if part.get("thought") is True:
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                img_b64 = inline["data"]
                img_mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"

    if not img_b64:
        raise HTTPException(
            status_code=502,
            detail=f"{model_id} não retornou imagem para esta frase.",
        )

    return {
        "ok": True,
        "image_model": req.image_model,
        "model_id": model_id,
        "mime_type": img_mime,
        "image_b64": img_b64,
        "image_ms": image_ms,
        "used_reference": use_ref,
    }


def wav_is_silent(audio_bytes: bytes, mime_type: str) -> bool:
    """Detecta silêncio em WAV 16-bit PCM em <1 ms para evitar alucinação de STT e chamadas inúteis."""
    if mime_type != "audio/wav":
        return False
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            if wf.getsampwidth() != 2:
                return False
            frames = wf.readframes(wf.getnframes())
    except Exception:
        return False
    n_samples = len(frames) // 2
    if n_samples == 0:
        return True
    # Amostra a cada 4 samples para ser instantâneo (~0.2 ms em 4 s de áudio 16 kHz)
    step = 4
    sq_sum = 0.0
    count = 0
    for i in range(0, n_samples, step):
        val = struct.unpack_from("<h", frames, i * 2)[0] / 32768.0
        sq_sum += val * val
        count += 1
    rms = math.sqrt(sq_sum / max(1, count))
    return rms < MIN_WAV_RMS


async def transcribe_audio_with_gemini(
    b64_audio: str, mime_type: str, api_key: str
) -> tuple[str, int]:
    t0 = time.perf_counter()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_STT_MODEL}:generateContent"
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "Transcreva exatamente em português do Brasil o que foi falado neste áudio de história infantil. "
                            "Retorne APENAS as palavras faladas, sem aspas nem comentários. "
                            "Se houver apenas silêncio, ruído ou nenhuma palavra inteligível, responda exatamente: SILENCIO"
                        )
                    },
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": b64_audio,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 120,
        },
    }
    client = get_http_client()
    try:
        resp = await client.post(
            url,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha de rede no Gemini STT ({GEMINI_STT_MODEL}): {exc.__class__.__name__}",
        ) from exc
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Gemini STT ({GEMINI_STT_MODEL}) HTTP {resp.status_code}: {extract_error_message(resp)}",
        )

    data = resp.json()
    stt_ms = round((time.perf_counter() - t0) * 1000)
    text_parts: list[str] = []
    for cand in data.get("candidates", []):
        content = cand.get("content") or {}
        for part in content.get("parts", []):
            txt = part.get("text")
            if isinstance(txt, str) and txt.strip():
                text_parts.append(txt.strip())

    transcript = " ".join(text_parts).strip().strip('"').strip("'").strip()
    cleaned = re.sub(r"[\[\(][^\]\)]{1,32}[\]\)]", "", transcript).strip()
    upper_clean = cleaned.upper().strip(".!?,;:")
    if (
        not cleaned
        or upper_clean in {"SILENCIO", "SILÊNCIO", "VAZIO", "NADA", "RUÍDO", "RUIDO"}
        or not re.search(r"[A-Za-zÀ-ÿ0-9]", cleaned)
    ):
        transcript = ""
    else:
        transcript = cleaned
    return transcript, stt_ms


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
    provider = resolve_default_provider()
    return {
        "ok": True,
        "provider": provider,
        "default_provider": provider,
        "gemini_configured": has_gemini,
        "jev_configured": has_jev,
        "audio_supported": has_gemini,
        "jev_audio_supported": has_gemini and has_jev,
        "image_supported": has_gemini,
        "image_models": IMAGE_MODELS,
        "stt_model": GEMINI_STT_MODEL,
    }


@app.post("/api/generate-image")
async def generate_scene_image(req: GenerateImageRequest) -> dict[str, Any]:
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY não configurada no servidor — geração de imagens Nano Banana requer GEMINI_API_KEY.",
        )
    return await call_nano_banana_image(req, gemini_key)


@app.post("/api/evaluate")
async def evaluate_story(req: EvaluateRequest) -> dict[str, Any]:
    provider = resolve_provider(req.provider)

    if provider == "jev":
        jev_key = os.getenv("TYPESAFE_API_KEY")
        if not jev_key:
            raise HTTPException(
                status_code=503,
                detail="TYPESAFE_API_KEY não configurada no Cloud Run.",
            )
        return await call_jev_system_one(req.fala_recente, req.estado_atual, jev_key)

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
    provider: str = Form(default="auto"),
    need_transcript: str = Form(default="false"),
) -> dict[str, Any]:
    selected_provider = resolve_provider(provider)
    gemini_key = os.getenv("GEMINI_API_KEY")
    jev_key = os.getenv("TYPESAFE_API_KEY")

    if selected_provider == "jev":
        if not jev_key:
            raise HTTPException(
                status_code=503,
                detail="TYPESAFE_API_KEY não configurada no Cloud Run.",
            )
        if not gemini_key:
            raise HTTPException(
                status_code=503,
                detail="GEMINI_API_KEY não configurada para transcrever o áudio antes de enviar ao Jev.",
            )
    elif not gemini_key:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY não configurada — áudio direto requer Gemini.",
        )

    mime_type = sanitize_audio_mime(audio.content_type)
    audio_bytes = await audio.read(MAX_AUDIO_BYTES + 1)
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Arquivo de áudio vazio.")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Trecho de áudio excede 5 MB.")

    if wav_is_silent(audio_bytes, mime_type):
        return build_no_change_result(
            selected_provider,
            JEV_MODEL if selected_provider == "jev" else GEMINI_DECISION_MODEL,
            stt_ms=0,
        )

    try:
        parsed_state = json.loads(estado_atual)
        if not isinstance(parsed_state, dict):
            parsed_state = {}
    except json.JSONDecodeError:
        parsed_state = {}

    b64_audio = base64.b64encode(audio_bytes).decode("ascii")

    if selected_provider == "jev":
        transcript, stt_ms = await transcribe_audio_with_gemini(
            b64_audio, mime_type, gemini_key
        )
        if not transcript:
            return build_no_change_result("jev", JEV_MODEL, stt_ms=stt_ms)

        result = await call_jev_system_one(transcript, parsed_state, jev_key)
        result["transcript"] = transcript
        result["stt_ms"] = stt_ms
        result["stt_model"] = GEMINI_STT_MODEL
        return result

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
    if need_transcript.strip().lower() in {"1", "true", "yes"}:
        result, (transcript, stt_ms) = await asyncio.gather(
            call_gemini_decisions(blocks, gemini_key),
            transcribe_audio_with_gemini(b64_audio, mime_type, gemini_key),
        )
        if not transcript:
            return build_no_change_result("gemini", GEMINI_DECISION_MODEL, stt_ms=stt_ms)
        result["transcript"] = transcript
        result["stt_ms"] = stt_ms
        result["stt_model"] = GEMINI_STT_MODEL
        return result

    return await call_gemini_decisions(blocks, gemini_key)

