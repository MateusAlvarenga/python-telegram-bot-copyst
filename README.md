# Bot copista de mídias do Telegram


[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/python-telegram-bot?referralCode=asepsp&utm_medium=integration&utm_source=template&utm_campaign=generic)


Bot privado que copia mensagens com mídias para um grupo, supergrupo ou canal configurado. O proprietário pode escolher um tópico de fórum e uma regra persistente para as legendas. A configuração fica no PostgreSQL, então continua disponível após reinicializações no Railway.

## Recursos

- Recebe mídias enviadas diretamente ou encaminhadas ao chat privado do bot.
- Copia os tipos de mensagem que a Bot API do Telegram permite copiar, incluindo fotos, vídeos, documentos, áudios, animações, mensagens de voz e stickers.
- Mantém álbuns de fotos/vídeos/áudios/documentos agrupados.
- Permite destino em grupo, supergrupo ou canal e tópico opcional em supergrupo com tópicos habilitados.
- Modos de legenda: preservar, acrescentar, inserir no início ou substituir todas as ocorrências literais.
- Restringe configuração e cópia ao proprietário identificado por `OWNER_ID`.
- Persiste as configurações em PostgreSQL.

Mensagens protegidas contra encaminhamento, mensagens de serviço e outros tipos que o Telegram não permite copiar não podem ser reproduzidos pela Bot API. Nesses casos o bot informa a falha no chat privado. O bot precisa ter permissão para enviar mensagens no destino; para um canal, adicione-o como administrador com permissão de publicação.

## Comandos

Use os comandos no chat privado do bot. Apenas o usuário definido por `OWNER_ID` pode configurá-lo.

| Comando | Descrição |
| --- | --- |
| `/start` | Mostrar instruções iniciais |
| `/help` | Listar comandos |
| `/destino <chat_id\|@username>` | Configurar o grupo, supergrupo ou canal de destino |
| `/topico <message_thread_id\|off>` | Enviar para um tópico específico ou desativar o tópico |
| `/id_topico` | No tópico do supergrupo de destino, enviar o ID do tópico ao proprietário no privado |
| `/caption original` | Preservar a legenda da mensagem |
| `/caption append <texto>` | Acrescentar o texto após a legenda, separado por uma nova linha |
| `/caption prepend <texto>` | Inserir o texto antes da legenda, separado por uma nova linha |
| `/caption replace <buscar> => <substituir>` | Substituir todas as ocorrências literais, diferenciando maiúsculas de minúsculas |
| `/status` | Mostrar destino, tópico e regra de legenda atuais |

Exemplo:

```text
/destino -1001234567890
/topico 456
/caption append Confira esta publicação
```

Use `/id_topico` dentro do tópico do supergrupo para receber seu ID no privado; depois configure com `/topico <id>`. Use `/topico off` para publicar no chat sem especificar um tópico. Ao mudar o destino, a configuração anterior do tópico é removida para evitar enviar a um tópico inválido.

O modo `replace` altera somente ocorrências do texto procurado na legenda original; se não houver correspondência, a legenda permanece igual. Para remover uma parte do texto, deixe o lado direito vazio, por exemplo `/caption replace texto antigo =>`. `/caption original` desativa qualquer transformação.

## Configuração local

1. Crie um bot com o [@BotFather](https://t.me/BotFather) e obtenha o token.
2. Descubra seu ID numérico do Telegram para preencher `OWNER_ID`.
3. Crie um banco PostgreSQL.
4. Copie `.env.example` para `.env` e preencha as variáveis:

```dotenv
BOT_TOKEN=...
OWNER_ID=123456789
DATABASE_URL=postgresql://usuario:senha@localhost:5432/bot
LOG_LEVEL=INFO
```

5. Instale dependências e inicie:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m bot.main
```

No Linux/macOS, ative o ambiente com `source .venv/bin/activate`.

## Implantação no Railway

O projeto já inclui `Dockerfile` e `railway.json`. Adicione um serviço PostgreSQL ao projeto Railway e configure no serviço do bot:

```text
BOT_TOKEN=<token do @BotFather>
OWNER_ID=<ID numérico do proprietário>
DATABASE_URL=${{Postgres.DATABASE_URL}}
LOG_LEVEL=INFO
```

Se o serviço PostgreSQL tiver outro nome no Railway, ajuste a referência da variável. O schema de configuração é criado automaticamente no primeiro início.

## Observações sobre cópia de mídias

- Ao mudar a legenda de um álbum, o bot reenvia o álbum como um único grupo e altera a legenda do item que a carrega.
- O Bot API limita álbuns a 2–10 itens e restringe quais tipos podem ser agrupados. Se um álbum ou mensagem não puder ser copiado, o bot apresenta o erro retornado pelo Telegram.
- O bot copia a mensagem recebida na conversa privada. Encaminhamentos que o Telegram marca como protegidos ou que não disponibilizam a mídia ao bot podem falhar.
- A legenda substituída é enviada como texto simples; entidades de formatação da legenda original não são reaplicadas quando o texto é modificado por `replace`.

## Testes

```powershell
python -m unittest discover -s tests
```
