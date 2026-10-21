<p align="center">
  <a href="https://agent2kb.com"><img src="docs/assets/hero.svg" width="100%"
    alt="agent2kb: der Wissens-Compiler für Agent-Teams"></a>
</p>

<p align="center">
  <b>agent2kb macht aus allem, was Ihre Agents finden, ein lebendiges Markdown-Wiki mit Quellenangaben.</b><br>
  Ein LLM kompiliert es in Ihrem eigenen Bucket; Agents lesen es über MCP,
  Menschen in Obsidian oder einem beliebigen Markdown-Editor.
</p>

<p align="center">
  <a href="https://agent2kb.com"><b>agent2kb.com</b></a> ·
  <a href="#ausprobieren">Ausprobieren</a> ·
  <a href="docs/first-run.md">Erster Start</a> ·
  <a href="#warum-agent2kb">Warum agent2kb</a> ·
  <a href="#dokumentation">Doku</a>
</p>

<p align="center">
  <a href="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml"><img
    src="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg"
    alt="Lizenz: AGPL-3.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg"
    alt="Python 3.10+"></a>
  <img src="https://img.shields.io/badge/MCP-server-6366f1.svg" alt="MCP-Server">
  <img src="https://img.shields.io/badge/storage-any%20S3-0d9488.svg" alt="Beliebiger S3-Speicher">
  <img src="https://img.shields.io/badge/runs-fully%20local%20(Ollama)-c2410c.svg"
    alt="Läuft vollständig lokal mit Ollama">
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.ja.md">日本語</a> ·
  <b>Deutsch</b> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.pt-BR.md">Português (Brasil)</a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.ko.md">한국어</a> ·
  <a href="README.zh-TW.md">繁體中文</a>
</p>

<p align="center"><i>Dies ist eine Übersetzung; maßgeblich ist das <a href="README.md">englische README</a>, das aktueller sein kann.</i></p>
<!-- Switch on once the package is on PyPI:
[![PyPI](https://img.shields.io/pypi/v/agent2kb.svg)](https://pypi.org/project/agent2kb/)
-->

**Ein headless Wissens-Compiler für Agent-Teams.** Agents und Bots liefern Rohquellen (Webseiten,
Videos, PDFs, Notizen); ein LLM kompiliert sie zu einem sich selbst pflegenden Wiki aus
verlinkten Markdown-Seiten; Agents lesen es über die CLI oder MCP, Menschen in Obsidian oder
einem anderen Markdown-Editor. Jedes Projekt hat sein eigenes, isoliertes Wiki in einem
beliebigen S3-kompatiblen Bucket, und keine Anwendung muss dafür geöffnet bleiben.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pipeline-dark.svg">
  <img src="docs/assets/pipeline-light.svg" width="100%"
    alt="So funktioniert agent2kb: a2kb ingest legt Quellen in raw/ ab, a2kb compile macht daraus
    mit einem LLM Seiten in wiki/, Agents lesen das Wiki über die CLI und MCP, Menschen lesen es in
    Obsidian, und gespeicherte Antworten in outputs/ fließen in den nächsten Compile ein">
</picture>

<!-- TODO(demo): record a short terminal GIF (ingest -> compile -> query) and save it as
     docs/assets/demo.gif, then uncomment:
![a2kb demo](docs/assets/demo.gif)
-->

- **Einmal erfassen, unbeaufsichtigt kompilieren.** Cron führt Compile, Lint, Backup und Digest
  aus, und die Pipeline weicht auf ein anderes LLM aus, wenn eines nicht verfügbar ist. Agents
  brauchen nur `a2kb ingest`.
- **Eine Wissensdatenbank (KB) pro Projekt.** Jede KB hat ihr eigenes Präfix und, sofern der
  Speicheranbieter das erlaubt, eigene Zugangsdaten, sodass ein Agent, der an einer KB arbeitet,
  nicht auf die anderen zugreifen kann.
- **Ein Wiki für Agents und Menschen.** Agents nutzen einen MCP-Server oder die CLI; Menschen
  lesen einfaches Markdown mit `[[wikilinks]]` in Obsidian oder einem beliebigen
  Markdown-Editor. Gespeicherte Antworten fließen in den nächsten Compile ein, sodass das Wiki
  in den Bereichen wächst, die am häufigsten abgefragt werden.
- **Ausgelegt für nicht vertrauenswürdige Inhalte und für Datenschutz.** Webseiten und
  Datei-Downloads laufen durch einen SSRF-Schutz (Video-URLs, die `yt-dlp` selbst abruft, sind
  die dokumentierte Ausnahme), `claude` und `codex` laufen ohne Tools, neue KBs lassen
  Cloud-Seitenkanäle deaktiviert, und ein vollständig lokales Setup mit Ollama wird unterstützt.

Es folgt Andrej Karpathys
[LLM-Wiki-Muster](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) und
macht daraus ein serverseitiges Werkzeug.

### Was Ihr Team im Wiki-Editor sieht

<p align="center">
  <img src="docs/assets/vault.svg" width="100%"
    alt="Illustration: eine kompilierte agent2kb-Seite, geöffnet in Obsidian, mit Frontmatter-Tags,
    dem Artikel mit Wikilinks, einer Graphansicht verwandter Seiten und einem Backlinks-Bereich">
</p>

Jede kompilierte Seite kommt als einfaches Markdown mit ihren Eigenschaften (`source`, `date`,
Tags, `source_kind`), einer Zusammenfassung, strukturierten Abschnitten und `[[wikilinks]]` an,
sodass Graphansicht, Backlinks und Tag-Suche in Obsidian sofort funktionieren, und `index.md`
beginnt mit einem Rückblick „Past 7 days“ auf das, was die KB in dieser Woche gelernt hat.
Unaufgelöste Links bleiben als Geisterknoten bestehen, bis ein späterer Compile oder `a2kb stubs`
sie füllt. *Die Illustration basiert auf dem Beispiel in
[examples/first-note.md](examples/first-note.md); wie Sie Ihren Vault verbinden, steht in
[docs/obsidian.md](docs/obsidian.md).*

Obsidian ist der Reader, den agent2kb dokumentiert und empfiehlt, weil er `[[wikilinks]]`, den
Graphen und Backlinks darstellt und mit dem Plugin Remotely Save aus S3 synchronisieren kann.
Die Dateien sind gewöhnliches Markdown, daher funktioniert jeder Editor, der sie lesen kann;
andere Werkzeuge als Obsidian brauchen einen eigenen Weg, um aus S3 zu synchronisieren, und
wurden nicht getestet.

### Was neu ist, für Sie zusammengefasst

Jeder Compile aktualisiert den Anfang von `wiki/index.md` mit einem Rückblick auf die letzten
7 Tage: einer kurzen Zusammenfassung der Themen, die die neuen Seiten gemeinsam haben, vom LLM
aus diesen Seiten geschrieben, gefolgt von Links zu jeder einzelnen. Niemand muss ihn schreiben,
und er hält mit der KB Schritt, sodass eine Kollegin oder ein Kollege nach dem Urlaub oder
jemand, der einmal pro Woche hineinschaut, auf einen Blick sieht, was die KB gelernt hat. Ein
Beispiel (fiktiver Inhalt):

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

Der Zeitraum ist eine Einstellung (`recent_days`: `30` für einen Monatsrückblick, `0` zum
Abschalten), und ein optionaler wöchentlicher Digest schreibt einen ausführlicheren Rückblick
nach `outputs/weekly/`.

### Jedes Gerät und jeder Agent, eine KB

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/devices-dark.svg">
  <img src="docs/assets/devices-light.svg" width="100%"
    alt="Eine KB in Ihrem S3-Bucket, geteilt von Menschen auf Desktop, iPhone und Android über
    Obsidian und von Agents über die CLI und MCP; daneben läuft der Compile nach Zeitplan">
</picture>

Die KB liegt in Ihrem S3-Bucket, nicht auf einem einzelnen Rechner, daher erreicht dasselbe
kompilierte Wiki jedes Gerät: Obsidian unter macOS, Windows und Linux, auf iPhone und iPad und
auf Android (über das Plugin Remotely Save), und Agents auf jedem Laptop, Server oder CI-Runner
(über die CLI oder MCP). Der Compile läuft nach Zeitplan neben dem Speicher, sodass sich die
Seiten weiter verbessern, während alle Geräte ausgeschaltet sind. Smartphones brauchen einen
HTTPS-Endpunkt (AWS S3, Cloudflare R2 oder das Docker-Backend mit seinem HTTPS-Profil); siehe
[docs/obsidian.md](docs/obsidian.md). Der Maintainer hat dieses Setup mit früheren Versionen
mit Obsidian auf mehreren Desktop- und Mobilgeräten gegen ein selbst gehostetes
SeaweedFS-Backend betrieben; die Anleitungen der aktuellen Version wurden noch nicht erneut
durchgängig ausgeführt.

## Ausprobieren

Sobald die unten genannten Voraussetzungen erfüllt sind, dauert die erste Seite wenige Minuten;
das Anlegen eines Cloud-Kontos, eines Buckets und von Schlüsseln dauert länger und ist unter
[Speicheranbieter](docs/storage-providers.md) beschrieben.

Voraussetzungen: Linux oder macOS mit einer POSIX-Shell (die Plattformen, die das Paket angibt;
Windows ist ungetestet), Python 3.10 oder neuer mit dem Modul `venv` (unter Debian und Ubuntu:
`sudo apt install python3-venv`) sowie `git`. Dazu:

- **Speicher:** ein S3-kompatibler Bucket und ein Schlüssel dafür (Anleitungen für AWS S3 und
  Cloudflare R2: [Speicheranbieter](docs/storage-providers.md)) oder das
  [Docker-Backend](#docker-backend-in-10-minuten), das Speicher mitbringt.
- **Ein LLM:** die CLI `claude` oder `codex`, installiert und angemeldet
  (`npm i -g @anthropic-ai/claude-code` und `claude auth login` oder `npm i -g @openai/codex`
  und `codex login`); oder ein API-Schlüssel (Gemini, Groq, OpenRouter); oder ein lokaler
  Ollama-Server.

### Installation

Installieren Sie in eine virtuelle Umgebung, damit agent2kb und seine Abhängigkeiten vom
System-Python getrennt bleiben (viele Linux-Distributionen lehnen ein systemweites
`pip install` mit dem Fehler `externally-managed-environment` ab):

```bash
git clone https://github.com/jcaol/agent2kb.git && cd agent2kb
python3 -m venv .venv                       # einmalig: eine isolierte Umgebung in .venv/
source .venv/bin/activate                   # in jedem neuen Terminal (zsh und bash)
python -m pip install --upgrade pip
python -m pip install ".[all]"              # Installation aus dem Quellcode bis zum ersten PyPI-Release
a2kb --help                                 # sollte die Befehlsliste ausgeben
```

`python -m pip` installiert in die aktive Umgebung, egal welches `pip` in Ihrem `PATH` zuerst
steht. Führen Sie in einem späteren Terminal vor der Verwendung von `a2kb` erneut
`source .venv/bin/activate` im Checkout aus, oder rufen Sie `.venv/bin/a2kb` direkt auf (zum
Beispiel aus Cron). `deactivate` verlässt die Umgebung.

`[all]` installiert alle Provider-SDKs, die Erfassung von Dokumenten und Videos sowie den
MCP-Server. Für Text, Markdown und Webseiten mit einer Anmeldung bei der CLI `claude` oder
`codex` genügt eine kleinere Installation:

```bash
python -m pip install .                     # nur der Kern (geprüft: installiert, `a2kb --help` läuft)
python -m pip install ".[mcp]"              # Kern plus der MCP-Server für Agents
```

Was jede Funktion zusätzlich zur Kerninstallation braucht (`a2kb doctor` listet auf, was
fehlt):

| Funktion | Python-Extra | Externes Programm |
|---|---|---|
| Markdown, Text, Webseiten | keins | keins |
| CLI `claude` / `codex` als LLM | keins | die CLI, angemeldet |
| Gemini, Groq, OpenRouter, Ollama | `gemini`, `groq`, `openai`, `ollama` | ein Ollama-Server, für Ollama |
| Embeddings und Rerank (optional) | `voyage`, `openai`, `gemini` oder `ollama`; `fast` für schnellere Vektorsuche | keins |
| Dokumente `.docx`, `.xlsx` | `media` | keins |
| PDF | `media` | keins |
| Andere Dokumentformate (zum Beispiel `.epub`) | keins | `pandoc` |
| Video-URLs (YouTube und andere) | `media` (yt-dlp) | `ffmpeg` |
| Lokale Audio- und Videotranskription | `whisper` (umfangreich; **nicht** Teil von `all`) | `ffmpeg` |
| MCP-Server (`a2kb mcp`) | `mcp` | keins |

Extras lassen sich kombinieren: `python -m pip install ".[mcp,media]"`. Python-Pakete
installieren nie `ffmpeg` oder `pandoc`; verwenden Sie dafür den Paketmanager Ihres Systems
(`apt install ffmpeg pandoc`, `brew install ffmpeg pandoc`).

### Die erste KB

```bash
a2kb setup                        # Assistent: Speicher, LLM, Sprache, Datenschutz, Ihre erste KB (notes)
a2kb doctor                       # prüft Konfiguration, S3-Zugriff, LLM-Anmeldung, externe Tools

a2kb ingest --file examples/first-note.md       # ein kleines Beispiel, das im Repo mitgeliefert wird
a2kb compile                      # raw/ -> wiki/ (inkrementell; --force kompiliert alles neu)
a2kb query "What is the codename of the alpha project?"
```

Das [Tutorial für den ersten Start](docs/first-run.md) geht dieselben Schritte mit der jeweils
erwarteten Ausgabe durch (Name der Rohdatei, Wiki-Seite, Antwort mit Quellenangabe), verbindet
Obsidian und enthält eine Tabelle zur Behebung der üblichen Probleme. Um eigene Quellen zu
erfassen, verwenden Sie `a2kb ingest --url <url>`, `--file <path>` oder `--folder <dir>`.

Wenn Sie einen Befehl ausführen, bevor eine KB existiert, bietet `a2kb` an, `a2kb setup` für Sie
zu starten (nur in einem Terminal; Skripte, Cron und Agents erhalten die übliche Fehlermeldung).

`a2kb doctor` meldet eine fehlende Anmeldung bei `claude` oder `codex` als Fehler. Das ist zu
erwarten, bis Sie sich angemeldet haben; API-Schlüssel und Ollama erfordern keine Anmeldung.
Solange Sie genau eine KB haben, kann die Option `-p` entfallen; bei mehreren KBs verwenden Sie
`a2kb -p notes ...`. `a2kb list` zeigt Ihre KBs, und `a2kb new myproject --here` schreibt einen
Verweis `.kb/config.yaml`, sodass `a2kb` in einem Projektordner ohne `-p` funktioniert. Das
Paket heißt `agent2kb`, und der einzige Befehl, den es installiert, ist `a2kb` (ein
`alias kb=a2kb` kann eine kürzere Form bereitstellen).

## Warum agent2kb

Agent-Teams sammeln Wissen in Chat-Verläufen, Notizzetteln und Browser-Tabs. Alles in einen
Vektorspeicher zu legen, liefert Fragmente statt Verständnis; ein einzelner gemeinsamer Vault
vermischt Projekte und hat keinen Prozess, der ihn konsistent hält; und keiner der beiden Ansätze
gibt den Menschen, die sich auf das Wissen verlassen, eine Möglichkeit, es zu prüfen.

agent2kb teilt die Arbeit in drei Stufen. Das **Sammeln** ist bewusst einfach: Jeder Prozess
kann `ingest` ausführen, um eine Quelle in `raw/` abzulegen. Das **Verstehen** ist der
Compile-Schritt: Ein LLM macht aus den Quellen Seiten mit Titeln, Zusammenfassungen, Tags und
Links, schreibt nur neu, was sich geändert hat, und kennzeichnet, was es selbst abgeleitet hat,
damit Rohquellen bei Widersprüchen Vorrang haben. Die **Nutzung** erfolgt über zwei
Schnittstellen auf denselben Dateien: `a2kb query` und MCP für Agents, Obsidian für Menschen.
Alles wird als Markdown in Ihrem eigenen Bucket gespeichert, sodass es gelesen, verglichen,
gesichert und von Hand bearbeitet werden kann.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pillars-dark.svg">
  <img src="docs/assets/pillars-light.svg" width="100%"
    alt="Was agent2kb auszeichnet: 1. kompiliert, statt nur abzurufen; 2. eine isolierte KB pro
    Projekt; 3. Agents und Menschen, ein Wiki; 4. Wissen, das sich aufbaut; 5. Ihr Bucket, kein
    Lock-in; 6. sicher mit nicht vertrauenswürdigen Inhalten">
</picture>

### Der Vergleich

| | Vektorspeicher / RAG | Gemeinsamer Notiz-Vault | Session-Memory von Agents | **agent2kb** |
|---|:---:|:---:|:---:|:---:|
| Lesbare Seiten statt Fragmente | ❌ Chunks | ✅ handgeschrieben | ❌ Transkripte | ✅ **vom LLM kompiliert** |
| Ohne Handarbeit konsistent gehalten | ❌ | ❌ | ❌ | ✅ **inkrementeller Compile + Lint** |
| Getrenntes, isoliertes Wissen pro Projekt | ⚠️ pro Index | ❌ ein Vault | ⚠️ pro Agent | ✅ **ein Präfix und Schlüssel pro KB** |
| Agents und Menschen nutzen dieselbe Quelle | ❌ nur Agents | ⚠️ nur Menschen | ❌ nur Agents | ✅ **MCP + Markdown-Editor** |
| Jede Antwort nennt ihre Quellen | ⚠️ | ❌ | ❌ | ✅ |
| Einfache Dateien in Ihrem eigenen Speicher | ❌ | ✅ | ❌ | ✅ **beliebiger S3-Bucket** |
| Kann ganz ohne Dritte laufen | ⚠️ | ✅ | ❌ | ✅ **Ollama + lokales S3** |

Jede Datei trägt YAML-Frontmatter (`title`, `source`, `date`, `tags`, `source_kind`).
`source_kind` hält die Vertrauensstufe fest: Rohquellen und kuratierte Themen sind primär;
Konzeptseiten und gespeicherte Antworten sind abgeleitet. Siehe
[docs/compounding-loop.md](docs/compounding-loop.md).

## Funktionen

| | |
|---|---|
| **Erfassung** | URLs, YouTube und andere Videos, Audio, PDF/DOCX/XLSX, Bilder (OCR), Markdown, Clippings aus dem Obsidian Web Clipper; Duplikaterkennung, Sicherheitsprüfung vor dem Abruf |
| **Compile** | Inkrementeller Compile von raw nach wiki mit `[[wikilinks]]`, Konzeptseiten und Stubs für Geisterlinks; ein Index, der mit einem Rückblick „Past 7 days“ beginnt, und ein optionaler wöchentlicher Digest; Ausgabe auf Englisch oder in jeder Sprache, die Sie angeben |
| **Abfrage** | Stichwortsuche mit optionalen Embeddings und Reranking, Antworten mit Quellenangaben und Erweiterung der Suchbegriffe; gespeicherte Antworten in `outputs/` schließen den Kreislauf, in dem sich Wissen aufbaut |
| **Agent-Zugriff** | MCP-Server (`a2kb mcp`, stdio oder per Bearer-Token geschütztes HTTP), standardmäßig schreibgeschützt; `ask` und `ingest_url` sind Opt-in |
| **Isolation** | Präfixe pro KB, vom Client durchgesetzt; Zugangsdaten pro KB, wo der Anbieter sie einschränken kann |
| **LLMs** | Anmeldungen bei den CLIs `claude` / `codex` (in einer Sandbox, mit einer Fallback-CLI), Gemini, Groq, OpenRouter, Ollama; separates Modell für den Massen-Compile und für Hilfsaufgaben |
| **Betrieb** | `a2kb doctor`, `lint`, `backup` und `restore`, eine Laufzusammenfassung für jeden Compile, Cron-Wrapper in `scripts/` |
| **Docker-Backend** | Ein einziges `docker compose up`: SeaweedFS mit Schlüsseln pro KB, Cron, ein MCP-Server pro KB, optional HTTPS |

## Schnellstart (lokal)

[Ausprobieren](#ausprobieren) deckt den kompletten Weg ab, und das
[Tutorial für den ersten Start](docs/first-run.md) zeigt das erwartete Ergebnis jedes Schritts.
Weitere Hinweise:

- `a2kb setup --non-interactive` übernimmt alles aus Flags und `KB_*`-Variablen, für CI und
  Provisionierung ([Flags](docs/configuration.md#non-interactive-setup)).
- Speicher-Voreinstellungen: AWS S3, Cloudflare R2, Backblaze B2, MinIO, SeaweedFS, Wasabi,
  andere ([Speicheranbieter](docs/storage-providers.md)).
- Compile, Lint, Backup und Stubs laufen nach Zeitplan: siehe
  [Das Wiki automatisch kompilieren](#das-wiki-automatisch-kompilieren).
- Alle Befehle und Flags: [docs/commands.md](docs/commands.md).

## Das Wiki automatisch kompilieren

`a2kb ingest` legt Quellen nur in `raw/` ab; Seiten entstehen, wenn `a2kb compile` läuft. Sie
können den Befehl von Hand ausführen, aber im üblichen Setup läuft er **nach Zeitplan**, sodass
alles, was ein Agent oder ein Mensch erfasst, zu einer Seite wird, ohne dass jemand darauf
warten muss:

| Wie Sie agent2kb betreiben | Wer den Compile plant |
|---|---|
| Docker-Backend | Wird für Sie erledigt: Compile alle 15 Minuten, Lint täglich, Backup zweimal täglich, Stubs täglich (`KB_CRON_DIGEST=1` ergänzt einen wöchentlichen Digest) |
| Native Installation (Linux, macOS) | Sie fügen einen Cron-Eintrag hinzu und verwenden die Wrapper in `scripts/` |
| Gelegentliche Nutzung | Führen Sie `a2kb compile` nach dem Erfassen selbst aus |

Eine minimale Crontab für eine native Installation (`crontab -e`; passen Sie die Pfade an):

```cron
# alle 15 Minuten: neue oder geänderte Quellen kompilieren (keine LLM-Nutzung, wenn sich nichts geändert hat)
*/15 * * * * /path/to/agent2kb/scripts/kb-cron-compile.sh /path/to/project notes
# täglich: strukturelles Lint und Platzhalterseiten für oft verlinkte, fehlende Themen
0 9 * * *    /path/to/agent2kb/scripts/kb-cron-lint.sh  /path/to/project notes
0 3 * * *    /path/to/agent2kb/scripts/kb-cron-stubs.sh /path/to/project notes
# zweimal täglich: lokales Backup-Archiv der KB
30 2,14 * * * /path/to/agent2kb/scripts/kb-cron-backup.sh /path/to/project notes
```

Die Wrapper protokollieren nach `kb-*.log` im Projektordner und rufen Compile mit `--strict`
auf, sodass ein fehlgeschlagener Compile für das Monitoring mit einem Exit-Code ungleich null
endet. Überlappende Läufe sind unbedenklich: Solange ein Compile einer KB seinen Lease im
Speicher hält, überspringt ein weiterer Lauf derselben KB diesen Takt (Exit-Code 75 unter
`--strict`), und der nächste Takt übernimmt die Arbeit. Die vollständige Vorlage ist
`scripts/kb-crontab.example`; Details zur Zeitplanung stehen in
[docs/project-workflow.md](docs/project-workflow.md#cron).

**Welches LLM kompiliert, ist eine Einstellung, nicht Teil des Zeitplans.** Cron führt nur
`a2kb compile` aus; der Block `llm:` der KB entscheidet, wer die Arbeit erledigt:

| `llm.provider` | Was kompiliert | Kosten und Datenschutz |
|---|---|---|
| `cli` mit `command: claude` oder `codex` | Die CLI von Claude Code oder Codex, bei der Sie angemeldet sind, ohne Tools in einer Sandbox ausgeführt | Nutzt Ihr Abonnement; Inhalte gehen an diesen Anbieter |
| `ollama` | Ein lokales Ollama-Modell (`model: llama3.3:70b` oder ein anderes Modell, das Sie heruntergeladen haben) | Vollständig lokal; braucht einen Rechner, der das Modell ausführen kann |
| `gemini`, `groq`, `openrouter` | Die API dieses Anbieters mit Ihrem Schlüssel | Kostenpflichtige (oder kostenlose) API-Nutzung; Inhalte gehen an diesen Anbieter |

Mit `provider: cli` benennt `fallback_command` eine zweite CLI (zum Beispiel `codex` hinter
`claude`), die für den Rest des Laufs übernimmt, wenn die erste kein Guthaben mehr hat oder
abgemeldet ist. `model` und `aux_model` wählen die Modelle für die Seitenkompilierung und für
die leichteren Aufgaben (Konzeptseiten, Lint-Berichte, Digest); beide verwenden standardmäßig
die Empfehlung des Anbieters. Details: [docs/llm-providers.md](docs/llm-providers.md).

## Einstellungen und Anpassung

Alles wird in einer einzigen Datei auf Benutzerebene festgelegt, `~/.kb/config.yaml`
(Modus 0600; `a2kb setup` schreibt sie, und `KB_CONFIG` kann auf einen anderen Ort zeigen).
Einstellungen unter `defaults:` gelten für jede KB; dieselben Schlüssel unter einer einzelnen KB
überschreiben sie nur für diese KB:

```yaml
version: 2
storage: { bucket: my-kb, region: auto, endpoint: "https://<account>.r2.cloudflarestorage.com" }
llm:
  provider: cli
  command: claude
  fallback_command: codex
defaults:
  output_language: en          # standardmäßig Englisch; jeder Sprachname funktioniert
  privacy_mode: false          # true: beim Erfassen überhaupt keine Cloud-Aufrufe
  allow_jina_reader: false     # true: schwer lesbare Webseiten werden über jina.ai abgerufen
  allow_cloud_vision: false    # true: Bilder werden zur OCR an ein Cloud-Modell gesendet
kbs:
  notes: {}                    # verwendet die Standardwerte
  research:
    output_language: Spanish   # diese KB schreibt ihre Seiten und Antworten auf Spanisch
  client-work:
    llm: { provider: cli, command: codex }   # diese KB wird stattdessen von der Codex-CLI kompiliert
```

Am schnellsten starten Sie mit der CLI `claude` oder `codex`, bei der Sie bereits angemeldet
sind: kein API-Schlüssel und kein zusätzlicher Dienst. Für eine KB, deren Inhalte Ihren Rechner
nie verlassen dürfen, setzen Sie `llm: { provider: ollama }` und betreiben ein lokales Modell
mit Ollama (dafür braucht es einen Rechner, der das Modell ausführen kann; siehe
[LLM-Anbieter](docs/llm-providers.md#ollama)).

| Was ändern | Einstellung | Mehr |
|---|---|---|
| Sprache der Seiten und Antworten | `output_language` (pro KB oder in `defaults`) | [Ausgabesprache](docs/configuration.md#output-language) |
| Der Rückblick „Past N days“ im Index | `recent_days` (standardmäßig 7, `0` = aus) | [Rückblick auf neue Seiten](docs/configuration.md#recent-pages-recap) |
| Welches LLM kompiliert und antwortet | der Block `llm:` (auch pro KB) | [LLM-Anbieter](docs/llm-providers.md) |
| Was Ihr Netzwerk verlassen darf | `privacy_mode`, `allow_jina_reader`, `allow_cloud_vision` | [Datenschutz](docs/privacy.md) |
| Wo eine KB gespeichert wird | `storage:` (gemeinsam) oder ein eigenes `storage:` und `prefix` der KB | [Speicheranbieter](docs/storage-providers.md) |
| Welche KB ein Ordner verwendet | `.kb/config.yaml` mit `kb: <name>` in diesem Ordner (`a2kb new <name> --here`) | [Konfiguration](docs/configuration.md#using-a-kb-from-a-project-folder) |
| Eine einmalige Abweichung | `KB_*`-Umgebungsvariablen, zum Beispiel `KB_OUTPUT_LANGUAGE=ja` | [Umgebungsvariablen](docs/configuration.md#environment-variables) |

`a2kb config show` gibt den wirksamen Wert jeder Einstellung aus und aus welcher Datei oder
Variable er stammt; `a2kb doctor` prüft, ob das Ergebnis funktioniert. Geheimnisse
(Speicherschlüssel, API-Schlüssel) gehören nur in diese Datei auf Benutzerebene oder in
Umgebungsvariablen, nie in einen Projektordner.

## Docker-Backend in 10 Minuten

```bash
cp .env.example .env                  # benennen Sie Ihre KBs (KB_NAMES=research,work), wählen Sie ein LLM
docker compose up -d                  # SeaweedFS (S3) + Cron-Jobs + MCP-Server; Schlüssel werden erzeugt
docker compose exec kb claude auth login                                # einmalig, wenn KB_LLM=claude
docker compose exec kb a2kb connect obsidian research --show-secret   # Einstellungen für Remotely Save
docker compose exec kb a2kb connect agent research --show-secret      # `claude mcp add ...` mit URL und Token
```

Das erzeugt S3-Speicher mit einem separaten, eingeschränkten Schlüssel pro KB, den Bucket, die
Konfiguration, einen Cron-Zeitplan und einen schreibgeschützten MCP-Server pro KB. Setzen Sie
`DOMAIN=kb.example.com` und verwenden Sie `--profile https`, um automatisch HTTPS zu erhalten
(für Obsidian auf dem Smartphone und für entfernte Agents), oder konfigurieren Sie stattdessen
Ihren eigenen S3-Speicher. Die Compose-Dienste heißen `kb` und `kb-mcp`; der Befehl darin ist
`a2kb`. Die Schritt-für-Schritt-Anleitung, die Schlüsselrotation und das Testprotokoll stehen in
[docs/docker.md](docs/docker.md).

## Obsidian verbinden

```bash
a2kb connect obsidian notes                # Einstellungen für Remotely Save für die KB notes
a2kb connect obsidian notes --show-secret  # dasselbe, einschließlich des geheimen Schlüssels
```

Das funktioniert mit jedem Setup: AWS S3, Cloudflare R2, andere Anbieter und das Docker-Backend
(dort: `docker compose exec kb a2kb connect obsidian <kb>`). Der Befehl gibt Endpunkt, Region,
Bucket, URL-Stil und das entfernte Präfix der KB (zum Beispiel `kbs/notes/`) für das
Community-Plugin **Remotely Save** („S3 or compatible“) aus. Mit gesetztem Präfix ist das
Vault-Stammverzeichnis die KB: `raw/`, `wiki/` und `outputs/` erscheinen als Ordner. Für eine
KB, die die AWS-Credential-Chain verwendet, zeigt die Übersicht Platzhalter für den Schlüssel,
weil ein Gerät seinen eigenen Schlüssel bekommen sollte, nie die AWS-Anmeldung des Rechners.

Wählen Sie den Schlüssel für das Plugin danach, was das Gerät tut:

- **Nur ansehen:** ein schreibgeschützter Schlüssel (auflisten und lesen) und eine
  Synchronisierungsrichtung, die nur herunterlädt.
- **Bearbeiten und Web Clipper:** Die Zwei-Wege-Synchronisierung lädt Dateien hoch und löscht
  sie, daher braucht sie einen Schlüssel, der im Präfix der KB auflisten, lesen, schreiben und
  löschen darf: einen separaten Schlüssel für das Gerät, nicht den, den agent2kb verwendet, damit
  er einzeln widerrufen werden kann.

Lassen Sie das Verschlüsselungspasswort des Plugins für die KB leer (agent2kb liest einfaches
Markdown) und beginnen Sie mit einem neuen, leeren Vault. Compile und Synchronisierung laufen
nach getrennten Uhren: Eine Seite erreicht das Gerät bei der ersten Synchronisierung nach dem
Compile, und synchronisiert wird nur, solange Obsidian geöffnet ist. Die vollständige Anleitung,
einschließlich beider Varianten, der Ordnerstruktur und einer Prüfung der ersten
Synchronisierung, ist [docs/obsidian.md](docs/obsidian.md).

Unaufgelöste `[[wikilinks]]` sind beabsichtigte Geisterknoten, die ein späterer Compile
auflöst; `assets/obsidian-snippets/dim-ghost-links.css` blendet sie ab, und `a2kb stubs` kann
Platzhalterseiten für die am häufigsten referenzierten anlegen.

## Agents verbinden (MCP)

```bash
claude mcp add kb-notes -- a2kb -p notes mcp      # schreibgeschützt: search, read_page, list_recent, kb_info
```

`--allow-llm` fügt das Tool `ask` hinzu und `--allow-write` fügt `ingest_url` hinzu; der
HTTP-Modus erfordert ein Bearer-Token. Konfigurationsbeispiele für Codex und Claude Desktop
stehen in [docs/mcp.md](docs/mcp.md). Um einem Agent vorzugeben, wann er die KB konsultieren
soll, schreibt `a2kb scaffold rules --target <file>` einen Abschnitt „KB query discipline“ in
seine Anweisungsdatei (zum Beispiel `AGENTS.md`).

## Vergleich mit anderen Ansätzen

- **Reine Obsidian-Synchronisierung.** Das Synchronisieren eines Vaults liefert Speicher und
  einen Reader, aber nichts schreibt die Seiten, und alle Projekte teilen sich einen Ordner.
  agent2kb ergänzt den Compiler, die Isolation pro Projekt und den unbeaufsichtigten Betrieb,
  während Obsidian der Reader bleibt. Für einen einzelnen lokalen Vault, den Sie aus Ihrem Editor
  heraus abfragen möchten, ist ein Plugin, das im Vault läuft, die einfachere Wahl.
- **Vektor-RAG-Dienste.** Diese liefern die Textfragmente, die einer Frage am nächsten kommen.
  agent2kb kompiliert Quellen zu lesbaren Seiten mit Links und Herkunftsangaben, sodass das
  Ergebnis durchsucht, korrigiert und verglichen werden kann, und die Suche läuft über diese
  Seiten. Embeddings sind optional.
- **Session-Memory-Werkzeuge für Agents.** Diese zeichnen auf, was ein Agent getan und gesagt
  hat. agent2kb kompiliert externe Quellen und die Antworten, die Sie speichern möchten, zu einem
  gemeinsamen Referenz-Wiki. Die beiden Ansätze ergänzen sich: Session-Memory beantwortet „was
  wurde versucht“, und agent2kb beantwortet „was ist bekannt“.

## Sicherheit und Datenschutz

- Compile und Abfrage senden Seitentext an das konfigurierte LLM, und ein kostenloser
  Gemini-Tarif darf Prompts zum Training verwenden. Neue KBs lassen Jina Reader und Cloud-OCR für
  Bilder **deaktiviert**, und jeder Compile und jede Erfassung gibt ein Banner aus, das den
  tatsächlich verwendeten Anbieter nennt. Für sensible Inhalte verwenden Sie
  `llm.provider: ollama`. Details: [docs/privacy.md](docs/privacy.md).
- Webseiten und Datei-Downloads laufen durch einen SSRF-Schutz, der private Adressen blockiert,
  jede Weiterleitung prüft und die Verbindung festpinnt. **Video-URLs sind nicht abgedeckt:**
  `yt-dlp` löst Namen auf und folgt Weiterleitungen selbst, erfassen Sie daher keine Video-URLs
  von nicht vertrauenswürdigen Hosts auf einem Rechner, der interne Dienste erreichen kann. Über
  MCP werden Video-URLs nur von YouTube oder von Hosts aus `--ingest-allow-domain` angenommen.
  Details: [docs/privacy.md](docs/privacy.md#video-urls-yt-dlp) und
  [docs/mcp.md](docs/mcp.md#fetch-safety-ssrf).
- `claude` und `codex` laufen ohne Tools und ohne MCP.
- Die Isolation der KBs hat zwei Ebenen: eine Präfixprüfung in agent2kb und Zugangsdaten pro KB,
  die vom Speicheranbieter eingeschränkt werden. Nur die zweite Ebene schützt vor einem
  geleakten Schlüssel. **SeaweedFS-Versionen vor 4.34 sind von CVE-2026-55874 betroffen**, das es
  einem KB-Schlüssel erlaubte, Objekte einer anderen KB zu kopieren; das Docker-Backend fixiert
  Version 4.48. Siehe [docs/storage-providers.md](docs/storage-providers.md#seaweedfs).
- Melden Sie Sicherheitslücken vertraulich: [SECURITY.md](SECURITY.md).

## FAQ

**Werden meine Daten zum Training verwendet?** agent2kb hat keine Telemetrie und sendet Daten
nur an den Speicher und das LLM, die Sie konfigurieren. Ob ein LLM-Anbieter mit Prompts
trainiert, bestimmt dessen Richtlinie: Ein kostenloser Gemini-Tarif darf das, ein lokaler
Ollama-Server dagegen behält die Daten auf Ihrem Rechner. Sie bleiben dafür verantwortlich, die
Rechte an den Inhalten zu besitzen, die Sie erfassen.

**Welche LLMs werden unterstützt?** Eine angemeldete CLI `claude` oder `codex` (kein
API-Schlüssel erforderlich), Gemini, Groq, OpenRouter und Ollama. Siehe
[docs/llm-providers.md](docs/llm-providers.md).

**Kann es vollständig lokal laufen?** Ja. Verwenden Sie Ollama für Compile und Embeddings,
SeaweedFS oder MinIO als Speicher und aktivieren Sie `privacy_mode`. Ihre Inhalte werden dann
nie an Dritte gesendet.

**Braucht es eine GPU?** Nein. Nur ein lokales Ollama-Modell profitiert davon, denn die
Kompilierung über eine CLI oder eine API läuft entfernt. Lokale Audiotranskription (das Extra
`whisper`) ist optional.

**Können Menschen Seiten bearbeiten?** Ja, in Obsidian oder einem beliebigen Markdown-Editor.
Eine Seite wird nur neu erzeugt, wenn ihre Rohquelle neu kompiliert wird. Für handgeschriebene
Seiten, die nie neu erzeugt werden dürfen, verwenden Sie `a2kb promote --as wiki-topic`.

**Welche Sprachen werden unterstützt?** Quellen können in jeder Sprache vorliegen. Seiten und
Antworten werden auf Englisch geschrieben, es sei denn, Sie setzen `output_language` auf eine
andere Sprache, für eine KB oder für alle.

**Wie sehe ich, was neu ist?** Öffnen Sie `wiki/index.md`: Die Datei beginnt mit einem
automatischen Rückblick auf die letzten 7 Tage (siehe
[das Beispiel](#was-neu-ist-für-sie-zusammengefasst)). Setzen Sie `recent_days` pro KB, um den
Zeitraum zu ändern, oder `0`, um ihn abzuschalten. Der optionale wöchentliche Digest
(`scripts/kb-cron-digest.sh` oder `KB_CRON_DIGEST=1` in Docker) schreibt einen ausführlicheren
Rückblick auf die Woche nach `outputs/weekly/`.

## Roadmap

agent2kb ist eine Version vor 1.0. Die CLI, die Konfigurationsformate und die S3-Struktur sind
durch Contract-Tests festgeschrieben, aber mit einigen Ecken und Kanten ist zu rechnen.

- Das Paket auf PyPI veröffentlichen (bis dahin aus dem Quellcode installieren).
- Weitere Speicheranbieter verifizieren: Nur SeaweedFS wurde gegen einen echten Server getestet;
  R2, B2, MinIO, AWS und Wasabi folgen ihrer Dokumentation.
- Den vollständigen Ablauf mit `docker compose` und eine Obsidian-Synchronisierung auf mehreren
  Geräten für diese Version erneut durchführen, einschließlich echter HTTPS-Zertifikate (beides
  wurde mit früheren Versionen verwendet).
- Nicht geplant: eine Weboberfläche (gelesen wird über Obsidian, die CLI oder MCP) und
  Session-Memory für Agents.

## Dokumentation

| | |
|---|---|
| [docs/first-run.md](docs/first-run.md) | Erster Start mit einer Beispieldatei: Erfassung, Compile, Abfrage, Obsidian, Fehlerbehebung |
| [docs/obsidian.md](docs/obsidian.md) | Obsidian und Remotely Save: Schlüssel, Synchronisierungsrichtung, Verschlüsselung, Timing |
| [docs/configuration.md](docs/configuration.md) | Konfigurationsdatei, KBs, Admin-Rolle, `KB_*`-Variablen, Ausgabesprache |
| [docs/commands.md](docs/commands.md) | Alle Befehle und Flags |
| [docs/llm-providers.md](docs/llm-providers.md) | Anbieter, CLI-Sandboxing, Routing und Fallback |
| [docs/storage-providers.md](docs/storage-providers.md) | Anleitungen für AWS S3 und R2, B2, MinIO, SeaweedFS, Wasabi, Isolation, häufige Fehler |
| [docs/docker.md](docs/docker.md) | Das Docker-Backend: Compose, Obsidian, Agents, HTTPS, Schlüsselrotation |
| [docs/mcp.md](docs/mcp.md) | MCP-Server, Tools, HTTP-Modus, Sicherheitsmodell |
| [docs/privacy.md](docs/privacy.md) | Was jede Stufe an welchen Dritten sendet |
| [docs/project-workflow.md](docs/project-workflow.md) | Tägliche Nutzung, `source_meta`, Cron |
| [docs/compounding-loop.md](docs/compounding-loop.md) | Wie gespeicherte Antworten in den nächsten Compile einfließen |
| [docs/operations.md](docs/operations.md) | Backup, Manifest, Laufzusammenfassungen, Lint-Hygiene |
| [CHANGELOG.md](CHANGELOG.md) | Releases und Hinweise zum Upgrade |

## Mitwirken

In [CONTRIBUTING.md](CONTRIBUTING.md) finden Sie Setup, den Kompatibilitätsvertrag und die
Code-Regeln; Coding-Agents sollten [CLAUDE.md](CLAUDE.md) lesen. Bitte beachten Sie den
[Verhaltenskodex](CODE_OF_CONDUCT.md).

## Marken

agent2kb ist ein unabhängiges Open-Source-Projekt. Es ist weder mit Obsidian, Anthropic, OpenAI,
Google, Amazon Web Services, Cloudflare noch mit einem anderen in diesem Repository genannten
Unternehmen oder Produkt verbunden, wird von ihnen nicht unterstützt und nicht gesponsert. Alle
Produktnamen, Logos und Marken sind Eigentum ihrer jeweiligen Inhaber und werden nur zur
Beschreibung der Kompatibilität verwendet.

## Lizenz

agent2kb steht unter der [GNU Affero General Public License v3.0](LICENSE)
(`AGPL-3.0-only`). Sie dürfen es unter diesen Bedingungen nutzen, verändern und weitergeben;
wenn Sie es verändern und anderen die Nutzung über ein Netzwerk ermöglichen (zum Beispiel als
gehosteten Dienst oder als gemeinsam genutzten MCP-Server), verpflichtet Sie die AGPL, diesen
Nutzern den Quellcode Ihrer veränderten Version anzubieten.

**Kommerzielle Lizenz.** Wenn die Bedingungen der AGPL nicht zu Ihrer Nutzung passen, zum
Beispiel beim Einbetten von agent2kb in ein Closed-Source-Produkt oder einen Closed-Source-Dienst,
ist bei den Maintainern möglicherweise eine separate kommerzielle Lizenz erhältlich: siehe
[agent2kb.com](https://agent2kb.com) oder schreiben Sie an
[licensing@agent2kb.com](mailto:licensing@agent2kb.com).

Beiträge werden unter dem [Contributor Licence Agreement](CLA.md) angenommen, das es dem
Projekt ermöglicht, beide Lizenzen anzubieten.
