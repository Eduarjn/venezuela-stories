# Venezuela Stories

Gerador local de 1 vídeo vertical por dia com histórias reais, curiosidades e
fatos históricos da Venezuela (TikTok, Reels, YouTube Shorts).

## Estrutura

```
venezuela-stories/
├── database.py            # Parte 1 – histórico de temas (SQLite)
├── story_engine.py        # Parte 2 – roteiro + prompts de cena (JSON)
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
python -m unittest discover -s tests -v                   # testes
```

## Gerar o roteiro do dia (`story_engine.py`)

```bash
python story_engine.py                                     # hoje, primeiro idioma de VIDEO_LANGUAGES
python story_engine.py --date 2026-11-05 --language es-419
```

Saída: `output/AAAA-MM-DD/current_episode.json` com `scenes` (`scene_id`, `narration_text`,
`image_prompt`, `motion_type`, `sfx_cue`), a pergunta em aberto, a cena do clímax, as fontes
conferidas e o texto da publicação.

Antes de salvar, o episódio passa por:

| Checagem | Se falhar |
|---|---|
| JSON válido e no schema | o erro volta para o modelo, que corrige (até 4 tentativas) |
| Hook ≤ 10 palavras, sem "Hoje vou falar…", "Neste vídeo…", "Você sabia…" | idem |
| 12–22 cenas de 3–16 palavras (3–5 s), duração total 35–90 s | idem |
| Pergunta lançada nas cenas 1–3 e respondida nos últimos 40% | idem |
| Tema inédito no banco | o modelo escolhe outro tema |
| Trilha `fact`: cada fonte é aberta e o trecho citado precisa estar na página; 2+ domínios | o modelo pesquisa de novo ou retira o dado |
| Trilha `fiction`: rótulo de lenda/ficção nas 4 primeiras cenas | o modelo corrige |

Só depois disso o tema é gravado no banco como `verified`.
