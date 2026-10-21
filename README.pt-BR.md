<p align="center">
  <a href="https://agent2kb.com"><img src="docs/assets/hero.svg" width="100%"
    alt="agent2kb: o compilador de conhecimento para equipes de agentes"></a>
</p>

<p align="center">
  <b>O agent2kb transforma tudo o que seus agentes encontram em uma wiki Markdown viva e com citações.</b><br>
  Um LLM a compila no seu próprio bucket; os agentes a leem via MCP,
  e as pessoas a leem no Obsidian ou em qualquer editor Markdown.
</p>

<p align="center">
  <a href="https://agent2kb.com"><b>agent2kb.com</b></a> ·
  <a href="#experimente">Experimente</a> ·
  <a href="docs/first-run.md">Primeira execução</a> ·
  <a href="#por-que-agent2kb">Por que agent2kb</a> ·
  <a href="#documentação">Documentação</a>
</p>

<p align="center">
  <a href="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml"><img
    src="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg"
    alt="Licença: AGPL-3.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg"
    alt="Python 3.10+"></a>
  <img src="https://img.shields.io/badge/MCP-server-6366f1.svg" alt="Servidor MCP">
  <img src="https://img.shields.io/badge/storage-any%20S3-0d9488.svg" alt="Qualquer armazenamento S3">
  <img src="https://img.shields.io/badge/runs-fully%20local%20(Ollama)-c2410c.svg"
    alt="Roda totalmente local com Ollama">
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.de.md">Deutsch</a> ·
  <a href="README.es.md">Español</a> ·
  <b>Português (Brasil)</b> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.ko.md">한국어</a> ·
  <a href="README.zh-TW.md">繁體中文</a>
</p>

<p align="center"><i>Esta é uma tradução; o <a href="README.md">README em inglês</a> é a referência e pode estar mais atualizado.</i></p>
<!-- Switch on once the package is on PyPI:
[![PyPI](https://img.shields.io/pypi/v/agent2kb.svg)](https://pypi.org/project/agent2kb/)
-->

**Um compilador de conhecimento headless para equipes de agentes.** Agentes e bots enviam fontes
brutas (páginas web, vídeos, PDFs, notas); um LLM as compila em uma wiki que se mantém sozinha,
feita de páginas Markdown interligadas; os agentes a leem pela CLI ou via MCP, e as pessoas a
leem no Obsidian ou em qualquer outro editor Markdown. Cada projeto tem sua própria wiki isolada
em qualquer bucket compatível com S3, e nenhum aplicativo precisa ficar aberto.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pipeline-dark.svg">
  <img src="docs/assets/pipeline-light.svg" width="100%"
    alt="Como o agent2kb funciona: a2kb ingest coloca as fontes em raw/, a2kb compile as
    transforma em páginas de wiki/ com um LLM, os agentes leem a wiki pela CLI e via MCP, as
    pessoas a leem no Obsidian, e as respostas salvas em outputs/ alimentam a próxima compilação">
</picture>

<!-- TODO(demo): record a short terminal GIF (ingest -> compile -> query) and save it as
     docs/assets/demo.gif, then uncomment:
![a2kb demo](docs/assets/demo.gif)
-->

- **Ingira uma vez, compile sem supervisão.** O cron executa compile, lint, backup e digest, e
  o pipeline recorre a outro LLM quando um deles está indisponível. Os agentes só precisam de
  `a2kb ingest`.
- **Uma base de conhecimento (KB) por projeto.** Cada KB tem seu próprio prefixo e, quando o
  provedor de armazenamento permite, suas próprias credenciais, de modo que um agente que
  trabalha em uma KB não consegue acessar as outras.
- **Uma única wiki para agentes e pessoas.** Os agentes usam um servidor MCP ou a CLI; as
  pessoas leem Markdown simples com `[[wikilinks]]` no Obsidian ou em qualquer editor Markdown.
  As respostas salvas alimentam a próxima compilação, então a wiki cresce nas áreas mais
  consultadas.
- **Projetado para conteúdo não confiável e para privacidade.** Páginas web e downloads de
  arquivos passam por uma proteção contra SSRF (URLs de vídeo, que o próprio `yt-dlp` busca,
  são a exceção documentada), `claude` e `codex` rodam sem ferramentas, novas KBs mantêm os
  canais laterais para a nuvem desativados, e uma configuração totalmente local com Ollama é
  suportada.

Ele segue o
[padrão de wiki LLM](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) de
Andrej Karpathy e o transforma em uma ferramenta do lado do servidor.

### O que sua equipe vê em um editor de wiki

<p align="center">
  <img src="docs/assets/vault.svg" width="100%"
    alt="Ilustração: uma página compilada do agent2kb aberta no Obsidian, com tags no
    frontmatter, o artigo com wikilinks, uma visualização em grafo das páginas relacionadas e um
    painel de backlinks">
</p>

Cada página compilada chega como Markdown simples com suas propriedades (`source`, `date`, tags,
`source_kind`), um resumo, seções estruturadas e `[[wikilinks]]`, de modo que a visualização em
grafo, os backlinks e a busca por tags do Obsidian funcionam de imediato, e o `index.md` começa
com um resumo "Past 7 days" do que a KB aprendeu naquela semana. Links não resolvidos ficam como
nós fantasma até que uma compilação posterior ou o `a2kb stubs` os preencha. *Ilustração
construída a partir do exemplo em [examples/first-note.md](examples/first-note.md); veja
[docs/obsidian.md](docs/obsidian.md) para conectar seu vault.*

O Obsidian é o leitor que o agent2kb documenta e recomenda, porque ele renderiza `[[wikilinks]]`,
o grafo e os backlinks, e pode sincronizar a partir do S3 com o plugin Remotely Save. Os arquivos
são Markdown comum, então qualquer editor que os leia funciona; ferramentas que não sejam o
Obsidian precisam de sua própria forma de sincronizar a partir do S3 e não foram testadas.

### Um resumo do que há de novo, escrito para você

Cada compilação atualiza o topo de `wiki/index.md` com um resumo dos últimos 7 dias: uma breve
síntese dos temas que as novas páginas têm em comum, escrita pelo LLM a partir dessas páginas,
seguida de links para cada uma delas. Ninguém precisa escrevê-lo, e ele acompanha a KB, então um
colega que volta de férias, ou alguém que estuda e passa por ali uma vez por semana, vê de
relance o que a KB aprendeu. Um exemplo (conteúdo fictício):

```markdown
## Past 7 Days (4 articles)

> This week the KB focused on safer releases. Two articles compare canary and blue-green
> deployments and agree that automated rollback matters more than the strategy itself; a
> third shows how feature flags keep unfinished work out of a release, and an incident review
> traces an outage to a missing health check, linking it to [[Rollback]] and [[Monitoring]].

- 2026-10-20 [[20261020-Canary-Releases-in-Practice-1a2b3c4d|Canary Releases in Practice]]
- 2026-10-19 [[20261019-Blue-Green-Deployments-5e6f7a8b|Blue-Green Deployments]]
- 2026-10-17 [[20261017-Feature-Flags-for-Safe-Releases-9c0d1e2f|Feature Flags for Safe Releases]]
- 2026-10-15 [[20261015-Incident-Review-Missing-Health-Check-3a4b5c6d|Incident Review: Missing Health Check]]
```

A janela é uma configuração (`recent_days`: `30` para um resumo mensal, `0` para desativá-lo), e
um digest semanal opcional grava um resumo mais longo em `outputs/weekly/`.

### Todos os dispositivos e todos os agentes, uma única KB

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/devices-dark.svg">
  <img src="docs/assets/devices-light.svg" width="100%"
    alt="Uma KB no seu bucket S3, compartilhada por pessoas no desktop, iPhone e Android por meio
    do Obsidian, e por agentes pela CLI e via MCP; a compilação roda de forma agendada ao lado">
</picture>

A KB fica no seu bucket S3, não em uma única máquina, então a mesma wiki compilada chega a todos
os dispositivos: Obsidian no macOS, Windows e Linux, no iPhone e iPad, e no Android (por meio do
plugin Remotely Save), e agentes em qualquer notebook, servidor ou runner de CI (pela CLI ou via
MCP). A compilação roda de forma agendada junto ao armazenamento, então as páginas continuam
melhorando mesmo com todos os dispositivos desligados. Celulares precisam de um endpoint HTTPS
(AWS S3, Cloudflare R2, ou o backend Docker com seu perfil HTTPS); veja
[docs/obsidian.md](docs/obsidian.md). O mantenedor já usou essa configuração com o Obsidian em
vários dispositivos desktop e móveis contra um backend SeaweedFS auto-hospedado em versões
anteriores; as receitas da versão atual ainda não foram reexecutadas de ponta a ponta.

## Experimente

Com os pré-requisitos abaixo prontos, a primeira página leva alguns minutos; criar uma conta na
nuvem, um bucket e chaves leva mais tempo e está descrito em
[provedores de armazenamento](docs/storage-providers.md).

Pré-requisitos: Linux ou macOS com um shell POSIX (as plataformas que o pacote declara; o Windows
não foi testado), Python 3.10 ou posterior com seu módulo `venv` (no Debian e no Ubuntu:
`sudo apt install python3-venv`) e `git`. Além disso:

- **Armazenamento:** um bucket compatível com S3 e uma chave para ele (receitas para AWS S3 e
  Cloudflare R2: [provedores de armazenamento](docs/storage-providers.md)), ou o
  [backend Docker](#backend-docker-em-10-minutos), que inclui o armazenamento.
- **Um LLM:** a CLI `claude` ou `codex`, instalada e com login feito
  (`npm i -g @anthropic-ai/claude-code` e `claude auth login`, ou `npm i -g @openai/codex`
  e `codex login`); ou uma chave de API (Gemini, Groq, OpenRouter); ou um servidor Ollama local.

### Instalação

Instale em um ambiente virtual, para que o agent2kb e suas dependências fiquem separados do
Python do sistema (muitas distribuições Linux recusam um `pip install` no sistema inteiro com um
erro `externally-managed-environment`):

```bash
git clone https://github.com/jcaol/agent2kb.git && cd agent2kb
python3 -m venv .venv                       # uma vez: um ambiente isolado em .venv/
source .venv/bin/activate                   # em cada novo terminal (zsh e bash)
python -m pip install --upgrade pip
python -m pip install ".[all]"              # instalação a partir do código-fonte até o primeiro release no PyPI
a2kb --help                                 # deve exibir a lista de comandos
```

`python -m pip` instala no ambiente ativo, seja qual for o `pip` que aparece primeiro no seu
`PATH`. Em um terminal posterior, execute `source .venv/bin/activate` no checkout novamente antes
de usar o `a2kb`, ou chame `.venv/bin/a2kb` diretamente (por exemplo, a partir do cron).
`deactivate` sai do ambiente.

`[all]` instala todos os SDKs de provedores, a ingestão de documentos e vídeos e o servidor MCP.
Uma instalação menor basta para texto, Markdown e páginas web com login em uma CLI `claude` ou
`codex`:

```bash
python -m pip install .                     # só o núcleo (verificado: instala, `a2kb --help` roda)
python -m pip install ".[mcp]"              # núcleo mais o servidor MCP para agentes
```

O que cada recurso precisa além da instalação do núcleo (`a2kb doctor` lista o que está faltando):

| Recurso | Extra do Python | Programa externo |
|---|---|---|
| Markdown, texto, páginas web | nenhum | nenhum |
| CLI `claude` / `codex` como LLM | nenhum | a CLI, com login feito |
| Gemini, Groq, OpenRouter, Ollama | `gemini`, `groq`, `openai`, `ollama` | um servidor Ollama, para o Ollama |
| Embeddings e rerank (opcional) | `voyage`, `openai`, `gemini` ou `ollama`; `fast` para busca vetorial mais rápida | nenhum |
| Documentos `.docx`, `.xlsx` | `media` | nenhum |
| PDF | `media` | nenhum |
| Outros formatos de documento (por exemplo `.epub`) | nenhum | `pandoc` |
| URLs de vídeo (YouTube e outros) | `media` (yt-dlp) | `ffmpeg` |
| Transcrição local de áudio e vídeo | `whisper` (pesado; **não** faz parte de `all`) | `ffmpeg` |
| Servidor MCP (`a2kb mcp`) | `mcp` | nenhum |

Os extras podem ser combinados: `python -m pip install ".[mcp,media]"`. Pacotes Python nunca
instalam `ffmpeg` nem `pandoc`; use o gerenciador de pacotes do seu sistema
(`apt install ffmpeg pandoc`, `brew install ffmpeg pandoc`).

### Primeira KB

```bash
a2kb setup                        # assistente: armazenamento, LLM, idioma, privacidade, sua primeira KB (notes)
a2kb doctor                       # verifica config, acesso ao S3, login do LLM, ferramentas externas

a2kb ingest --file examples/first-note.md       # um pequeno exemplo que vem com o repositório
a2kb compile                      # raw/ -> wiki/ (incremental; --force recompila tudo)
a2kb query "What is the codename of the alpha project?"
```

O [tutorial de primeira execução](docs/first-run.md) percorre os mesmos passos com a saída
esperada de cada um (o nome do arquivo bruto, a página da wiki, a resposta com citações), conecta
o Obsidian e traz uma tabela de recuperação para os problemas mais comuns. Para ingerir suas
próprias fontes, use `a2kb ingest --url <url>`, `--file <path>` ou `--folder <dir>`.

Se você executar qualquer comando antes de existir uma KB, o `a2kb` se oferece para iniciar o
`a2kb setup` para você (somente em um terminal; scripts, cron e agentes recebem o erro habitual).

O `a2kb doctor` informa a falta de login no `claude` ou no `codex` como uma falha. Isso é esperado
até você fazer login; chaves de API e o Ollama não exigem login. Enquanto você tiver exatamente
uma KB, a opção `-p` pode ser omitida; com várias KBs, use `a2kb -p notes ...`. `a2kb list`
mostra suas KBs, e `a2kb new myproject --here` grava um ponteiro `.kb/config.yaml` para que o
`a2kb` funcione em uma pasta de projeto sem `-p`. O pacote se chama `agent2kb`, e o único comando
que ele instala é `a2kb` (um `alias kb=a2kb` pode oferecer uma forma mais curta).

## Por que agent2kb

Equipes de agentes acumulam conhecimento em logs de chat, notas avulsas e abas do navegador.
Colocar tudo isso em um vector store devolve fragmentos em vez de compreensão; um único vault
compartilhado mistura projetos e não tem nenhum processo que o mantenha consistente; e nenhuma
das duas abordagens dá às pessoas que dependem desse conhecimento uma forma de auditá-lo.

O agent2kb separa o trabalho em três etapas. A **coleta** é deliberadamente simples: qualquer
processo pode executar `ingest` para colocar uma fonte em `raw/`. A **compreensão** é a etapa de
compilação: um LLM transforma as fontes em páginas com títulos, resumos, tags e links, reescreve
apenas o que mudou e rotula o que ele mesmo derivou, para que as fontes brutas prevaleçam quando
houver divergência. O **uso** acontece por duas interfaces sobre os mesmos arquivos: `a2kb query`
e MCP para os agentes, e o Obsidian para as pessoas. Tudo é armazenado como Markdown no seu
próprio bucket, então pode ser lido, comparado, copiado em backup e editado à mão.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pillars-dark.svg">
  <img src="docs/assets/pillars-light.svg" width="100%"
    alt="O que diferencia o agent2kb: 1. compila, não apenas recupera; 2. uma KB isolada por
    projeto; 3. agentes e pessoas, uma única wiki; 4. conhecimento que se acumula; 5. seu bucket,
    sem lock-in; 6. seguro com conteúdo não confiável">
</picture>

### Como se compara

| | Vector store / RAG | Vault de notas compartilhado | Memória de sessão do agente | **agent2kb** |
|---|:---:|:---:|:---:|:---:|
| Páginas legíveis, não fragmentos | ❌ chunks | ✅ escritas à mão | ❌ transcrições | ✅ **compiladas por LLM** |
| Mantido consistente sem trabalho manual | ❌ | ❌ | ❌ | ✅ **compilação incremental + lint** |
| Conhecimento separado e isolado por projeto | ⚠️ por índice | ❌ um único vault | ⚠️ por agente | ✅ **um prefixo e uma chave por KB** |
| Agentes e pessoas usam a mesma fonte | ❌ só agentes | ⚠️ só pessoas | ❌ só agentes | ✅ **MCP + editor Markdown** |
| Toda resposta cita suas fontes | ⚠️ | ❌ | ❌ | ✅ |
| Arquivos simples em um armazenamento seu | ❌ | ✅ | ❌ | ✅ **qualquer bucket S3** |
| Pode rodar sem nenhum terceiro | ⚠️ | ✅ | ❌ | ✅ **Ollama + S3 local** |

Todo arquivo traz frontmatter YAML (`title`, `source`, `date`, `tags`, `source_kind`).
`source_kind` registra o nível de confiança: fontes brutas e tópicos curados são primários;
páginas de conceito e respostas salvas são derivadas. Veja
[docs/compounding-loop.md](docs/compounding-loop.md).

## Recursos

| | |
|---|---|
| **Ingestão** | URLs, YouTube e outros vídeos, áudio, PDF/DOCX/XLSX, imagens (OCR), Markdown, recortes do Obsidian Web Clipper; detecção de duplicatas, verificação de segurança antes de buscar |
| **Compilação** | Compilação incremental de raw para wiki com `[[wikilinks]]`, páginas de conceito e stubs para links fantasma; um índice que começa com um resumo "Past 7 days", e um digest semanal opcional; saída em inglês ou em qualquer idioma que você indicar |
| **Consulta** | Busca por palavra-chave com embeddings e reranking opcionais, respostas com citações e expansão dos termos de busca; respostas salvas em `outputs/` fecham o ciclo de acumulação |
| **Acesso para agentes** | Servidor MCP (`a2kb mcp`, stdio ou HTTP protegido por bearer token), somente leitura por padrão; `ask` e `ingest_url` são opcionais (opt-in) |
| **Isolamento** | Prefixos por KB impostos pelo cliente; credenciais por KB quando o provedor consegue restringi-las |
| **LLMs** | Logins nas CLIs `claude` / `codex` (em sandbox, com uma CLI de fallback), Gemini, Groq, OpenRouter, Ollama; modelos separados para a compilação em massa e para as tarefas auxiliares |
| **Operações** | `a2kb doctor`, `lint`, `backup` e `restore`, um resumo de execução para cada compilação, wrappers de cron em `scripts/` |
| **Backend Docker** | Um único `docker compose up`: SeaweedFS com chaves por KB, cron, um servidor MCP por KB, HTTPS opcional |

## Início rápido (local)

[Experimente](#experimente) cobre o caminho completo, e o
[tutorial de primeira execução](docs/first-run.md) mostra o resultado esperado de cada passo.
Observações adicionais:

- `a2kb setup --non-interactive` recebe tudo por flags e variáveis `KB_*`, para CI e
  provisionamento ([flags](docs/configuration.md#non-interactive-setup)).
- Presets de armazenamento: AWS S3, Cloudflare R2, Backblaze B2, MinIO, SeaweedFS, Wasabi, outros
  ([provedores de armazenamento](docs/storage-providers.md)).
- Compile, lint, backup e stubs rodam de forma agendada: veja
  [Mantendo a wiki compilada](#mantendo-a-wiki-compilada).
- Todos os comandos e flags: [docs/commands.md](docs/commands.md).

## Mantendo a wiki compilada

O `a2kb ingest` apenas armazena as fontes em `raw/`; as páginas aparecem quando o `a2kb compile`
é executado. Você pode executá-lo à mão, mas a configuração habitual o executa **de forma
agendada**, para que tudo o que um agente ou uma pessoa ingere vire uma página sem que ninguém
precise esperar por isso:

| Como você roda o agent2kb | Quem agenda a compilação |
|---|---|
| Backend Docker | Já vem pronto: compile a cada 15 minutos, lint diário, backup duas vezes por dia, stubs diários (`KB_CRON_DIGEST=1` adiciona um digest semanal) |
| Instalação nativa (Linux, macOS) | Você adiciona uma entrada no cron, usando os wrappers em `scripts/` |
| Uso ocasional | Execute `a2kb compile` você mesmo depois de ingerir |

Um crontab mínimo para uma instalação nativa (`crontab -e`; ajuste os caminhos):

```cron
# a cada 15 minutos: compila fontes novas ou alteradas (sem uso de LLM quando nada mudou)
*/15 * * * * /path/to/agent2kb/scripts/kb-cron-compile.sh /path/to/project notes
# diariamente: lint estrutural e páginas provisórias para tópicos ausentes muito referenciados
0 9 * * *    /path/to/agent2kb/scripts/kb-cron-lint.sh  /path/to/project notes
0 3 * * *    /path/to/agent2kb/scripts/kb-cron-stubs.sh /path/to/project notes
# duas vezes por dia: arquivo de backup local da KB
30 2,14 * * * /path/to/agent2kb/scripts/kb-cron-backup.sh /path/to/project notes
```

Os wrappers gravam logs em `kb-*.log` na pasta do projeto e chamam o compile com `--strict`,
para que uma compilação com falha termine com código diferente de zero para fins de
monitoramento. Execuções sobrepostas são seguras: enquanto uma compilação de uma KB detém o seu
lease no armazenamento, outra execução da mesma KB pula aquele ciclo (exit 75 com `--strict`) e
o ciclo seguinte retoma o trabalho. O modelo completo é `scripts/kb-crontab.example`; os
detalhes de agendamento estão em [docs/project-workflow.md](docs/project-workflow.md#cron).

**Qual LLM compila é uma configuração, não parte do agendamento.** O cron só executa
`a2kb compile`; o bloco `llm:` da KB decide quem faz o trabalho:

| `llm.provider` | O que compila | Custo e privacidade |
|---|---|---|
| `cli` com `command: claude` ou `codex` | A CLI do Claude Code ou do Codex em que você está logado, executada sem ferramentas em um sandbox | Usa sua assinatura; o conteúdo vai para esse provedor |
| `ollama` | Um modelo local do Ollama (`model: llama3.3:70b`, ou qualquer modelo que você tenha baixado) | Totalmente local; exige uma máquina capaz de rodar o modelo |
| `gemini`, `groq`, `openrouter` | A API desse provedor com a sua chave | Uso de API cobrado (ou no plano gratuito); o conteúdo vai para esse provedor |

Com `provider: cli`, `fallback_command` indica uma segunda CLI (por exemplo `codex` atrás do
`claude`) que assume pelo resto da execução quando a primeira fica sem créditos ou sem login.
`model` e `aux_model` escolhem os modelos para a compilação de páginas e para as tarefas mais
leves (páginas de conceito, relatórios de lint, digest); ambos usam por padrão a escolha
recomendada pelo provedor. Detalhes: [docs/llm-providers.md](docs/llm-providers.md).

## Configurações e personalização

Tudo é definido em um único arquivo no nível do usuário, `~/.kb/config.yaml` (modo 0600; o
`a2kb setup` o grava, e `KB_CONFIG` pode apontar para outro local). As configurações em
`defaults:` se aplicam a todas as KBs; as mesmas chaves dentro de uma KB as sobrescrevem apenas
para essa KB:

```yaml
version: 2
storage: { bucket: my-kb, region: auto, endpoint: "https://<account>.r2.cloudflarestorage.com" }
llm:
  provider: cli
  command: claude
  fallback_command: codex
defaults:
  output_language: en          # inglês por padrão; qualquer nome de idioma funciona
  privacy_mode: false          # true: nenhuma chamada à nuvem durante a ingestão
  allow_jina_reader: false     # true: páginas web difíceis de ler são buscadas via jina.ai
  allow_cloud_vision: false    # true: imagens são enviadas a um modelo na nuvem para OCR
kbs:
  notes: {}                    # usa os valores padrão
  research:
    output_language: Spanish   # esta KB escreve suas páginas e respostas em espanhol
  client-work:
    llm: { provider: cli, command: codex }   # esta KB é compilada pela CLI do Codex
```

O início mais rápido é com a CLI `claude` ou `codex` em que você já está logado: sem chave de API
e sem serviço extra. Para uma KB cujo conteúdo nunca pode sair da sua máquina, defina
`llm: { provider: ollama }` e rode um modelo local com o Ollama (é preciso uma máquina capaz de
rodar o modelo; veja [provedores de LLM](docs/llm-providers.md#ollama)).

| Para mudar | Defina | Mais |
|---|---|---|
| Idioma das páginas e respostas | `output_language` (por KB ou em `defaults`) | [Idioma de saída](docs/configuration.md#output-language) |
| O resumo "Past N days" no índice | `recent_days` (7 por padrão, `0` = desativado) | [Resumo das páginas recentes](docs/configuration.md#recent-pages-recap) |
| Qual LLM compila e responde | o bloco `llm:` (permitido por KB) | [Provedores de LLM](docs/llm-providers.md) |
| O que pode sair da sua rede | `privacy_mode`, `allow_jina_reader`, `allow_cloud_vision` | [Privacidade](docs/privacy.md) |
| Onde uma KB é armazenada | `storage:` (compartilhado) ou o `storage:` e o `prefix` próprios de uma KB | [Provedores de armazenamento](docs/storage-providers.md) |
| Qual KB uma pasta usa | `.kb/config.yaml` com `kb: <name>` nessa pasta (`a2kb new <name> --here`) | [Configuração](docs/configuration.md#using-a-kb-from-a-project-folder) |
| Uma sobrescrita pontual | variáveis de ambiente `KB_*`, por exemplo `KB_OUTPUT_LANGUAGE=ja` | [Variáveis de ambiente](docs/configuration.md#environment-variables) |

O `a2kb config show` exibe o valor efetivo de cada configuração e de qual arquivo ou variável ele
veio; o `a2kb doctor` verifica se o resultado funciona. Segredos (chaves de armazenamento, chaves
de API) pertencem apenas a esse arquivo no nível do usuário ou a variáveis de ambiente, nunca a
uma pasta de projeto.

## Backend Docker em 10 minutos

```bash
cp .env.example .env                  # dê nome às suas KBs (KB_NAMES=research,work), escolha um LLM
docker compose up -d                  # SeaweedFS (S3) + jobs de cron + servidores MCP; as chaves são geradas
docker compose exec kb claude auth login                                # uma vez, se KB_LLM=claude
docker compose exec kb a2kb connect obsidian research --show-secret   # configurações do Remotely Save
docker compose exec kb a2kb connect agent research --show-secret      # `claude mcp add ...` com URL e token
```

Isso gera um armazenamento S3 com uma chave separada e restrita por KB, o bucket, a configuração,
um agendamento de cron e um servidor MCP somente leitura por KB. Defina `DOMAIN=kb.example.com` e
use `--profile https` para obter HTTPS automático (para o Obsidian no celular e para agentes
remotos), ou configure seu próprio armazenamento S3. Os serviços do compose se chamam `kb` e
`kb-mcp`; o comando dentro deles é `a2kb`. O passo a passo, a rotação de chaves e o registro de
testes estão em [docs/docker.md](docs/docker.md).

## Conectar o Obsidian

```bash
a2kb connect obsidian notes                # configurações do Remotely Save para a KB notes
a2kb connect obsidian notes --show-secret  # o mesmo, incluindo a chave secreta
```

Isso funciona em qualquer configuração: AWS S3, Cloudflare R2, outros provedores e o backend
Docker (nele: `docker compose exec kb a2kb connect obsidian <kb>`). O comando exibe o endpoint, a
região, o bucket, o estilo de URL e o prefixo remoto da KB (por exemplo `kbs/notes/`) para o
plugin da comunidade **Remotely Save** ("S3 or compatible"). Com o prefixo definido, a raiz do
vault é a KB: `raw/`, `wiki/` e `outputs/` aparecem como pastas. Para uma KB que usa a cadeia de
credenciais da AWS, a ficha mostra placeholders para a chave, porque um dispositivo deve receber
sua própria chave, nunca o login da AWS da máquina.

Escolha a chave para o plugin de acordo com o que o dispositivo faz:

- **Somente visualização:** uma chave somente leitura (listar e ler) e uma direção de
  sincronização apenas de download (pull).
- **Edição e Web Clipper:** a sincronização bidirecional envia e exclui arquivos, então ela
  precisa de uma chave que possa listar, ler, gravar e excluir no prefixo da KB: uma chave
  separada para o dispositivo, não a que o agent2kb usa, para que ela possa ser revogada
  isoladamente.

Deixe a senha de criptografia do plugin vazia para a KB (o agent2kb lê Markdown simples) e comece
com um vault novo e vazio. Compilação e sincronização seguem relógios separados: uma página chega
ao dispositivo na primeira sincronização após a compilação, e a sincronização só roda enquanto o
Obsidian está aberto. O guia completo, incluindo as duas receitas, a estrutura de pastas e uma
verificação da primeira sincronização, está em [docs/obsidian.md](docs/obsidian.md).

`[[wikilinks]]` não resolvidos são nós fantasma intencionais que uma compilação posterior
resolve; `assets/obsidian-snippets/dim-ghost-links.css` os deixa esmaecidos, e o `a2kb stubs`
pode criar páginas provisórias para os mais referenciados.

## Conectar seus agentes (MCP)

```bash
claude mcp add kb-notes -- a2kb -p notes mcp      # somente leitura: search, read_page, list_recent, kb_info
```

`--allow-llm` adiciona a ferramenta `ask` e `--allow-write` adiciona `ingest_url`; o modo HTTP
exige um bearer token. Trechos de configuração para o Codex e o Claude Desktop estão em
[docs/mcp.md](docs/mcp.md). Para instruir um agente sobre quando consultar a KB,
`a2kb scaffold rules --target <file>` grava uma seção "KB query discipline" no seu arquivo de
instruções (por exemplo `AGENTS.md`).

## Comparação com outras abordagens

- **Sincronização simples do Obsidian.** Sincronizar um vault fornece armazenamento e um leitor,
  mas nada escreve as páginas, e todos os projetos compartilham uma única pasta. O agent2kb
  acrescenta o compilador, o isolamento por projeto e a operação sem supervisão, enquanto o
  Obsidian continua sendo o leitor. Para um único vault local que você quer consultar a partir do
  seu editor, um plugin que roda dentro do vault é a escolha mais simples.
- **Serviços de RAG vetorial.** Eles devolvem os fragmentos de texto mais próximos de uma
  pergunta. O agent2kb compila as fontes em páginas legíveis com links e procedência, de modo que
  o resultado pode ser navegado, corrigido e comparado, e a recuperação roda sobre essas páginas.
  Embeddings são opcionais.
- **Ferramentas de memória de sessão de agentes.** Elas registram o que um agente fez e disse. O
  agent2kb compila fontes externas, e as respostas que você escolhe salvar, em uma wiki de
  referência compartilhada. As duas abordagens se complementam: a memória de sessão responde "o
  que foi tentado", e o agent2kb responde "o que se sabe".

## Segurança e privacidade

- A compilação e a consulta enviam o texto das páginas ao LLM configurado, e um plano gratuito do
  Gemini pode usar os prompts para treinamento. Novas KBs mantêm o Jina Reader e o OCR de imagens
  na nuvem **desativados**, e toda compilação e ingestão exibe um banner que indica o provedor
  efetivo. Para material sensível, use `llm.provider: ollama`. Detalhes:
  [docs/privacy.md](docs/privacy.md).
- Páginas web e downloads de arquivos passam por uma proteção contra SSRF que bloqueia endereços
  privados, verifica cada redirecionamento e fixa a conexão. **URLs de vídeo não são cobertas:** o
  `yt-dlp` resolve nomes e segue redirecionamentos por conta própria, então não ingira URLs de
  vídeo de hosts não confiáveis em uma máquina que consiga alcançar serviços internos. Via MCP,
  URLs de vídeo só são aceitas do YouTube ou de hosts em `--ingest-allow-domain`. Detalhes:
  [docs/privacy.md](docs/privacy.md#video-urls-yt-dlp) e
  [docs/mcp.md](docs/mcp.md#fetch-safety-ssrf).
- `claude` e `codex` rodam sem ferramentas e sem MCP.
- O isolamento entre KBs tem duas camadas: uma verificação de prefixo no agent2kb e credenciais
  por KB restritas pelo provedor de armazenamento. Somente a segunda camada protege contra uma
  chave vazada. **Versões do SeaweedFS anteriores à 4.34 são afetadas pela CVE-2026-55874**, que
  permitia que uma chave de uma KB copiasse objetos de outra KB; o backend Docker fixa a versão
  4.48. Veja [docs/storage-providers.md](docs/storage-providers.md#seaweedfs).
- Relate vulnerabilidades de forma privada: [SECURITY.md](SECURITY.md).

## Perguntas frequentes

**Meus dados são usados para treinamento?** O agent2kb não tem telemetria e envia dados apenas ao
armazenamento e ao LLM que você configura. Se um provedor de LLM treina com os prompts é definido
pela política desse provedor: um plano gratuito do Gemini pode fazer isso, enquanto um servidor
Ollama local mantém os dados na sua máquina. Você continua responsável por deter os direitos
sobre o conteúdo que ingere.

**Quais LLMs são suportados?** Uma CLI `claude` ou `codex` com login feito (sem necessidade de
chave de API), Gemini, Groq, OpenRouter e Ollama. Veja
[docs/llm-providers.md](docs/llm-providers.md).

**Ele pode rodar totalmente local?** Sim. Use o Ollama para a compilação e os embeddings, o
SeaweedFS ou o MinIO para o armazenamento, e ative `privacy_mode`. Assim, seu conteúdo nunca é
enviado a terceiros.

**Ele precisa de GPU?** Não. Só um modelo local do Ollama se beneficia de uma, porque a
compilação por meio de uma CLI ou de uma API roda remotamente. A transcrição local de áudio (o
extra `whisper`) é opcional.

**As pessoas podem editar as páginas?** Sim, no Obsidian ou em qualquer editor Markdown. Uma
página só é regenerada quando sua fonte bruta é recompilada. Para páginas escritas à mão que nunca
devem ser regeneradas, use `a2kb promote --as wiki-topic`.

**Quais idiomas são suportados?** As fontes podem estar em qualquer idioma. Páginas e respostas
são escritas em inglês, a menos que você defina `output_language` como outro idioma, para uma KB
ou para todas.

**Como vejo o que há de novo?** Abra `wiki/index.md`: ele começa com um resumo automático dos
últimos 7 dias (veja [o exemplo](#um-resumo-do-que-há-de-novo-escrito-para-você)). Defina
`recent_days` por KB para mudar a janela ou `0` para desativá-lo. O digest semanal opcional
(`scripts/kb-cron-digest.sh`, ou `KB_CRON_DIGEST=1` no Docker) grava um resumo mais longo da
semana em `outputs/weekly/`.

## Roadmap

O agent2kb está em uma versão anterior à 1.0. A CLI, os formatos de configuração e a estrutura no
S3 são fixados por testes de contrato, mas algumas arestas ainda por aparar devem ser esperadas.

- Publicar o pacote no PyPI (até lá, instale a partir do código-fonte).
- Verificar outros provedores de armazenamento: apenas o SeaweedFS foi testado contra um servidor
  real, e R2, B2, MinIO, AWS e Wasabi seguem suas respectivas documentações.
- Reexecutar o fluxo completo do `docker compose` e uma sincronização do Obsidian em vários
  dispositivos para esta versão, incluindo certificados HTTPS reais (ambos foram usados com
  versões anteriores).
- Fora dos planos: uma interface web (a leitura é feita pelo Obsidian, pela CLI ou via MCP) e
  memória de sessão de agentes.

## Documentação

| | |
|---|---|
| [docs/first-run.md](docs/first-run.md) | Primeira execução com um arquivo de exemplo: ingestão, compilação, consulta, Obsidian, recuperação |
| [docs/obsidian.md](docs/obsidian.md) | Obsidian e Remotely Save: chaves, direção de sincronização, criptografia, timing |
| [docs/configuration.md](docs/configuration.md) | Arquivo de configuração, KBs, papel de admin, variáveis `KB_*`, idioma de saída |
| [docs/commands.md](docs/commands.md) | Todos os comandos e flags |
| [docs/llm-providers.md](docs/llm-providers.md) | Provedores, sandbox das CLIs, roteamento e fallback |
| [docs/storage-providers.md](docs/storage-providers.md) | Receitas para AWS S3 e R2, B2, MinIO, SeaweedFS, Wasabi, isolamento, erros comuns |
| [docs/docker.md](docs/docker.md) | O backend Docker: compose, Obsidian, agentes, HTTPS, rotação de chaves |
| [docs/mcp.md](docs/mcp.md) | Servidor MCP, ferramentas, modo HTTP, modelo de segurança |
| [docs/privacy.md](docs/privacy.md) | O que cada etapa envia a qual terceiro |
| [docs/project-workflow.md](docs/project-workflow.md) | Uso diário, `source_meta`, cron |
| [docs/compounding-loop.md](docs/compounding-loop.md) | Como as respostas salvas alimentam a próxima compilação |
| [docs/operations.md](docs/operations.md) | Backup, manifest, resumos de execução, higiene de lint |
| [CHANGELOG.md](CHANGELOG.md) | Releases e notas de atualização |

## Como contribuir

Veja [CONTRIBUTING.md](CONTRIBUTING.md) para a configuração do ambiente, o contrato de
compatibilidade e as regras de código; agentes de programação devem ler [CLAUDE.md](CLAUDE.md).
Siga o [Código de Conduta](CODE_OF_CONDUCT.md).

## Marcas registradas

O agent2kb é um projeto open source independente. Ele não é afiliado, endossado nem patrocinado
por Obsidian, Anthropic, OpenAI, Google, Amazon Web Services, Cloudflare ou qualquer outra
empresa ou produto citado neste repositório. Todos os nomes de produtos, logotipos e marcas
registradas pertencem a seus respectivos proprietários e são usados apenas para descrever
compatibilidade.

## Licença

O agent2kb é licenciado sob a [GNU Affero General Public License v3.0](LICENSE)
(`AGPL-3.0-only`). Você pode usá-lo, modificá-lo e redistribuí-lo nesses termos; se você o
modificar e permitir que outras pessoas o usem por uma rede (por exemplo, como serviço hospedado
ou como servidor MCP compartilhado), a AGPL exige que você ofereça a elas o código-fonte da sua
versão modificada.

**Licença comercial.** Se os termos da AGPL não se adequarem ao seu uso, por exemplo para
incorporar o agent2kb em um produto ou serviço de código fechado, uma licença comercial separada
pode estar disponível com os mantenedores: veja [agent2kb.com](https://agent2kb.com) ou escreva
para [licensing@agent2kb.com](mailto:licensing@agent2kb.com).

Contribuições são aceitas sob o [Contributor Licence Agreement](CLA.md) (acordo de licença de
contribuidor), que permite ao projeto oferecer as duas licenças.
