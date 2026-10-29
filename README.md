# Venezuela Stories

## Primeiro "Play" (5 passos)

```bash
1. python -m venv .venv && .venv\Scripts\activate        # Mac/Linux: source .venv/bin/activate
2. pip install -r requirements.txt                          # e instale o FFmpeg (ffmpeg -version)
3. copy .env.example .env                                   # Mac/Linux: cp — preencha as chaves e GEMINI_IMAGE_MODEL
4. python main.py --check   &&   python main.py             # testa e gera o 1º vídeo agora
5. start_scheduler.bat  +  install_autostart_windows.bat    # Mac/Linux: ./start_scheduler.sh && ./install_autostart.sh
```

Gerador local de 1 vídeo vertical por dia com histórias reais, curiosidades e
fatos históricos da Venezuela (TikTok, Reels, YouTube Shorts).

## Estrutura

```
venezuela-stories/
├── database.py            # Parte 1 – histórico de temas (SQLite)
├── story_engine.py        # Parte 2 – roteiro + prompts de cena (JSON)
├── visual_generator.py    # Parte 3 – imagens (Gemini) + clipes animados
├── voice_engine.py        # Parte 4 – narração + timestamps por palavra
├── video_editor.py        # Parte 5 – montagem final (FFmpeg)
├── main.py                # orquestrador com checkpoint
├── daily_scheduler.py     # Parte 6 – execução diária automática
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

## Narração (`voice_engine.py`)

```bash
python voice_engine.py                 # episódio de hoje
```

| `TTS_PROVIDER` | Custo | Tempo de cada palavra |
|---|---|---|
| `edge` (padrão) | grátis | evento `WordBoundary` do edge-tts |
| `elevenlabs` | pago | tempo por caractere da API, agrupado em palavras |
| `openai` | pago | não vem da API: alinhamento local com faster-whisper (`pip install -r requirements-optional.txt`) |

Depois do TTS, cada cena é "enxugada": no máximo 0,04 s antes da primeira palavra, 0,12 s
depois da última e **nenhuma pausa interna maior que 0,25 s**. Os tempos das palavras são
recalculados. Saída: `assets/episodes/{id}/audio/scene_XX.wav` e `audio/timeline.json`
(duração medida com ffprobe). 1 tentativa + até 3 novas por cena.

## Edição (`video_editor.py`)

```bash
python video_editor.py                 # gera output/AAAA-MM-DD/final_video.mp4
```

Coloque seus arquivos (com licença de uso):

| Pasta | Arquivos |
|---|---|
| `assets/sfx/` | `whoosh.mp3`, `impact.mp3` (usado para `thunder`), `riser.mp3` (para `suspense_riser`), `click.mp3` (para `old_film_click`), `crowd.mp3` (para `crowd_murmur`). Também aceita o nome exato do `sfx_cue`. |
| `assets/music/` | trilhas de suspense/históricas (`.mp3/.wav/.m4a`); cada episódio usa uma, alternando |
| `assets/fonts/` | opcional: `.ttf/.otf` da fonte das legendas (defina `SUBTITLE_FONT` com o nome dela) |

O que o editor faz:

- **Sincronia:** cada cena dura exatamente a fala dela, arredondada para frames inteiros
  (30 fps); vídeo e áudio terminam no mesmo frame.
- **Transições:** corte seco no hook; depois, *whip pan* (borrão horizontal de 0,14 s) nas
  cenas `whoosh` e flash sutil (0,12 s) nas outras, com o efeito sonoro 0,08 s antes do corte.
- **Trilha com ducking:** música a −6 dB e **−18 dB a mais enquanto há fala**, voltando nas
  pausas ≥ 0,6 s com rampas de 0,15 s. Mix final normalizado a −14 LUFS (padrão TikTok).
- **Legendas:** 2–4 palavras por vez (nunca cruzam o fim de frase), até 2 linhas, caixa alta,
  borda preta grossa, centralizadas a 61% da altura; palavra falada em amarelo, em vermelho se
  tiver número; cada grupo entra com um "pop" de 90 ms.
- **Memória:** só FFmpeg em fluxo (sem MoviePy). Cada cena é renderizada sozinha e as cenas são
  concatenadas sem recodificar; uma passada final aplica legendas + mix. Num teste com 14 cenas
  (37 s) o pico foi ~520 MB. Para limitar CPU/RAM, use `FFMPEG_THREADS=2`.
- Marca o tema no banco como `rendered`.

## Piloto automático (`main.py` + `daily_scheduler.py`)

```bash
python main.py                          # vídeo de hoje (retoma se já começou)
python main.py --check                  # FFmpeg, chaves, disco e internet
python main.py --status                 # checkpoint de hoje
python main.py --mark-published 2026-11-05
```

Saída do dia: `output/AAAA-MM-DD_titulo-do-video.mp4` e `output/AAAA-MM-DD/post_copy.txt`
(título, descrição com hashtags, hashtags, fontes e checklist antes de publicar).

**Anti-falhas**

- `output/AAAA-MM-DD/checkpoint.json` guarda cada passo (`story → visuals → voice → edit`).
  Passo concluído nunca roda de novo; dentro dos passos, imagens, áudios e clipes prontos são
  reaproveitados. Uma queda no meio não gasta crédito de novo.
- Cada passo: até 3 tentativas (espera 1 min e 5 min). Antes dos passos que usam API, espera a
  internet voltar por até 30 min.
- Health check antes de começar; trava contra duas execuções simultâneas; log em `logs/AAAA-MM-DD.log`.

**Agendador**

- Roda `main.py` todo dia em `DAILY_RUN_TIME`. Se o PC estava desligado no horário, roda ao ligar.
- Se falhar, tenta de novo a cada `RETRY_MINUTES` (até `MAX_RUNS_PER_DAY`), retomando do checkpoint.
- Cada execução é um processo separado (se travar, o agendador segue vivo; memória liberada).
- Só um agendador por computador. Log: `logs/scheduler.log`.

| Sistema | Iniciar agora | Iniciar ao ligar o PC |
|---|---|---|
| Windows | `start_scheduler.bat` (sem janela) | `install_autostart_windows.bat` (pasta Inicializar) |
| Mac/Linux | `./start_scheduler.sh` | `./install_autostart.sh` (crontab `@reboot`) |

O PC precisa estar ligado (pode estar com a tela bloqueada) no horário. A publicação continua
manual: assista ao vídeo, cole o `post_copy.txt` e rode `--mark-published`.
