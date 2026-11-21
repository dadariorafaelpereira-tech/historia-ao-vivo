"""Backend FastAPI do MVP História ao Vivo com TypeSafe Jev (System One).

Uso:
    export TYPESAFE_API_KEY="sua-chave-aqui"
    uvicorn server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="História ao Vivo — Jev Backend")


class EvaluateRequest(BaseModel):
    fala_recente: str = Field(..., min_length=1, max_length=2000)
    estado_atual: dict[str, Any] = Field(default_factory=dict)


@app.get("/")
@app.get("/index.html")
async def serve_index() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


@app.get("/manifest.json")
async def serve_manifest() -> FileResponse:
    return FileResponse(BASE_DIR / "manifest.json", media_type="application/manifest+json")


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
            detail="TYPESAFE_API_KEY não configurada no ambiente do servidor.",
        )

    try:
        from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="Pacote typesafe-sdk não instalado. Rode: pip install -r requirements.txt",
        ) from exc

    async with AsyncTypeSafeClient(model="jev-latest") as client:
        res = await client.system_one(
            state={
                "estado_atual": req.estado_atual,
                "fala_recente": req.fala_recente,
            },
            questions={
                "cenario": Choice(
                    instructions="Qual o ambiente atual da história?",
                    criteria={
                        "floresta": "Mata, árvores, natureza ou bosque",
                        "cidade": "Prédios, ruas, ambiente urbano",
                        "quarto": "Casa, cama ou ambiente interno",
                    },
                ),
                "periodo": Choice(
                    instructions="Qual o horário ou iluminação da cena?",
                    criteria={
                        "dia": "Claro, manhã ou sol",
                        "noite": "Escuro, estrelas, lua ou anoiteceu",
                    },
                ),
                "emocao_menino": Choice(
                    instructions="Qual o estado emocional atual do menino?",
                    criteria={
                        "triste": "Chorando, cabisbaixo ou solitário",
                        "feliz": "Alegre, empolgado ou sorrindo",
                        "neutro": "Sem emoção explícita",
                    },
                ),
                "heroi_presente": Noul(
                    instructions="Há um super-herói presente na cena?"
                ),
                "personagens_voando": Noul(
                    instructions="Os personagens estão voando agora?"
                ),
                "chuva": Noul(
                    instructions="Está chovendo ou há tempestade na cena?"
                ),
            },
        )

    return {
        "mode": "jev",
        "model": "jev-latest",
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
