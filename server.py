"""Backend FastAPI do MVP História ao Vivo com TypeSafe Jev (System One).

Uso local / VPS:
    export TYPESAFE_API_KEY="sua-chave-aqui"
    uvicorn server:app --host 0.0.0.0 --port 8000

No Render:
    Use o render.yaml e informe TYPESAFE_API_KEY nas Environment Variables.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="História ao Vivo — Jev Backend")

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
async def health() -> dict[str, bool]:
    return {"ok": True, "jev_configured": bool(os.getenv("TYPESAFE_API_KEY"))}


@app.post("/api/evaluate")
async def evaluate_story(req: EvaluateRequest) -> dict[str, Any]:
    api_key = os.getenv("TYPESAFE_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="TYPESAFE_API_KEY não configurada nas variáveis de ambiente do servidor.",
        )

    try:
        from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="Pacote typesafe-sdk não instalado no servidor.",
        ) from exc

    t0 = time.perf_counter()
    try:
        async with AsyncTypeSafeClient(model="jev-latest") as client:
            res = await client.system_one(
                state={
                    "estado_atual": req.estado_atual,
                    "fala_recente": req.fala_recente,
                },
                questions={
                    "cenario": Choice(
                        instructions=(
                            "Qual o cenário/ambiente atual da história? "
                            "Se a fala recente não mencionar mudança de lugar, mantenha o cenário de estado_atual."
                        ),
                        criteria={
                            "quarto": "Quarto, casa, cama, sala ou ambiente interno doméstico",
                            "floresta": "Floresta, mata, bosque, árvores, selva ou natureza selvagem",
                            "cidade": "Cidade, prédios, ruas, avenida, carros ou ambiente urbano",
                            "praia": "Praia, mar, oceano, areia, ondas, ilha ou litoral",
                            "castelo": "Castelo, reino, palácio, torre medieval ou fortaleza",
                            "espaco": "Espaço sideral, planetas, lua, estrelas, galáxia ou foguete",
                            "parque": "Parque, jardim, praça, campo aberto, gramado ou flores",
                        },
                    ),
                    "periodo": Choice(
                        instructions=(
                            "Qual o horário ou iluminação atual da cena? "
                            "Se não houver mudança explícita, mantenha o periodo de estado_atual."
                        ),
                        criteria={
                            "dia": "Manhã, dia claro, sol brilhando ou céu azul",
                            "entardecer": "Pôr do sol, fim de tarde, entardecendo ou céu alaranjado",
                            "noite": "Noite, anoiteceu, escuro, lua, estrelas ou madrugada",
                        },
                    ),
                    "protagonista": Choice(
                        instructions=(
                            "Quem é a criança protagonista principal da história? "
                            "Mantenha o valor de estado_atual se não houver indicação contrária."
                        ),
                        criteria={
                            "menino": "Menino, garoto, filho, príncipe ou pronome masculino",
                            "menina": "Menina, garota, filha, princesa ou pronome feminino",
                        },
                    ),
                    "emocao": Choice(
                        instructions="Qual o estado emocional atual da criança protagonista?",
                        criteria={
                            "neutro": "Calma, observando ou sem emoção forte explícita",
                            "feliz": "Alegre, sorrindo, rindo, animada, empolgada ou comemorando",
                            "triste": "Triste, chorando, solitária, cabisbaixa ou chateada",
                            "surpreso": "Surpresa, admirada, maravilhada, espantada ou assustada",
                        },
                    ),
                    "heroi_presente": Noul(
                        instructions="Há um super-herói ou heroína presente junto na cena atual?"
                    ),
                    "animal_presente": Noul(
                        instructions="Há um cachorrinho, gato ou animal de estimação amigo presente na cena atual?"
                    ),
                    "dragao_presente": Noul(
                        instructions="Há um dragão, dinossauro ou criatura mágica presente na cena atual?"
                    ),
                    "personagens_voando": Noul(
                        instructions="Os personagens estão voando, flutuando ou no céu neste momento?"
                    ),
                    "chuva": Noul(
                        instructions="Está chovendo ou há tempestade na cena atual?"
                    ),
                    "neve": Noul(
                        instructions="Está nevando ou há flocos de neve / frio gelado na cena atual?"
                    ),
                    "arco_iris": Noul(
                        instructions="Há um arco-íris colorido ou brilho mágico no céu da cena atual?"
                    ),
                },
            )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Erro ao consultar a API do Jev: {type(exc).__name__}",
        ) from exc

    elapsed_ms = round((time.perf_counter() - t0) * 1000)

    return {
        "mode": "jev",
        "model": "jev-latest",
        "server_ms": elapsed_ms,
        "choices": {
            key: {
                "choice": ans.choice,
                "confidence": ans.confidence,
            }
            for key, ans in res.choices.items()
        },
        "nouls": {
            key: {
                "noul": ans.noul,
            }
            for key, ans in res.nouls.items()
        },
    }
