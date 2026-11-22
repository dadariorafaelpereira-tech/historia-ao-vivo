# História ao Vivo — 100% Google (Gemini Decision 1 + Cloud Run)

Aplicativo web/PWA de histórias infantis em tempo real por voz ou texto usando **100% ferramentas Google**:

- **IA de decisão multimodal:** [`gemini-decision-1`](https://aistudio.google.com/docs/models/gemini-decision-1) via **Gemini Decisions API** — avalia **áudio direto do microfone** (funciona no iPhone Safari e Android Chrome!) ou texto, retornando `choice` + `noul` calibrados com zero tokens de saída.
- **Hospedagem com HTTPS:** **Google Cloud Run** (conecta direto ao GitHub usando o `Dockerfile` deste repositório).

## O que o palco suporta

- **7 cenários:** `quarto`, `floresta`, `cidade`, `praia`, `castelo`, `espaco`, `parque`
- **3 horários / iluminações:** `dia`, `entardecer`, `noite`
- **Protagonista:** `menino` ou `menina` com **4 emoções** (`neutro`, `feliz`, `triste`, `surpreso`)
- **Extras (`noul` com histerese):** super-herói, cachorrinho, dragão, voo, chuva, neve e arco-íris

## Deploy 100% Google no Cloud Run (com HTTPS para o celular)

1. **Crie sua chave gratuita no Google AI Studio:** acesse https://aistudio.google.com/apikey e gere uma **`GEMINI_API_KEY`** *(o `gemini-decision-1` tem free tier de 200 req/min e 10.000 req/dia no AI Studio)*.
2. **Abra o Google Cloud Run:** acesse https://console.cloud.google.com/run e clique em **Connect repo** (ou **Deploy container → Continuously deploy from a repository**).
3. Conecte sua conta GitHub e selecione o repositório **`dadariorafaelpereira-tech/historia-ao-vivo`** (branch `main`, tipo de build: **Dockerfile**).
4. Em **Authentication**, marque **Allow public access** (para abrir no celular sem login do GCP).
5. Em **Container(s) → Variables & Secrets**, adicione a variável:
   - Nome: `GEMINI_API_KEY`
   - Valor: sua chave do Google AI Studio
6. Clique em **Create**. Em ~2 minutos o Cloud Run gera um link `https://historia-ao-vivo-....run.app`.
7. Abra esse link no **Safari do iPhone** (ou Chrome no Android), toque em **🎙️ Falar no microfone** e conte sua história!

> **Segurança:** nunca coloque `GEMINI_API_KEY` em commits, chat ou arquivos públicos. Configure-a apenas nas variáveis de ambiente do Cloud Run (ou `.env` local ignorado pelo `.gitignore`).

## Rodar localmente

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY="sua-chave-do-google-ai-studio"
uvicorn server:app --host 0.0.0.0 --port 8080
```
