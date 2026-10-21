<p align="center">
  <a href="https://agent2kb.com"><img src="docs/assets/hero.svg" width="100%"
    alt="agent2kb: el compilador de conocimiento para equipos de agentes"></a>
</p>

<p align="center">
  <b>agent2kb convierte todo lo que encuentran sus agentes en una wiki Markdown viva y con citas.</b><br>
  Un LLM la compila en su propio bucket; los agentes la leen por MCP
  y las personas, en Obsidian o en cualquier editor Markdown.
</p>

<p align="center">
  <a href="https://agent2kb.com"><b>agent2kb.com</b></a> ·
  <a href="#pruébelo">Pruébelo</a> ·
  <a href="docs/first-run.md">Primera ejecución</a> ·
  <a href="#por-qué-agent2kb">Por qué agent2kb</a> ·
  <a href="#documentación">Documentación</a>
</p>

<p align="center">
  <a href="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml"><img
    src="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg"
    alt="Licencia: AGPL-3.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg"
    alt="Python 3.10+"></a>
  <img src="https://img.shields.io/badge/MCP-server-6366f1.svg" alt="Servidor MCP">
  <img src="https://img.shields.io/badge/storage-any%20S3-0d9488.svg" alt="Cualquier almacenamiento S3">
  <img src="https://img.shields.io/badge/runs-fully%20local%20(Ollama)-c2410c.svg"
    alt="Funciona de forma totalmente local con Ollama">
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.de.md">Deutsch</a> ·
  <b>Español</b> ·
  <a href="README.pt-BR.md">Português (Brasil)</a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.ko.md">한국어</a> ·
  <a href="README.zh-TW.md">繁體中文</a>
</p>

*Esta es una traducción; el [README en inglés](README.md) es la referencia y puede estar más actualizado.*

<!-- Switch on once the package is on PyPI:
[![PyPI](https://img.shields.io/pypi/v/agent2kb.svg)](https://pypi.org/project/agent2kb/)
-->

**Un compilador de conocimiento headless para equipos de agentes.** Los agentes y bots envían
fuentes en bruto (páginas web, videos, PDF, notas); un LLM las compila en una wiki que se
mantiene sola, formada por páginas Markdown enlazadas entre sí; los agentes la leen mediante la
CLI o MCP, y las personas la leen en Obsidian o en cualquier otro editor Markdown. Cada
proyecto tiene su propia wiki aislada en cualquier bucket compatible con S3, y ninguna
aplicación necesita permanecer abierta.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pipeline-dark.svg">
  <img src="docs/assets/pipeline-light.svg" width="100%"
    alt="Cómo funciona agent2kb: a2kb ingest coloca las fuentes en raw/, a2kb compile las
    convierte en páginas de wiki/ con un LLM, los agentes leen la wiki mediante la CLI y MCP,
    las personas la leen en Obsidian, y las respuestas guardadas en outputs/ alimentan la
    siguiente compilación">
</picture>

<!-- TODO(demo): record a short terminal GIF (ingest -> compile -> query) and save it as
     docs/assets/demo.gif, then uncomment:
![a2kb demo](docs/assets/demo.gif)
-->

- **Ingiera una vez, compile sin supervisión.** Cron ejecuta compile, lint, backup y digest, y
  el pipeline recurre a otro LLM cuando uno no está disponible. Los agentes solo necesitan
  `a2kb ingest`.
- **Una base de conocimiento (KB) por proyecto.** Cada KB tiene su propio prefijo y, cuando el
  proveedor de almacenamiento lo permite, sus propias credenciales, de modo que un agente que
  trabaja en una KB no puede acceder a las demás.
- **Una sola wiki para agentes y personas.** Los agentes usan un servidor MCP o la CLI; las
  personas leen Markdown simple con `[[wikilinks]]` en Obsidian o en cualquier editor Markdown.
  Las respuestas guardadas alimentan la siguiente compilación, de modo que la wiki crece en las
  áreas que se consultan con más frecuencia.
- **Diseñado para contenido no confiable y para la privacidad.** Las páginas web y las
  descargas de archivos pasan por una protección contra SSRF (las URL de video, que `yt-dlp`
  descarga por su cuenta, son la excepción documentada), `claude` y `codex` se ejecutan sin
  herramientas, las KB nuevas mantienen desactivados los canales laterales hacia la nube, y se
  admite una configuración totalmente local con Ollama.

Sigue el [patrón de wiki LLM](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
de Andrej Karpathy y lo convierte en una herramienta del lado del servidor.

### Lo que su equipo ve en un editor de wiki

<p align="center">
  <img src="docs/assets/vault.svg" width="100%"
    alt="Ilustración: una página compilada por agent2kb abierta en Obsidian, con etiquetas en
    el frontmatter, el artículo con wikilinks, una vista de grafo de páginas relacionadas y un
    panel de backlinks">
</p>

Cada página compilada llega como Markdown simple con sus propiedades (`source`, `date`,
etiquetas, `source_kind`), un resumen, secciones estructuradas y `[[wikilinks]]`, de modo que la
vista de grafo, los backlinks y la búsqueda por etiquetas de Obsidian funcionan de inmediato, e
`index.md` empieza con un resumen de los "últimos 7 días" de lo que la KB aprendió esa semana.
Los enlaces sin resolver quedan como nodos fantasma hasta que una compilación posterior o
`a2kb stubs` los completa. *Ilustración creada a partir del ejemplo de
[examples/first-note.md](examples/first-note.md); consulte [docs/obsidian.md](docs/obsidian.md)
para conectar su bóveda (vault).*

Obsidian es el lector que agent2kb documenta y recomienda, porque muestra los `[[wikilinks]]`,
el grafo y los backlinks, y puede sincronizar desde S3 con el plugin Remotely Save. Los archivos
son Markdown corriente, así que cualquier editor que los lea sirve; las herramientas distintas
de Obsidian necesitan su propia forma de sincronizar desde S3 y no se han probado.

### Un resumen de las novedades, escrito para usted

Cada compilación actualiza la parte superior de `wiki/index.md` con un resumen de los últimos 7
días: una breve síntesis de los temas que comparten las páginas nuevas, escrita por el LLM a
partir de esas páginas, seguida de enlaces a cada una de ellas. Nadie tiene que escribirlo y se
mantiene al día con la KB, de modo que un compañero que vuelve de vacaciones, o alguien que
aprende y la revisa una vez por semana, ve de un vistazo lo que la KB ha aprendido. Un ejemplo
(contenido ficticio):

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

El periodo es configurable (`recent_days`: `30` para un resumen mensual, `0` para desactivarlo),
y un digest semanal opcional escribe un resumen más extenso en `outputs/weekly/`.

### Todos los dispositivos y todos los agentes, una sola KB

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/devices-dark.svg">
  <img src="docs/assets/devices-light.svg" width="100%"
    alt="Una KB en su bucket S3, compartida por personas en escritorio, iPhone y Android a
    través de Obsidian, y por agentes a través de la CLI y MCP; la compilación se ejecuta
    según una programación junto a ella">
</picture>

La KB vive en su bucket S3, no en una sola máquina, así que la misma wiki compilada llega a
todos los dispositivos: Obsidian en macOS, Windows y Linux, en iPhone y iPad, y en Android
(mediante el plugin Remotely Save), y los agentes en cualquier portátil, servidor o runner de CI
(mediante la CLI o MCP). La compilación se ejecuta de forma programada junto al almacenamiento,
de modo que las páginas siguen mejorando aunque todos los dispositivos estén apagados. Los
teléfonos necesitan un endpoint HTTPS (AWS S3, Cloudflare R2 o el backend Docker con su perfil
HTTPS); consulte [docs/obsidian.md](docs/obsidian.md). El mantenedor ha utilizado esta
configuración con Obsidian en varios dispositivos de escritorio y móviles contra un backend
SeaweedFS autoalojado en versiones anteriores; las recetas de la versión actual aún no se han
vuelto a ejecutar de principio a fin.

## Pruébelo

Una vez cumplidos los requisitos previos indicados abajo, la primera página tarda unos pocos
minutos; crear una cuenta en la nube, un bucket y las claves lleva más tiempo y se describe en
[proveedores de almacenamiento](docs/storage-providers.md).

Requisitos previos: Linux o macOS con un shell POSIX (las plataformas que declara el paquete;
Windows no se ha probado), Python 3.10 o posterior con su módulo `venv` (en Debian y Ubuntu:
`sudo apt install python3-venv`) y `git`. Además:

- **Almacenamiento:** un bucket compatible con S3 y una clave para él (recetas para AWS S3 y
  Cloudflare R2: [proveedores de almacenamiento](docs/storage-providers.md)), o el
  [backend Docker](#backend-docker-en-10-minutos), que incluye el almacenamiento.
- **Un LLM:** la CLI de `claude` o `codex`, instalada y con sesión iniciada
  (`npm i -g @anthropic-ai/claude-code` y `claude auth login`, o `npm i -g @openai/codex`
  y `codex login`); o una clave de API (Gemini, Groq, OpenRouter); o un servidor Ollama local.

### Instalación

Instale en un entorno virtual, para que agent2kb y sus dependencias queden separados del Python
del sistema (muchas distribuciones Linux rechazan un `pip install` a nivel de sistema con un
error `externally-managed-environment`):

```bash
git clone https://github.com/jcaol/agent2kb.git && cd agent2kb
python3 -m venv .venv                       # una vez: un entorno aislado en .venv/
source .venv/bin/activate                   # en cada terminal nueva (zsh y bash)
python -m pip install --upgrade pip
python -m pip install ".[all]"              # instalación desde el código fuente hasta la primera versión en PyPI
a2kb --help                                 # debería mostrar la lista de comandos
```

`python -m pip` instala en el entorno activo, sea cual sea el `pip` que aparezca primero en su
`PATH`. En una terminal posterior, vuelva a ejecutar `source .venv/bin/activate` en el
directorio del repositorio antes de usar `a2kb`, o llame directamente a `.venv/bin/a2kb` (por
ejemplo, desde cron). `deactivate` sale del entorno.

`[all]` instala todos los SDK de proveedores, la ingesta de documentos y video, y el servidor
MCP. Una instalación más pequeña basta para texto, Markdown y páginas web con una sesión
iniciada en la CLI de `claude` o `codex`:

```bash
python -m pip install .                     # solo el núcleo (comprobado: se instala, `a2kb --help` funciona)
python -m pip install ".[mcp]"              # núcleo más el servidor MCP para agentes
```

Lo que cada funcionalidad necesita además de la instalación del núcleo (`a2kb doctor` indica lo
que falta):

| Funcionalidad | Extra de Python | Programa externo |
|---|---|---|
| Markdown, texto, páginas web | ninguno | ninguno |
| CLI de `claude` / `codex` como LLM | ninguno | la CLI, con sesión iniciada |
| Gemini, Groq, OpenRouter, Ollama | `gemini`, `groq`, `openai`, `ollama` | un servidor Ollama, para Ollama |
| Embeddings y rerank (opcional) | `voyage`, `openai`, `gemini` u `ollama`; `fast` para una búsqueda vectorial más rápida | ninguno |
| Documentos `.docx`, `.xlsx` | `media` | ninguno |
| PDF | `media` | ninguno |
| Otros formatos de documento (por ejemplo, `.epub`) | ninguno | `pandoc` |
| URL de video (YouTube y otros) | `media` (yt-dlp) | `ffmpeg` |
| Transcripción local de audio y video | `whisper` (pesado; **no** forma parte de `all`) | `ffmpeg` |
| Servidor MCP (`a2kb mcp`) | `mcp` | ninguno |

Los extras se pueden combinar: `python -m pip install ".[mcp,media]"`. Los paquetes de Python
nunca instalan `ffmpeg` ni `pandoc`; use el gestor de paquetes de su sistema
(`apt install ffmpeg pandoc`, `brew install ffmpeg pandoc`).

### Primera KB

```bash
a2kb setup                        # asistente: almacenamiento, LLM, idioma, privacidad, su primera KB (notes)
a2kb doctor                       # comprueba la configuración, el acceso a S3, la sesión del LLM y las herramientas externas

a2kb ingest --file examples/first-note.md       # un pequeño ejemplo incluido en el repositorio
a2kb compile                      # raw/ -> wiki/ (incremental; --force lo recompila todo)
a2kb query "What is the codename of the alpha project?"
```

El [tutorial de primera ejecución](docs/first-run.md) recorre los mismos pasos con la salida
esperada de cada uno (el nombre del archivo raw, la página de la wiki, la respuesta con citas),
conecta Obsidian e incluye una tabla de recuperación para los problemas habituales. Para ingerir
sus propias fuentes, use `a2kb ingest --url <url>`, `--file <path>` o `--folder <dir>`.

Si ejecuta cualquier comando antes de que exista una KB, `a2kb` le ofrece iniciar `a2kb setup`
(solo en una terminal; los scripts, cron y los agentes reciben el error habitual).

`a2kb doctor` informa como fallo la ausencia de sesión en `claude` o `codex`. Es lo esperado
hasta que inicie sesión; las claves de API y Ollama no requieren inicio de sesión. Mientras
tenga exactamente una KB, puede omitir la opción `-p`; con varias KB, use `a2kb -p notes ...`.
`a2kb list` muestra sus KB, y `a2kb new myproject --here` escribe un puntero `.kb/config.yaml`
para que `a2kb` funcione en una carpeta de proyecto sin `-p`. El paquete se llama `agent2kb`, y
el único comando que instala es `a2kb` (un `alias kb=a2kb` puede ofrecer una forma más corta).

## Por qué agent2kb

Los equipos de agentes acumulan conocimiento en registros de chat, notas sueltas y pestañas del
navegador. Ponerlo todo en un vector store devuelve fragmentos en lugar de comprensión; una
única bóveda compartida mezcla proyectos y no tiene ningún proceso que la mantenga coherente; y
ninguno de los dos enfoques da a las personas que dependen de ese conocimiento una forma de
auditarlo.

agent2kb divide el trabajo en tres etapas. La **recopilación** es deliberadamente simple:
cualquier proceso puede ejecutar `ingest` para colocar una fuente en `raw/`. La **comprensión**
es el paso de compilación: un LLM convierte las fuentes en páginas con títulos, resúmenes,
etiquetas y enlaces, reescribe solo lo que ha cambiado y etiqueta lo que deriva por sí mismo,
de modo que las fuentes en bruto prevalecen cuando hay discrepancias. El **uso** se realiza a
través de dos interfaces sobre los mismos archivos: `a2kb query` y MCP para los agentes, y
Obsidian para las personas. Todo se almacena como Markdown en su propio bucket, así que puede
leerse, compararse, respaldarse y editarse a mano.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pillars-dark.svg">
  <img src="docs/assets/pillars-light.svg" width="100%"
    alt="Lo que diferencia a agent2kb: 1. compila, no solo recupera; 2. una KB aislada por
    proyecto; 3. agentes y personas, una sola wiki; 4. conocimiento que se acumula; 5. su
    bucket, sin dependencia de un proveedor; 6. seguro con contenido no confiable">
</picture>

### Cómo se compara

| | Vector store / RAG | Bóveda de notas compartida | Memoria de sesión del agente | **agent2kb** |
|---|:---:|:---:|:---:|:---:|
| Páginas legibles, no fragmentos | ❌ chunks | ✅ escritas a mano | ❌ transcripciones | ✅ **compiladas por LLM** |
| Se mantiene coherente sin trabajo manual | ❌ | ❌ | ❌ | ✅ **compilación incremental + lint** |
| Conocimiento separado y aislado por proyecto | ⚠️ por índice | ❌ una bóveda | ⚠️ por agente | ✅ **un prefijo y una clave por KB** |
| Agentes y personas usan la misma fuente | ❌ solo agentes | ⚠️ solo personas | ❌ solo agentes | ✅ **MCP + editor Markdown** |
| Cada respuesta cita sus fuentes | ⚠️ | ❌ | ❌ | ✅ |
| Archivos simples en un almacenamiento propio | ❌ | ✅ | ❌ | ✅ **cualquier bucket S3** |
| Puede funcionar sin ningún tercero | ⚠️ | ✅ | ❌ | ✅ **Ollama + S3 local** |

Cada archivo lleva frontmatter YAML (`title`, `source`, `date`, `tags`, `source_kind`).
`source_kind` registra la confianza: las fuentes en bruto y los temas curados son primarios; las
páginas de conceptos y las respuestas guardadas son derivadas. Consulte
[docs/compounding-loop.md](docs/compounding-loop.md).

## Funcionalidades

| | |
|---|---|
| **Ingesta** | URL, YouTube y otros videos, audio, PDF/DOCX/XLSX, imágenes (OCR), Markdown, recortes de Obsidian Web Clipper; detección de duplicados, comprobación de seguridad antes de descargar |
| **Compilación** | Compilación incremental de raw a wiki con `[[wikilinks]]`, páginas de conceptos y stubs para enlaces fantasma; un índice que empieza con un resumen de los "últimos 7 días" y un digest semanal opcional; salida en inglés o en el idioma que usted indique |
| **Consulta** | Búsqueda por palabras clave con embeddings y reranking opcionales, respuestas con citas y expansión de términos de búsqueda; las respuestas guardadas en `outputs/` cierran el ciclo de acumulación |
| **Acceso de agentes** | Servidor MCP (`a2kb mcp`, stdio o HTTP protegido con bearer token), de solo lectura por defecto; `ask` e `ingest_url` se activan opcionalmente |
| **Aislamiento** | Prefijos por KB aplicados por el cliente; credenciales por KB cuando el proveedor puede limitar su alcance |
| **LLM** | Sesiones de la CLI de `claude` / `codex` (en sandbox, con una CLI de respaldo), Gemini, Groq, OpenRouter, Ollama; modelos separados para la compilación masiva y para las tareas auxiliares |
| **Operaciones** | `a2kb doctor`, `lint`, `backup` y `restore`, un resumen de ejecución por cada compilación, wrappers de cron en `scripts/` |
| **Backend Docker** | Un solo `docker compose up`: SeaweedFS con claves por KB, cron, un servidor MCP por KB, HTTPS opcional |

## Inicio rápido (local)

[Pruébelo](#pruébelo) cubre el recorrido completo, y el
[tutorial de primera ejecución](docs/first-run.md) muestra el resultado esperado de cada paso.
Notas adicionales:

- `a2kb setup --non-interactive` toma todo de flags y de variables `KB_*`, para CI y
  aprovisionamiento ([flags](docs/configuration.md#non-interactive-setup)).
- Ajustes predefinidos de almacenamiento: AWS S3, Cloudflare R2, Backblaze B2, MinIO, SeaweedFS,
  Wasabi, otros ([proveedores de almacenamiento](docs/storage-providers.md)).
- Compile, lint, backup y stubs se ejecutan de forma programada: consulte
  [Mantener la wiki compilada](#mantener-la-wiki-compilada).
- Todos los comandos y flags: [docs/commands.md](docs/commands.md).

## Mantener la wiki compilada

`a2kb ingest` solo almacena las fuentes en `raw/`; las páginas aparecen cuando se ejecuta
`a2kb compile`. Puede ejecutarlo a mano, pero la configuración habitual lo ejecuta **de forma
programada**, de modo que todo lo que ingiera un agente o una persona se convierte en una página
sin que nadie tenga que esperar:

| Cómo ejecuta agent2kb | Quién programa la compilación |
|---|---|
| Backend Docker | Ya está hecho: compile cada 15 minutos, lint a diario, backup dos veces al día, stubs a diario (`KB_CRON_DIGEST=1` añade un digest semanal) |
| Instalación nativa (Linux, macOS) | Usted añade una entrada de cron, usando los wrappers de `scripts/` |
| Uso ocasional | Ejecute usted mismo `a2kb compile` después de ingerir |

Un crontab mínimo para una instalación nativa (`crontab -e`; ajuste las rutas):

```cron
# cada 15 minutos: compilar fuentes nuevas o modificadas (sin uso del LLM si nada cambió)
*/15 * * * * /path/to/agent2kb/scripts/kb-cron-compile.sh /path/to/project notes
# a diario: lint estructural y páginas provisionales para temas ausentes muy enlazados
0 9 * * *    /path/to/agent2kb/scripts/kb-cron-lint.sh  /path/to/project notes
0 3 * * *    /path/to/agent2kb/scripts/kb-cron-stubs.sh /path/to/project notes
# dos veces al día: archivo de copia de seguridad local de la KB
30 2,14 * * * /path/to/agent2kb/scripts/kb-cron-backup.sh /path/to/project notes
```

Los wrappers escriben sus registros en `kb-*.log` en la carpeta del proyecto y llaman a compile
con `--strict`, de modo que una compilación fallida termina con un código distinto de cero para
la monitorización. Las ejecuciones superpuestas son seguras: mientras una compilación de una KB
mantiene su lease en el almacenamiento, otra ejecución de la misma KB omite ese ciclo (código de
salida 75 con `--strict`) y el siguiente ciclo retoma el trabajo. La plantilla completa es
`scripts/kb-crontab.example`; los detalles de la programación están en
[docs/project-workflow.md](docs/project-workflow.md#cron).

**Qué LLM compila es un ajuste de configuración, no parte de la programación.** Cron solo
ejecuta `a2kb compile`; el bloque `llm:` de la KB decide quién hace el trabajo:

| `llm.provider` | Qué compila | Costo y privacidad |
|---|---|---|
| `cli` con `command: claude` o `codex` | La CLI de Claude Code o Codex en la que tiene la sesión iniciada, ejecutada sin herramientas en un sandbox | Usa su suscripción; el contenido se envía a ese proveedor |
| `ollama` | Un modelo local de Ollama (`model: llama3.3:70b`, o cualquier modelo que haya descargado) | Totalmente local; necesita una máquina capaz de ejecutar el modelo |
| `gemini`, `groq`, `openrouter` | La API de ese proveedor con su clave | Uso de API facturado (o del nivel gratuito); el contenido se envía a ese proveedor |

Con `provider: cli`, `fallback_command` indica una segunda CLI (por ejemplo, `codex` detrás de
`claude`) que toma el relevo durante el resto de la ejecución cuando la primera se queda sin
créditos o pierde la sesión. `model` y `aux_model` eligen los modelos para la compilación de
páginas y para las tareas más ligeras (páginas de conceptos, informes de lint, digest); ambos
toman por defecto la opción recomendada por el proveedor.
Detalles: [docs/llm-providers.md](docs/llm-providers.md).

## Configuración y personalización

Todo se configura en un único archivo a nivel de usuario, `~/.kb/config.yaml` (modo 0600;
`a2kb setup` lo escribe, y `KB_CONFIG` puede apuntar a otra ubicación). Los ajustes bajo
`defaults:` se aplican a todas las KB; las mismas claves dentro de una KB los sustituyen solo
para esa KB:

```yaml
version: 2
storage: { bucket: my-kb, region: auto, endpoint: "https://<account>.r2.cloudflarestorage.com" }
llm:
  provider: cli
  command: claude
  fallback_command: codex
defaults:
  output_language: en          # inglés por defecto; sirve cualquier nombre de idioma
  privacy_mode: false          # true: ninguna llamada a la nube durante la ingesta
  allow_jina_reader: false     # true: las páginas web difíciles de leer se obtienen a través de jina.ai
  allow_cloud_vision: false    # true: las imágenes se envían a un modelo en la nube para OCR
kbs:
  notes: {}                    # usa los valores por defecto
  research:
    output_language: Spanish   # esta KB escribe sus páginas y respuestas en español
  client-work:
    llm: { provider: cli, command: codex }   # esta KB la compila en cambio la CLI de Codex
```

La forma más rápida de empezar es la CLI de `claude` o `codex` en la que ya tiene la sesión
iniciada: sin clave de API y sin ningún servicio adicional. Para una KB cuyo contenido nunca
debe salir de su máquina, configure `llm: { provider: ollama }` y ejecute un modelo local con
Ollama (necesita una máquina capaz de ejecutar el modelo; consulte
[proveedores de LLM](docs/llm-providers.md#ollama)).

| Para cambiar | Configure | Más información |
|---|---|---|
| Idioma de las páginas y respuestas | `output_language` (por KB o en `defaults`) | [Output language](docs/configuration.md#output-language) |
| El resumen de los "últimos N días" en el índice | `recent_days` (7 por defecto, `0` = desactivado) | [Recent-pages recap](docs/configuration.md#recent-pages-recap) |
| Qué LLM compila y responde | el bloque `llm:` (se permite por KB) | [Proveedores de LLM](docs/llm-providers.md) |
| Qué puede salir de su red | `privacy_mode`, `allow_jina_reader`, `allow_cloud_vision` | [Privacidad](docs/privacy.md) |
| Dónde se almacena una KB | `storage:` (compartido) o el `storage:` y el `prefix` propios de una KB | [Proveedores de almacenamiento](docs/storage-providers.md) |
| Qué KB usa una carpeta | `.kb/config.yaml` con `kb: <name>` en esa carpeta (`a2kb new <name> --here`) | [Configuración](docs/configuration.md#using-a-kb-from-a-project-folder) |
| Un cambio puntual | Variables de entorno `KB_*`, por ejemplo `KB_OUTPUT_LANGUAGE=ja` | [Variables de entorno](docs/configuration.md#environment-variables) |

`a2kb config show` muestra el valor efectivo de cada ajuste y de qué archivo o variable
proviene; `a2kb doctor` comprueba que el resultado funciona. Los secretos (claves de
almacenamiento, claves de API) solo deben estar en este archivo a nivel de usuario o en
variables de entorno, nunca en una carpeta de proyecto.

## Backend Docker en 10 minutos

```bash
cp .env.example .env                  # ponga nombre a sus KB (KB_NAMES=research,work), elija un LLM
docker compose up -d                  # SeaweedFS (S3) + tareas cron + servidores MCP; las claves se generan
docker compose exec kb claude auth login                                # una vez, si KB_LLM=claude
docker compose exec kb a2kb connect obsidian research --show-secret   # ajustes de Remotely Save
docker compose exec kb a2kb connect agent research --show-secret      # `claude mcp add ...` con URL y token
```

Esto genera un almacenamiento S3 con una clave independiente y de alcance limitado por KB, el
bucket, la configuración, una programación de cron y un servidor MCP de solo lectura por KB.
Configure `DOMAIN=kb.example.com` y use `--profile https` para obtener HTTPS automático (para
Obsidian en un teléfono y para agentes remotos), o configure en su lugar su propio
almacenamiento S3. Los servicios de compose se llaman `kb` y `kb-mcp`; el comando dentro de
ellos es `a2kb`. La guía paso a paso, la rotación de claves y el registro de pruebas están en
[docs/docker.md](docs/docker.md).

## Conectar Obsidian

```bash
a2kb connect obsidian notes                # ajustes de Remotely Save para la KB notes
a2kb connect obsidian notes --show-secret  # lo mismo, incluida la clave secreta
```

Esto funciona en cualquier configuración: AWS S3, Cloudflare R2, otros proveedores y el backend
Docker (en este caso: `docker compose exec kb a2kb connect obsidian <kb>`). Muestra el endpoint,
la región, el bucket, el estilo de URL y el prefijo remoto de la KB (por ejemplo, `kbs/notes/`)
para el plugin de la comunidad **Remotely Save** ("S3 or compatible"). Con el prefijo
configurado, la raíz de la bóveda es la KB: `raw/`, `wiki/` y `outputs/` aparecen como
carpetas. Para una KB que usa la cadena de credenciales de AWS, la hoja muestra marcadores de
posición en lugar de la clave, porque cada dispositivo debe tener su propia clave, nunca el
inicio de sesión de AWS de la máquina.

Elija la clave para el plugin según lo que haga el dispositivo:

- **Solo lectura:** una clave de solo lectura (listar y leer) y una dirección de sincronización
  de solo descarga (pull-only).
- **Edición y Web Clipper:** la sincronización bidireccional sube y elimina archivos, así que
  necesita una clave que pueda listar, leer, escribir y eliminar en el prefijo de la KB: una
  clave aparte para el dispositivo, no la que usa agent2kb, para poder revocarla por separado.

Deje vacía la contraseña de cifrado del plugin para la KB (agent2kb lee Markdown sin cifrar) y
empiece con una bóveda nueva y vacía. La compilación y la sincronización siguen relojes
distintos: una página llega al dispositivo en la primera sincronización posterior a la
compilación, y la sincronización solo se ejecuta mientras Obsidian está abierto. La guía
completa, con ambas recetas, la estructura de carpetas y una comprobación de la primera
sincronización, está en [docs/obsidian.md](docs/obsidian.md).

Los `[[wikilinks]]` sin resolver son nodos fantasma intencionados que una compilación posterior
resuelve; `assets/obsidian-snippets/dim-ghost-links.css` los atenúa, y `a2kb stubs` puede crear
páginas provisionales para los más referenciados.

## Conectar sus agentes (MCP)

```bash
claude mcp add kb-notes -- a2kb -p notes mcp      # solo lectura: search, read_page, list_recent, kb_info
```

`--allow-llm` añade la herramienta `ask` y `--allow-write` añade `ingest_url`; el modo HTTP
requiere un bearer token. Los fragmentos de configuración para Codex y Claude Desktop están en
[docs/mcp.md](docs/mcp.md). Para indicar a un agente cuándo consultar la KB,
`a2kb scaffold rules --target <file>` escribe una sección "KB query discipline" en su archivo de
instrucciones (por ejemplo, `AGENTS.md`).

## Comparación con otros enfoques

- **Sincronización simple de Obsidian.** Sincronizar una bóveda aporta almacenamiento y un
  lector, pero nada escribe las páginas, y todos los proyectos comparten una carpeta. agent2kb
  añade el compilador, el aislamiento por proyecto y el funcionamiento sin supervisión, mientras
  Obsidian sigue siendo el lector. Para una única bóveda local que quiera consultar desde su
  editor, un plugin que se ejecute dentro de la bóveda es la opción más sencilla.
- **Servicios de RAG vectorial.** Devuelven los fragmentos de texto más cercanos a una pregunta.
  agent2kb compila las fuentes en páginas legibles con enlaces y procedencia, de modo que el
  resultado se puede explorar, corregir y comparar, y la recuperación se hace sobre esas
  páginas. Los embeddings son opcionales.
- **Herramientas de memoria de sesión de agentes.** Registran lo que un agente hizo y dijo.
  agent2kb compila fuentes externas, y las respuestas que usted decide guardar, en una wiki de
  referencia compartida. Ambos enfoques se complementan: la memoria de sesión responde a "qué se
  intentó", y agent2kb responde a "qué se sabe".

## Seguridad y privacidad

- La compilación y las consultas envían el texto de las páginas al LLM configurado, y un nivel
  gratuito de Gemini puede usar los prompts para entrenamiento. Las KB nuevas mantienen
  **desactivados** Jina Reader y el OCR de imágenes en la nube, y cada compilación e ingesta
  muestra un aviso con el proveedor efectivo. Para material sensible, use
  `llm.provider: ollama`. Detalles: [docs/privacy.md](docs/privacy.md).
- Las páginas web y las descargas de archivos pasan por una protección contra SSRF que bloquea
  direcciones privadas, comprueba cada redirección y fija la conexión. **Las URL de video no
  están cubiertas:** `yt-dlp` resuelve los nombres y sigue las redirecciones por su cuenta, así
  que no ingiera URL de video de hosts no confiables en una máquina que pueda alcanzar servicios
  internos. Por MCP, las URL de video solo se aceptan de YouTube o de los hosts indicados con
  `--ingest-allow-domain`. Detalles:
  [docs/privacy.md](docs/privacy.md#video-urls-yt-dlp) y
  [docs/mcp.md](docs/mcp.md#fetch-safety-ssrf).
- `claude` y `codex` se ejecutan sin herramientas y sin MCP.
- El aislamiento de las KB tiene dos capas: una comprobación de prefijo en agent2kb y
  credenciales por KB con alcance limitado por el proveedor de almacenamiento. Solo la segunda
  capa protege frente a una clave filtrada.
  **Las versiones de SeaweedFS anteriores a la 4.34 están afectadas por CVE-2026-55874**, que
  permitía que una clave de una KB copiara objetos de otra KB; el backend Docker fija la
  versión 4.48. Consulte [docs/storage-providers.md](docs/storage-providers.md#seaweedfs).
- Informe de vulnerabilidades de forma privada: [SECURITY.md](SECURITY.md).

## Preguntas frecuentes

**¿Se usan mis datos para entrenamiento?** agent2kb no tiene telemetría y solo envía datos al
almacenamiento y al LLM que usted configure. Que un proveedor de LLM entrene con los prompts
depende de la política de ese proveedor: un nivel gratuito de Gemini puede hacerlo, mientras que
un servidor Ollama local mantiene los datos en su máquina. Usted sigue siendo responsable de
tener los derechos sobre el contenido que ingiere.

**¿Qué LLM se admiten?** Una CLI de `claude` o `codex` con sesión iniciada (no requiere clave de
API), Gemini, Groq, OpenRouter y Ollama. Consulte [docs/llm-providers.md](docs/llm-providers.md).

**¿Puede funcionar de forma totalmente local?** Sí. Use Ollama para la compilación y los
embeddings, SeaweedFS o MinIO para el almacenamiento, y active `privacy_mode`. Así su contenido
nunca se envía a terceros.

**¿Necesita una GPU?** No. Solo un modelo local de Ollama se beneficia de ella, porque la
compilación mediante una CLI o una API se ejecuta de forma remota. La transcripción local de
audio (el extra `whisper`) es opcional.

**¿Pueden las personas editar las páginas?** Sí, en Obsidian o en cualquier editor Markdown. Una
página solo se regenera cuando se vuelve a compilar su fuente en bruto. Para páginas escritas a
mano que nunca deben regenerarse, use `a2kb promote --as wiki-topic`.

**¿Qué idiomas se admiten?** Las fuentes pueden estar en cualquier idioma. Las páginas y las
respuestas se escriben en inglés, a menos que configure `output_language` con otro idioma, para
una KB o para todas.

**¿Cómo veo las novedades?** Abra `wiki/index.md`: empieza con un resumen automático de los
últimos 7 días (consulte [el ejemplo](#un-resumen-de-las-novedades-escrito-para-usted)).
Configure `recent_days` por KB para cambiar el periodo, o `0` para desactivarlo. El digest
semanal opcional (`scripts/kb-cron-digest.sh`, o `KB_CRON_DIGEST=1` en Docker) escribe un
resumen más extenso de la semana en `outputs/weekly/`.

## Hoja de ruta

agent2kb es una versión anterior a la 1.0. La CLI, los formatos de configuración y la
estructura de S3 están fijados por pruebas de contrato, pero cabe esperar algunas asperezas.

- Publicar el paquete en PyPI (hasta entonces, instálelo desde el código fuente).
- Verificar más proveedores de almacenamiento: solo SeaweedFS se ha probado contra un servidor
  real; R2, B2, MinIO, AWS y Wasabi siguen su documentación.
- Volver a ejecutar el flujo completo de `docker compose` y una sincronización de Obsidian en
  varios dispositivos para esta versión, incluidos certificados HTTPS reales (ambos se
  utilizaron con versiones anteriores).
- No previsto: una interfaz web (la lectura se hace mediante Obsidian, la CLI o MCP) ni memoria
  de sesión de agentes.

## Documentación

| | |
|---|---|
| [docs/first-run.md](docs/first-run.md) | Primera ejecución con un archivo de ejemplo: ingesta, compilación, consulta, Obsidian, recuperación |
| [docs/obsidian.md](docs/obsidian.md) | Obsidian y Remotely Save: claves, dirección de sincronización, cifrado, tiempos |
| [docs/configuration.md](docs/configuration.md) | Archivo de configuración, KB, rol de administrador, variables `KB_*`, idioma de salida |
| [docs/commands.md](docs/commands.md) | Todos los comandos y flags |
| [docs/llm-providers.md](docs/llm-providers.md) | Proveedores, sandbox de la CLI, enrutamiento y respaldo |
| [docs/storage-providers.md](docs/storage-providers.md) | Recetas para AWS S3 y R2, B2, MinIO, SeaweedFS, Wasabi, aislamiento, errores comunes |
| [docs/docker.md](docs/docker.md) | El backend Docker: compose, Obsidian, agentes, HTTPS, rotación de claves |
| [docs/mcp.md](docs/mcp.md) | Servidor MCP, herramientas, modo HTTP, modelo de seguridad |
| [docs/privacy.md](docs/privacy.md) | Qué envía cada etapa y a qué tercero |
| [docs/project-workflow.md](docs/project-workflow.md) | Uso diario, `source_meta`, cron |
| [docs/compounding-loop.md](docs/compounding-loop.md) | Cómo las respuestas guardadas alimentan la siguiente compilación |
| [docs/operations.md](docs/operations.md) | Copias de seguridad, manifest, resúmenes de ejecución, higiene de lint |
| [CHANGELOG.md](CHANGELOG.md) | Versiones y notas de actualización |

## Contribuir

Consulte [CONTRIBUTING.md](CONTRIBUTING.md) para la configuración del entorno, el contrato de
compatibilidad y las reglas de código; los agentes de programación deben leer
[CLAUDE.md](CLAUDE.md). Por favor, respete el [Código de conducta](CODE_OF_CONDUCT.md).

## Marcas registradas

agent2kb es un proyecto de código abierto independiente. No está afiliado, respaldado ni
patrocinado por Obsidian, Anthropic, OpenAI, Google, Amazon Web Services, Cloudflare ni ninguna
otra empresa o producto mencionado en este repositorio. Todos los nombres de productos, logotipos
y marcas registradas son propiedad de sus respectivos titulares y se usan únicamente para
describir la compatibilidad.

## Licencia

agent2kb se distribuye bajo la [GNU Affero General Public License v3.0](LICENSE)
(`AGPL-3.0-only`). Puede usarlo, modificarlo y redistribuirlo según esos términos; si lo
modifica y permite que otros lo usen a través de una red (por ejemplo, como servicio alojado o
como servidor MCP compartido), la AGPL le exige ofrecerles el código fuente de su versión
modificada.

**Licencia comercial.** Si los términos de la AGPL no se ajustan a su uso, por ejemplo para
integrar agent2kb en un producto o servicio de código cerrado, es posible que los mantenedores
ofrezcan una licencia comercial aparte: consulte [agent2kb.com](https://agent2kb.com) o escriba
a [licensing@agent2kb.com](mailto:licensing@agent2kb.com).

Las contribuciones se aceptan bajo el [Acuerdo de licencia para contribuidores](CLA.md), que
permite al proyecto ofrecer ambas licencias.
