# História ao Vivo — MVP (Voz + Jev System One)

Protótipo navegável de um aplicativo de histórias infantis em tempo real por voz/texto, onde um modelo **System One** (**Jev**, da TypeSafe AI) classifica o estado da cena (`Choice` + `Noul`) e um palco ilustrado em camadas SVG reage instantaneamente.

## Arquivos

- `index.html` — Frontend PWA autocontido (palco SVG em camadas animadas, captura por microfone via Web Speech API `pt-BR`, simulação de narração palavra por palavra, motor com limiar de confiança `0.70` e histerese `0.30 / 0.75`, e efeitos sonoros sintetizados).
- `manifest.json` — Manifesto PWA para instalar na tela inicial do celular.
- `server.py` — Backend FastAPI que serve o `index.html` e expõe `POST /api/evaluate` conectado ao `AsyncTypeSafeClient(model="jev-latest")`.
- `requirements.txt` — Dependências Python do backend.

## Como testar

### 1. Modo rápido (sem API key — simulador local)
Basta abrir `index.html` no navegador ou servir com:

```bash
python3 -m http.server 8000
```

### 2. Modo completo com Jev real

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TYPESAFE_API_KEY="sua-chave-typesafe"
uvicorn server:app --host 0.0.0.0 --port 8000
```

> **Uso no celular com microfone:** navegadores móveis exigem **HTTPS** para liberar o microfone (`SpeechRecognition`). Ao hospedar em uma VPS (com Nginx/Caddy + Let's Encrypt) ou via túnel HTTPS, abra no navegador do celular e toque em *Adicionar à Tela Inicial*.
