# Venezuela Stories: guia para o Claude Code

Gerador local de 1 vídeo vertical por dia sobre a Venezuela (TikTok, Reels, Shorts).
Arquitetura e comandos completos: `README.md`.

## Ambiente
- Roda no PC do usuário (Windows, PowerShell), com o Python do `.venv` (`.venv\Scripts\python.exe`).
- Testes: `python -m unittest discover -s tests`. Rode antes de qualquer commit.
- Comandos do pipeline (`main.py`) podem passar de 2 minutos: rode com timeout de 600000 ms ou
  em segundo plano. Se o terminal matar o processo no meio, o passo fica pela metade.
- Console do Windows em cp1252: nas mensagens use `[OK]`/`[X]`, nunca símbolos como ✓/✗.
- Chaves só no `.env` (fora do Git). Nunca mostre, copie ou peça chaves no chat.
- Commits vão direto para `main` de https://github.com/Eduarjn/venezuela-stories.

## Dia a dia
- `python main.py --check` (saúde), `python main.py` (vídeo de hoje, retoma do checkpoint),
  `python main.py --date AAAA-MM-DD --status` (passos e erros do dia).
- Imagens: Wikimedia Commons (sem chave) + Pexels (`PEXELS_API_KEY`, opcional). Só domínio
  público, CC0, CC BY e Pexels; CC BY-SA só com `ALLOW_SHARE_ALIKE=1`.
- Testar uma busca sem baixar: `python visual_generator.py --search "Lake Maracaibo"`.

## Revisão antes de publicar (sempre)
1. Leia a seção REVISÃO no fim de `output\AAAA-MM-DD\post_copy.txt` (cena -> imagem).
2. Cena que cita um lugar (Lago de Maracaibo, Zulia, Catatumbo...) mostra esse lugar. Se o
   título da imagem for de outro país ou cidade, troque.
3. Cena genérica ("um céu que relampeja") pode ter imagem ilustrativa, desde que sem cidade,
   farol, ponte ou lago reconhecível de outro lugar.
4. No máximo 2 mapas ou satélites por vídeo, nunca em cenas seguidas.
5. Trilha `fact`: história que só aparece em sites de curiosidades ou turismo é narrada como
   "conta-se que" / "segundo a tradição", no `narration_text` e no `post_description`.
6. Trocar a imagem da cena N: mude o `image_query` dela em `output\AAAA-MM-DD\current_episode.json`
   (inglês; nome próprio com maiúscula; busca genérica toda em minúsculas), confira com
   `--search` que o 1º resultado com lado menor >= 600 px é o certo e rode
   `python main.py --date AAAA-MM-DD --redo-images N`. A imagem trocada vai para
   `assets\episodes\<topic_id>\visuals\rejected.json` e não volta; para trazê-la de volta,
   tire a URL de lá.
7. Mudou o `narration_text` da cena N: `--redo-voice N` (pode ir no mesmo comando do
   `--redo-images`). Mudou só título/descrição: `python main.py --date AAAA-MM-DD --post-copy`.
8. Depois de qualquer redo, leia a REVISÃO de novo. Se ela não mudou, o comando falhou: veja
   `--status` e o log `logs\AAAA-MM-DD.log`.

## Publicação (manual)
- Cole título, descrição, hashtags e o bloco "CRÉDITOS DAS IMAGENS (obrigatório...)". Os
  créditos opcionais e a REVISÃO não vão para a postagem. No Instagram (2.200 caracteres),
  corte hashtags, nunca créditos.
- Depois de publicar: `python main.py --mark-published AAAA-MM-DD`.
