# História ao Vivo — Voz + Jev (System One)

Aplicativo web/PWA de histórias infantis em tempo real por voz ou texto. Um modelo **System One** (**Jev**, da TypeSafe AI) avalia a fala recente junto do estado anterior da história e transforma um palco ilustrado em camadas instantaneamente.

## O que o palco suporta

- **7 cenários:** `quarto`, `floresta`, `cidade`, `praia`, `castelo`, `espaco`, `parque`
- **3 horários / iluminações:** `dia`, `entardecer`, `noite`
- **Protagonista:** `menino` ou `menina` com **4 emoções** (`neutro`, `feliz`, `triste`, `surpreso`)
- **Personagens e criaturas (`Noul`):** `heroi_presente`, `animal_presente` (cachorrinho), `dragao_presente`
- **Ações e clima (`Noul`):** `personagens_voando`, `chuva`, `neve`, `arco_iris`

## Deploy grátis com HTTPS na Render (recomendado para usar no celular)

1. Acesse https://render.com e faça login com sua conta GitHub.
2. Clique em **New + → Blueprint** e selecione este repositório (`dadariorafaelpereira-tech/historia-ao-vivo`).
3. O arquivo `render.yaml` configura tudo automaticamente e pede apenas a variável **`TYPESAFE_API_KEY`**.
4. Cole sua chave da TypeSafe direto no painel da Render e clique em **Apply**.
5. Abra o link `https://historia-ao-vivo.onrender.com` (ou o subdomínio gerado) no navegador do celular!

> **Segurança:** nunca faça commit de arquivos `.env` ou chaves no GitHub. Coloque `TYPESAFE_API_KEY` apenas nas *Environment Variables* da Render ou em um `.env` local na sua VPS.

## Rodar localmente ou em VPS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TYPESAFE_API_KEY="sua-chave-typesafe"
uvicorn server:app --host 0.0.0.0 --port 8000
```

Ou via Docker:

```bash
docker build -t historia-ao-vivo .
docker run -p 8000:8000 -e TYPESAFE_API_KEY="sua-chave-typesafe" historia-ao-vivo
```
