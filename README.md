# Venezuela Stories

Gerador local de 1 vídeo vertical por dia com histórias reais, curiosidades e
fatos históricos da Venezuela (TikTok, Reels, YouTube Shorts).

## Estrutura

```
venezuela-stories/
├── database.py            # Parte 1 – histórico de temas (SQLite)
├── story_engine.py        # Parte 2 – roteiro + prompts de cena (JSON)
├── visual_generator.py    # Parte 3 – imagens (Gemini) + clipes animados
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
| Trilha `fact`: 2+ domínios, e pelo menos 1 fonte com o trecho conferido (na página ou na citação da busca). Página que bloqueia o robô (403, Cloudflare, JS) vale se a busca da Anthropic retornou a URL; página que abre sem o trecho é reprovada | o modelo pesquisa de novo ou retira o dado |
| Trilha `fiction`: rótulo de lenda/ficção nas 4 primeiras cenas | o modelo corrige |

Só depois disso o tema é gravado no banco como `verified`.

## Gerar imagens e clipes (`visual_generator.py`)

```bash
python visual_generator.py --list-models     # modelos de imagem da sua conta -> GEMINI_IMAGE_MODEL
python visual_generator.py                   # episódio de hoje
python visual_generator.py --date 2026-11-05
```

Para cada cena de `current_episode.json`:

1. Monta o prompt com o **prefixo fixo de estilo** (mesmo "diretor de arte" em todas as cenas;
   troque em `VISUAL_STYLE_PREFIX` no `.env` se quiser outro visual).
2. Gera a imagem no Gemini em 9:16: 1 tentativa + até 3 novas (espera 2, 4 e 8 s), timeout de
   120 s por chamada; erro 400/401/403/404 para na hora porque é configuração.
3. Ajusta para 1080x1920 → `assets/episodes/{episode_id}/visuals/scene_XX.png`
   (`episode_id` = `topic_id` do banco).
4. Anima com FFmpeg → `visuals/scene_XX.mp4` (30 fps, H.264), com vinheta que "respira",
   tremulação leve de projetor antigo e granulação de filme:

| motion_type | Efeito |
|---|---|
| `zoom_in_fast` | zoom rápido até 1,25x nos primeiros 0,5 s, depois aproximação lenta |
| `pan_left_to_right` | câmera desliza da esquerda para a direita |
| `shake_impact` | tremor forte no primeiro meio segundo |
| `slow_ken_burns` | zoom lento com leve deriva |
| `pulse_glow` | zoom lento + brilho/saturação pulsando |

Rodar de novo não gera de novo o que já existe (economiza API). `visuals/manifest.json` lista
as cenas; as durações são estimadas pelas palavras e o editor (Módulo 4) re-renderiza com a
duração exata da narração.
