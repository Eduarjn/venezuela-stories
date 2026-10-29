# Venezuela Stories

Gerador local de 1 vídeo vertical por dia com histórias reais, curiosidades e
fatos históricos da Venezuela (TikTok, Reels, YouTube Shorts).

## Estrutura

```
venezuela-stories/
├── database.py            # Parte 1 – histórico de temas (SQLite)
├── story_engine.py        # Parte 2 – roteiro + prompts de cena (JSON)        [a fazer]
├── visual_generator.py    # Parte 3 – imagens (Gemini) + clipes animados        [a fazer]
├── voice_engine.py        # Parte 4 – narração + timestamps por palavra         [a fazer]
├── video_editor.py        # Parte 5 – montagem final (FFmpeg/MoviePy)           [a fazer]
├── daily_scheduler.py     # Parte 6 – execução diária automática                [a fazer]
├── data/                  # stories.db (não vai para o Git)
├── output/AAAA-MM-DD/     # roteiro, imagens, áudio e vídeo final de cada dia
├── assets/music|sfx|fonts # trilhas, efeitos e fontes licenciados (locais)
├── logs/
└── tests/
```

## Instalação

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # e preencha as chaves
python database.py init
```

O FFmpeg precisa estar instalado no sistema (`ffmpeg -version`).

## Regras do banco (`database.py`)

| Regra | Como é garantida |
|---|---|
| Nenhum tema repete | Bloqueia o mesmo título, o mesmo assunto principal (`entity`) e títulos ≥ 85% parecidos |
| 1 vídeo por dia | Cada data só aceita um tema ativo |
| Fato só vai ao ar com fonte | Trilha `fact` só passa para `verified` com 2+ fontes de domínios diferentes (pt.wikipedia e es.wikipedia contam como uma só) |
| Falha técnica não queima o tema | `failed` libera o dia e permite tentar o mesmo tema depois |
| Fato não confirmado é descartado | `rejected` bloqueia o tema para sempre |

Fluxo de status: `reserved → scripted → verified → rendered → published`.

```bash
python database.py list                                   # histórico
python database.py check "Relâmpago do Catatumbo"         # já foi usado?
python -m unittest -v tests/test_database.py              # testes
```
