# Canais, fila de publicação e agendamento

Recursos deste fork para produzir para **vários canais** com uma única configuração de IA.

- **Canais:** cada canal tem o próprio perfil no Upload-Post, plataformas, horários de postagem, fuso horário e estilo de vídeo (voz, idioma, formato, legendas, música, prompt do roteiro).
- **IA compartilhada:** LLM, vozes, fontes de vídeo e a chave de API do Upload-Post continuam globais.
- **Fila de aprovação:** os vídeos do canal ficam esperando revisão. Você edita título, legenda e hashtags e aprova ou rejeita.
- **Agendamento:** um vídeo aprovado ocupa o próximo horário livre do canal, ou a data que você escolher.
- **Fontes de vídeo em cascata:** se a Pexels, a Pixabay ou o Coverr falharem ou não acharem clipes, a próxima fonte com chave é usada.
- **Login na WebUI:** protege a interface com usuário e senha.

## Como funciona

```
Gerar vídeo (canal X) ──► vídeo pronto ──► fila do canal X
                                              │
      modo "approval": aguardando aprovação ──┤── você aprova ──► agendado ──► publicado
      modo "auto":     agendado direto ───────┘                     (no horário)
      modo "manual":   não entra na fila
```

O agendador roda dentro da WebUI e da API e confere a fila a cada 30 segundos (`publication_poll_seconds`). **O app precisa estar rodando no horário agendado.** A fila fica em `storage/publications/` e sobrevive a reinícios.

Se o app cair no meio de uma publicação, ela vai para **Com falha** e **não é repetida automaticamente**, porque o Upload-Post pode já ter postado. Quando houver um `request_id`, confira no Upload-Post antes de clicar em **Tentar de novo**.

## Configuração passo a passo

1. **Upload-Post:** em *Configurações → Publicação automática*, preencha a **chave de API**. Não é preciso marcar "publicação automática", porque os canais usam a fila.
2. **Um usuário por canal:** na página *Manage Users* do Upload-Post, crie um usuário de publicação para cada canal e conecte as redes dele.
3. **Crie o canal:** em *Configurações → Canais → Novo canal*, informe o ID (ex.: `receitas`), o nome, o usuário do Upload-Post, as plataformas, o modo, os horários (ex.: `09:00, 18:30`) e o fuso.
   - Deixe marcado **"Salvar as configurações de vídeo atuais da página como padrão deste canal"** para guardar a voz, o formato, as legendas e o prompt que estão na tela.
4. **Produza:** escolha o canal no seletor da barra superior. As configurações dele são carregadas. Escreva o tema e clique em **Gerar Vídeo**.
5. **Revise:** clique em **Fila** na barra superior. Para cada vídeo, use **Aprovar** (próximo horário livre), **Agendar na data**, **Publicar agora** ou **Rejeitar**.

## Arquivo `channels.toml`

Fica ao lado do `config.toml` (no Docker, em `/data/channels.toml`). A WebUI edita este arquivo, mas você também pode editá-lo à mão:

```toml
[channels.receitas]
name = "Receitas Rápidas"
upload_post_username = "receitas"
platforms = ["tiktok", "instagram", "youtube"]
publish_mode = "approval"            # manual | approval | auto
posting_times = ["09:00", "18:30"]
timezone = "America/Sao_Paulo"
youtube_privacy_status = "public"    # public | unlisted | private
youtube_made_for_kids = false

[channels.receitas.params]           # qualquer campo de VideoParams, exceto conteúdo
voice_name = "pt-BR-FranciscaNeural-Female"
video_language = "pt-BR"
video_aspect = "9:16"
video_source = "pixabay"
video_script_prompt = "Tom descontraído, termine com uma pergunta para o público."
```

Tema, roteiro, palavras-chave e arquivos locais **nunca** são guardados no canal. O que você informar ao gerar o vídeo sempre tem prioridade sobre o padrão do canal.

## API

Todas as rotas exigem `x-api-key` quando `app.api_key` está definido.

| Método | Rota | Para quê |
|---|---|---|
| GET | `/api/v1/channels` | listar canais |
| PUT | `/api/v1/channels/{id}` | criar ou substituir um canal |
| DELETE | `/api/v1/channels/{id}` | excluir um canal |
| POST | `/api/v1/videos` com `"channel_id": "receitas"` | gerar um vídeo para o canal |
| GET | `/api/v1/publications?status=pending_approval` | ver a fila (com contagem por estado) |
| PATCH | `/api/v1/publications/{id}` | editar `title`, `caption`, `hashtags`, `platforms` |
| POST | `/api/v1/publications/{id}/approve` | aprovar; corpo opcional `{"scheduled_at": "2026-11-01T18:30:00-03:00"}` ou `{"publish_now": true}` |
| POST | `/api/v1/publications/{id}/reject` | rejeitar |
| POST | `/api/v1/publications/{id}/retry` | tentar de novo um envio com falha |
| DELETE | `/api/v1/publications/{id}` | remover um registro publicado, rejeitado ou com falha |

Estados: `pending_approval`, `scheduled`, `publishing`, `published`, `failed`, `rejected`.

## CLI

```shell
uv run python cli.py --video-subject "Bolo de cenoura" --channel receitas
```

As opções que você passar na linha de comando têm prioridade sobre o padrão do canal. Num manifesto `--batch-file`, cada item pode ter seu próprio `"channel_id"`, o que permite produzir para vários canais numa rodada só.

## Fontes de vídeo em cascata

```toml
[app]
video_source_fallbacks = ["pexels", "pixabay", "coverr"]   # [] desativa
```

Só entram as fontes que têm chave configurada. Fontes de vídeo por IA pagas **nunca** trocam sozinhas, para não gerar cobranças inesperadas.

## Login na WebUI

- **Pela interface:** *Configurações → Acesso*. Só um hash (PBKDF2) da senha é salvo no `config.toml`.
- **Por variável de ambiente** (Docker, Coolify): `MPT_WEBUI_PASSWORD` e, opcionalmente, `MPT_WEBUI_USERNAME` (padrão `admin`). Têm prioridade sobre a configuração da interface.

Depois de 5 tentativas erradas, o login fica bloqueado por 60 segundos. Recarregar a página pede a senha de novo. Para proteger também a API, defina `app.api_key`.
