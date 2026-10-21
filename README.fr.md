<p align="center">
  <a href="https://agent2kb.com"><img src="docs/assets/hero.svg" width="100%"
    alt="agent2kb : le compilateur de connaissances pour les équipes d'agents"></a>
</p>

<p align="center">
  <b>agent2kb transforme tout ce que trouvent vos agents en un wiki Markdown vivant et sourcé.</b><br>
  Un LLM le compile dans votre propre bucket ; les agents le lisent via MCP,
  les personnes le lisent dans Obsidian ou n'importe quel éditeur Markdown.
</p>

<p align="center">
  <a href="https://agent2kb.com"><b>agent2kb.com</b></a> ·
  <a href="#essayer">Essayer</a> ·
  <a href="docs/first-run.md">Premier lancement</a> ·
  <a href="#pourquoi-agent2kb">Pourquoi agent2kb</a> ·
  <a href="#documentation">Documentation</a>
</p>

<p align="center">
  <a href="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml"><img
    src="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg"
    alt="Licence : AGPL-3.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg"
    alt="Python 3.10+"></a>
  <img src="https://img.shields.io/badge/MCP-server-6366f1.svg" alt="Serveur MCP">
  <img src="https://img.shields.io/badge/storage-any%20S3-0d9488.svg" alt="Tout stockage S3">
  <img src="https://img.shields.io/badge/runs-fully%20local%20(Ollama)-c2410c.svg"
    alt="Fonctionne entièrement en local avec Ollama">
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.de.md">Deutsch</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.pt-BR.md">Português (Brasil)</a> ·
  <b>Français</b> ·
  <a href="README.ko.md">한국어</a> ·
  <a href="README.zh-TW.md">繁體中文</a>
</p>

<p align="center"><i>Ceci est une traduction ; le <a href="README.md">README anglais</a> fait
référence et peut être plus récent.</i></p>
<!-- Switch on once the package is on PyPI:
[![PyPI](https://img.shields.io/pypi/v/agent2kb.svg)](https://pypi.org/project/agent2kb/)
-->

**Un compilateur de connaissances headless pour les équipes d'agents.** Les agents et les bots
soumettent des sources brutes (pages web, vidéos, PDF, notes) ; un LLM les compile en un wiki
auto-entretenu de pages Markdown reliées entre elles ; les agents le lisent via la CLI ou MCP,
et les personnes le lisent dans Obsidian ou tout autre éditeur Markdown. Chaque projet dispose
de son propre wiki isolé dans n'importe quel bucket compatible S3, et aucune application n'a
besoin de rester ouverte.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pipeline-dark.svg">
  <img src="docs/assets/pipeline-light.svg" width="100%"
    alt="Fonctionnement d'agent2kb : a2kb ingest dépose les sources dans raw/, a2kb compile
    les transforme en pages wiki/ avec un LLM, les agents lisent le wiki via la CLI et MCP, les
    personnes le lisent dans Obsidian, et les réponses enregistrées dans outputs/ alimentent la
    compilation suivante">
</picture>

<!-- TODO(demo): record a short terminal GIF (ingest -> compile -> query) and save it as
     docs/assets/demo.gif, then uncomment:
![a2kb demo](docs/assets/demo.gif)
-->

- **Ingérer une fois, compiler sans surveillance.** Cron exécute la compilation, le lint, la
  sauvegarde et le digest, et le pipeline bascule vers un autre LLM lorsque l'un d'eux est
  indisponible. Les agents n'ont besoin que de `a2kb ingest`.
- **Une base de connaissances (KB) par projet.** Chaque KB a son propre préfixe et, lorsque le
  fournisseur de stockage le permet, ses propres identifiants, de sorte qu'un agent travaillant
  sur une KB ne peut pas accéder aux autres.
- **Un seul wiki pour les agents et les personnes.** Les agents utilisent un serveur MCP ou la
  CLI ; les personnes lisent du Markdown brut avec des `[[wikilinks]]` dans Obsidian ou
  n'importe quel éditeur Markdown. Les réponses enregistrées alimentent la compilation suivante,
  si bien que le wiki s'enrichit dans les domaines les plus souvent interrogés.
- **Conçu pour les contenus non fiables et pour la confidentialité.** Les pages web et les
  téléchargements de fichiers passent par une protection contre les SSRF (les URL de vidéos, que
  `yt-dlp` récupère lui-même, constituent l'exception documentée), `claude` et `codex`
  s'exécutent sans outils, les nouvelles KB gardent les canaux annexes vers le cloud désactivés,
  et une configuration entièrement locale avec Ollama est prise en charge.

Le projet suit le
[modèle de wiki LLM](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
d'Andrej Karpathy et en fait un outil côté serveur.

### Ce que votre équipe voit dans un éditeur de wiki

<p align="center">
  <img src="docs/assets/vault.svg" width="100%"
    alt="Illustration : une page agent2kb compilée ouverte dans Obsidian, avec les tags du
    frontmatter, l'article et ses wikilinks, une vue graphe des pages liées et un panneau de
    rétroliens">
</p>

Chaque page compilée arrive sous forme de Markdown brut avec ses propriétés (`source`, `date`,
tags, `source_kind`), un résumé, des sections structurées et des `[[wikilinks]]`, de sorte que
la vue graphe, les rétroliens et la recherche par tag d'Obsidian fonctionnent immédiatement, et
`index.md` s'ouvre sur un récapitulatif « Past 7 days » de ce que la KB a appris dans la
semaine. Les liens non résolus restent des nœuds fantômes jusqu'à ce qu'une compilation
ultérieure ou `a2kb stubs` les complète. *Illustration construite à partir de l'exemple de
[examples/first-note.md](examples/first-note.md) ; voir [docs/obsidian.md](docs/obsidian.md)
pour connecter votre coffre (vault).*

Obsidian est le lecteur qu'agent2kb documente et recommande, car il affiche les `[[wikilinks]]`,
le graphe et les rétroliens, et peut se synchroniser depuis S3 avec le plugin Remotely Save. Les
fichiers sont du Markdown ordinaire, donc tout éditeur capable de les lire fonctionne ; les
outils autres qu'Obsidian nécessitent leur propre moyen de synchronisation depuis S3 et n'ont pas
été testés.

### Un récapitulatif des nouveautés, rédigé pour vous

Chaque compilation actualise le haut de `wiki/index.md` avec un récapitulatif des 7 derniers
jours : un court résumé des thèmes communs aux nouvelles pages, rédigé par le LLM à partir de
ces pages, suivi de liens vers chacune d'elles. Personne n'a besoin de l'écrire, et il suit
l'évolution de la KB ; ainsi, un collègue qui revient de congé ou un apprenant qui passe une
fois par semaine voit d'un coup d'œil ce que la KB a appris. Un exemple (contenu fictif) :

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

La fenêtre est un paramètre (`recent_days` : `30` pour un récapitulatif mensuel, `0` pour le
désactiver), et un digest hebdomadaire facultatif écrit un récapitulatif plus long dans
`outputs/weekly/`.

### Tous les appareils et tous les agents, une seule KB

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/devices-dark.svg">
  <img src="docs/assets/devices-light.svg" width="100%"
    alt="Une KB dans votre bucket S3, partagée par des personnes sur ordinateur, iPhone et
    Android via Obsidian, et par des agents via la CLI et MCP ; la compilation s'exécute à côté
    selon une planification">
</picture>

La KB réside dans votre bucket S3, et non sur une seule machine, si bien que le même wiki
compilé atteint tous les appareils : Obsidian sur macOS, Windows et Linux, sur iPhone et iPad,
et sur Android (via le plugin Remotely Save), ainsi que les agents sur n'importe quel portable,
serveur ou runner de CI (via la CLI ou MCP). La compilation s'exécute selon une planification à
côté du stockage, de sorte que les pages continuent de s'améliorer même lorsque tous les
appareils sont éteints. Les téléphones nécessitent un endpoint HTTPS (AWS S3, Cloudflare R2, ou
le backend Docker avec son profil HTTPS) ; voir [docs/obsidian.md](docs/obsidian.md). Le
mainteneur a utilisé cette configuration avec Obsidian sur plusieurs appareils de bureau et
mobiles, face à un backend SeaweedFS auto-hébergé, sur des versions antérieures ; les recettes
de la version actuelle n'ont pas encore été rejouées de bout en bout.

## Essayer

Une fois les prérequis ci-dessous en place, la première page prend quelques minutes ; la
création d'un compte cloud, d'un bucket et de clés prend plus de temps et est décrite dans
[fournisseurs de stockage](docs/storage-providers.md).

Prérequis : Linux ou macOS avec un shell POSIX (les plateformes déclarées par le paquet ;
Windows n'est pas testé), Python 3.10 ou ultérieur avec son module `venv` (sous Debian et
Ubuntu : `sudo apt install python3-venv`), et `git`. Ensuite :

- **Stockage :** un bucket compatible S3 et une clé pour celui-ci (recettes AWS S3 et
  Cloudflare R2 : [fournisseurs de stockage](docs/storage-providers.md)), ou le
  [backend Docker](#backend-docker-en-10-minutes), qui inclut le stockage.
- **Un LLM :** la CLI `claude` ou `codex`, installée et connectée
  (`npm i -g @anthropic-ai/claude-code` et `claude auth login`, ou `npm i -g @openai/codex`
  et `codex login`) ; ou une clé d'API (Gemini, Groq, OpenRouter) ; ou un serveur Ollama local.

### Installation

Installez dans un environnement virtuel, afin qu'agent2kb et ses dépendances restent séparés du
Python système (de nombreuses distributions Linux refusent un `pip install` global avec une
erreur `externally-managed-environment`) :

```bash
git clone https://github.com/jcaol/agent2kb.git && cd agent2kb
python3 -m venv .venv                       # une fois : un environnement isolé dans .venv/
source .venv/bin/activate                   # dans chaque nouveau terminal (zsh et bash)
python -m pip install --upgrade pip
python -m pip install ".[all]"              # installation depuis les sources jusqu'à la première version PyPI
a2kb --help                                 # doit afficher la liste des commandes
```

`python -m pip` installe dans l'environnement actif, quel que soit le `pip` qui apparaît en
premier dans votre `PATH`. Dans un terminal ultérieur, exécutez à nouveau
`source .venv/bin/activate` dans le dépôt cloné avant d'utiliser `a2kb`, ou appelez directement
`.venv/bin/a2kb` (par exemple depuis cron). `deactivate` quitte l'environnement.

`[all]` installe tous les SDK de fournisseurs, l'ingestion de documents et de vidéos, ainsi que
le serveur MCP. Une installation plus légère suffit pour le texte, le Markdown et les pages web
avec une CLI `claude` ou `codex` connectée :

```bash
python -m pip install .                     # cœur seul (vérifié : s'installe, `a2kb --help` fonctionne)
python -m pip install ".[mcp]"              # cœur plus le serveur MCP pour les agents
```

Ce dont chaque fonctionnalité a besoin en plus de l'installation de base (`a2kb doctor` liste
ce qui manque) :

| Fonctionnalité | Extra Python | Programme externe |
|---|---|---|
| Markdown, texte, pages web | aucun | aucun |
| CLI `claude` / `codex` comme LLM | aucun | la CLI, connectée |
| Gemini, Groq, OpenRouter, Ollama | `gemini`, `groq`, `openai`, `ollama` | un serveur Ollama, pour Ollama |
| Embeddings et rerank (facultatif) | `voyage`, `openai`, `gemini` ou `ollama` ; `fast` pour une recherche vectorielle plus rapide | aucun |
| Documents `.docx`, `.xlsx` | `media` | aucun |
| PDF | `media` | aucun |
| Autres formats de documents (par exemple `.epub`) | aucun | `pandoc` |
| URL de vidéos (YouTube et autres) | `media` (yt-dlp) | `ffmpeg` |
| Transcription audio et vidéo locale | `whisper` (lourd ; **ne fait pas** partie de `all`) | `ffmpeg` |
| Serveur MCP (`a2kb mcp`) | `mcp` | aucun |

Les extras se combinent : `python -m pip install ".[mcp,media]"`. Les paquets Python
n'installent jamais `ffmpeg` ni `pandoc` ; utilisez le gestionnaire de paquets de votre système
(`apt install ffmpeg pandoc`, `brew install ffmpeg pandoc`).

### Première KB

```bash
a2kb setup                        # assistant : stockage, LLM, langue, confidentialité, votre première KB (notes)
a2kb doctor                       # vérifie la config, l'accès S3, la connexion au LLM, les outils externes

a2kb ingest --file examples/first-note.md       # un petit exemple fourni avec le dépôt
a2kb compile                      # raw/ -> wiki/ (incrémental ; --force recompile tout)
a2kb query "What is the codename of the alpha project?"
```

Le [tutoriel de premier lancement](docs/first-run.md) reprend les mêmes étapes avec la sortie
attendue de chacune (le nom du fichier brut, la page wiki, la réponse sourcée), connecte
Obsidian et propose un tableau de résolution des problèmes courants. Pour ingérer vos propres
sources, utilisez `a2kb ingest --url <url>`, `--file <path>` ou `--folder <dir>`.

Si vous exécutez une commande avant qu'une KB n'existe, `a2kb` propose de lancer `a2kb setup`
pour vous (uniquement dans un terminal ; les scripts, cron et les agents reçoivent l'erreur
habituelle).

`a2kb doctor` signale l'absence de connexion `claude` ou `codex` comme un échec. C'est normal
tant que vous ne vous êtes pas connecté ; les clés d'API et Ollama ne nécessitent aucune
connexion. Tant que vous n'avez qu'une seule KB, l'option `-p` peut être omise ; avec plusieurs
KB, utilisez `a2kb -p notes ...`. `a2kb list` affiche vos KB, et `a2kb new myproject --here`
écrit un pointeur `.kb/config.yaml` afin que `a2kb` fonctionne dans un dossier de projet sans
`-p`. Le paquet s'appelle `agent2kb`, et la seule commande qu'il installe est `a2kb` (un
`alias kb=a2kb` peut fournir une forme plus courte).

## Pourquoi agent2kb

Les équipes d'agents accumulent des connaissances dans des historiques de chat, des notes
éparses et des onglets de navigateur. Tout placer dans une base vectorielle renvoie des
fragments plutôt qu'une compréhension ; un coffre partagé unique mélange les projets et
n'a aucun processus qui en maintienne la cohérence ; et aucune de ces approches ne donne aux
personnes qui dépendent de ces connaissances un moyen de les vérifier.

agent2kb sépare le travail en trois étapes. La **collecte** est volontairement simple : tout
processus peut exécuter `ingest` pour déposer une source dans `raw/`. La **compréhension** est
l'étape de compilation : un LLM transforme les sources en pages avec titres, résumés, tags et
liens, ne réécrit que ce qui a changé, et étiquette ce qu'il a lui-même dérivé afin que les
sources brutes priment en cas de désaccord. L'**utilisation** passe par deux interfaces sur les
mêmes fichiers : `a2kb query` et MCP pour les agents, et Obsidian pour les personnes. Tout est
stocké en Markdown dans votre propre bucket, et peut donc être lu, comparé, sauvegardé et
modifié à la main.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pillars-dark.svg">
  <img src="docs/assets/pillars-light.svg" width="100%"
    alt="Ce qui distingue agent2kb : 1. compile, ne se contente pas de récupérer ; 2. une KB
    isolée par projet ; 3. agents et personnes, un seul wiki ; 4. des connaissances qui se
    cumulent ; 5. votre bucket, sans enfermement propriétaire ; 6. sûr face aux contenus non
    fiables">
</picture>

### Comparaison

| | Base vectorielle / RAG | Coffre de notes partagé | Mémoire de session d'agent | **agent2kb** |
|---|:---:|:---:|:---:|:---:|
| Pages lisibles, pas des fragments | ❌ chunks | ✅ écrites à la main | ❌ transcriptions | ✅ **compilées par LLM** |
| Cohérence maintenue sans travail manuel | ❌ | ❌ | ❌ | ✅ **compilation incrémentale + lint** |
| Connaissances séparées et isolées par projet | ⚠️ par index | ❌ un seul coffre | ⚠️ par agent | ✅ **un préfixe et une clé par KB** |
| Agents et personnes utilisent la même source | ❌ agents seulement | ⚠️ personnes seulement | ❌ agents seulement | ✅ **MCP + éditeur Markdown** |
| Chaque réponse cite ses sources | ⚠️ | ❌ | ❌ | ✅ |
| Fichiers bruts dans un stockage qui vous appartient | ❌ | ✅ | ❌ | ✅ **n'importe quel bucket S3** |
| Peut fonctionner sans aucun tiers | ⚠️ | ✅ | ❌ | ✅ **Ollama + S3 local** |

Chaque fichier porte un frontmatter YAML (`title`, `source`, `date`, `tags`, `source_kind`).
`source_kind` enregistre le niveau de confiance : les sources brutes et les sujets organisés
manuellement sont primaires ; les pages de concepts et les réponses enregistrées sont dérivées.
Voir [docs/compounding-loop.md](docs/compounding-loop.md).

## Fonctionnalités

| | |
|---|---|
| **Ingestion** | URL, YouTube et autres vidéos, audio, PDF/DOCX/XLSX, images (OCR), Markdown, captures d'Obsidian Web Clipper ; détection des doublons, contrôle de sécurité avant récupération |
| **Compilation** | Compilation incrémentale de raw vers wiki avec `[[wikilinks]]`, pages de concepts et ébauches pour les liens fantômes ; un index qui s'ouvre sur un récapitulatif « Past 7 days », et un digest hebdomadaire facultatif ; sortie en anglais ou dans toute langue que vous indiquez |
| **Requêtes** | Recherche par mots-clés avec embeddings et reranking facultatifs, réponses sourcées et expansion des termes de recherche ; les réponses enregistrées dans `outputs/` bouclent la boucle de capitalisation |
| **Accès des agents** | Serveur MCP (`a2kb mcp`, stdio ou HTTP protégé par bearer), en lecture seule par défaut ; `ask` et `ingest_url` sont à activer explicitement |
| **Isolation** | Préfixes par KB imposés par le client ; identifiants par KB lorsque le fournisseur peut en restreindre la portée |
| **LLM** | Connexions aux CLI `claude` / `codex` (en sandbox, avec une CLI de repli), Gemini, Groq, OpenRouter, Ollama ; modèle distinct pour la compilation en masse et pour les tâches auxiliaires |
| **Exploitation** | `a2kb doctor`, `lint`, `backup` et `restore`, un résumé d'exécution pour chaque compilation, des wrappers cron dans `scripts/` |
| **Backend Docker** | Un seul `docker compose up` : SeaweedFS avec des clés par KB, cron, un serveur MCP par KB, HTTPS facultatif |

## Démarrage rapide (local)

[Essayer](#essayer) couvre le parcours complet, et le
[tutoriel de premier lancement](docs/first-run.md) montre le résultat attendu de chaque étape.
Remarques complémentaires :

- `a2kb setup --non-interactive` prend tout depuis les options et les variables `KB_*`, pour la
  CI et le provisionnement ([options](docs/configuration.md#non-interactive-setup)).
- Préréglages de stockage : AWS S3, Cloudflare R2, Backblaze B2, MinIO, SeaweedFS, Wasabi,
  autre ([fournisseurs de stockage](docs/storage-providers.md)).
- La compilation, le lint, la sauvegarde et les ébauches s'exécutent selon une planification :
  voir [Garder le wiki compilé](#garder-le-wiki-compilé).
- Toutes les commandes et options : [docs/commands.md](docs/commands.md).

## Garder le wiki compilé

`a2kb ingest` se contente de stocker les sources dans `raw/` ; les pages apparaissent lorsque
`a2kb compile` s'exécute. Vous pouvez le lancer à la main, mais la configuration habituelle
l'exécute **selon une planification**, de sorte que tout ce qu'un agent ou une personne ingère
devient une page sans que personne n'ait à attendre :

| Comment vous exécutez agent2kb | Qui planifie la compilation |
|---|---|
| Backend Docker | C'est fait pour vous : compilation toutes les 15 minutes, lint quotidien, sauvegarde deux fois par jour, ébauches quotidiennes (`KB_CRON_DIGEST=1` ajoute un digest hebdomadaire) |
| Installation native (Linux, macOS) | Vous ajoutez une entrée cron, à l'aide des wrappers de `scripts/` |
| Usage occasionnel | Exécutez vous-même `a2kb compile` après l'ingestion |

Une crontab minimale pour une installation native (`crontab -e` ; adaptez les chemins) :

```cron
# toutes les 15 minutes : compiler les sources nouvelles ou modifiées (aucun appel au LLM si rien n'a changé)
*/15 * * * * /path/to/agent2kb/scripts/kb-cron-compile.sh /path/to/project notes
# chaque jour : lint structurel, et pages provisoires pour les sujets manquants souvent liés
0 9 * * *    /path/to/agent2kb/scripts/kb-cron-lint.sh  /path/to/project notes
0 3 * * *    /path/to/agent2kb/scripts/kb-cron-stubs.sh /path/to/project notes
# deux fois par jour : archive de sauvegarde locale de la KB
30 2,14 * * * /path/to/agent2kb/scripts/kb-cron-backup.sh /path/to/project notes
```

Les wrappers journalisent dans `kb-*.log` dans le dossier du projet et appellent la compilation
avec `--strict`, de sorte qu'une compilation échouée se termine avec un code non nul pour la
supervision. Les exécutions qui se chevauchent sont sans danger : tant qu'une compilation d'une
KB détient son bail (lease) dans le stockage, une autre exécution de la même KB saute ce
créneau (code de sortie 75 avec `--strict`) et le créneau suivant reprend le travail. Le modèle
complet est `scripts/kb-crontab.example` ; les détails de planification se trouvent dans
[docs/project-workflow.md](docs/project-workflow.md#cron).

**Le LLM qui compile est un paramètre, pas une partie de la planification.** Cron se contente
d'exécuter `a2kb compile` ; le bloc `llm:` de la KB décide qui fait le travail :

| `llm.provider` | Ce qui compile | Coût et confidentialité |
|---|---|---|
| `cli` avec `command: claude` ou `codex` | La CLI Claude Code ou Codex à laquelle vous êtes connecté, exécutée sans outils dans une sandbox | Utilise votre abonnement ; le contenu est envoyé à ce fournisseur |
| `ollama` | Un modèle Ollama local (`model: llama3.3:70b`, ou tout modèle que vous avez téléchargé) | Entièrement local ; nécessite une machine capable d'exécuter le modèle |
| `gemini`, `groq`, `openrouter` | L'API de ce fournisseur avec votre clé | Usage d'API facturé (ou offre gratuite) ; le contenu est envoyé à ce fournisseur |

Avec `provider: cli`, `fallback_command` désigne une seconde CLI (par exemple `codex` derrière
`claude`) qui prend le relais pour le reste de l'exécution lorsque la première n'a plus de
crédits ou est déconnectée. `model` et `aux_model` choisissent les modèles pour la compilation
des pages et pour les tâches plus légères (pages de concepts, rapports de lint, digest) ; tous
deux prennent par défaut le choix recommandé par le fournisseur.
Détails : [docs/llm-providers.md](docs/llm-providers.md).

## Paramètres et personnalisation

Tout se règle dans un seul fichier au niveau utilisateur, `~/.kb/config.yaml` (mode 0600 ;
`a2kb setup` l'écrit, et `KB_CONFIG` peut pointer ailleurs). Les paramètres sous `defaults:`
s'appliquent à toutes les KB ; les mêmes clés sous une KB les remplacent pour cette KB
uniquement :

```yaml
version: 2
storage: { bucket: my-kb, region: auto, endpoint: "https://<account>.r2.cloudflarestorage.com" }
llm:
  provider: cli
  command: claude
  fallback_command: codex
defaults:
  output_language: en          # anglais par défaut ; tout nom de langue fonctionne
  privacy_mode: false          # true : aucun appel cloud au moment de l'ingestion
  allow_jina_reader: false     # true : les pages web difficiles à lire sont récupérées via jina.ai
  allow_cloud_vision: false    # true : les images sont envoyées à un modèle cloud pour l'OCR
kbs:
  notes: {}                    # utilise les valeurs par défaut
  research:
    output_language: Spanish   # cette KB rédige ses pages et ses réponses en espagnol
  client-work:
    llm: { provider: cli, command: codex }   # cette KB est compilée par la CLI Codex à la place
```

Le démarrage le plus rapide passe par la CLI `claude` ou `codex` à laquelle vous êtes déjà
connecté : aucune clé d'API ni service supplémentaire. Pour une KB dont le contenu ne doit
jamais quitter votre machine, définissez `llm: { provider: ollama }` et exécutez un modèle local
avec Ollama (il faut une machine capable d'exécuter le modèle ; voir
[fournisseurs de LLM](docs/llm-providers.md#ollama)).

| Pour modifier | Définissez | En savoir plus |
|---|---|---|
| La langue des pages et des réponses | `output_language` (par KB ou dans `defaults`) | [Langue de sortie](docs/configuration.md#output-language) |
| Le récapitulatif « Past N days » de l'index | `recent_days` (7 par défaut, `0` = désactivé) | [Récapitulatif des pages récentes](docs/configuration.md#recent-pages-recap) |
| Le LLM qui compile et répond | le bloc `llm:` (autorisé par KB) | [Fournisseurs de LLM](docs/llm-providers.md) |
| Ce qui peut quitter votre réseau | `privacy_mode`, `allow_jina_reader`, `allow_cloud_vision` | [Confidentialité](docs/privacy.md) |
| L'emplacement de stockage d'une KB | `storage:` (partagé) ou le `storage:` et le `prefix` propres à une KB | [Fournisseurs de stockage](docs/storage-providers.md) |
| La KB utilisée par un dossier | `.kb/config.yaml` avec `kb: <name>` dans ce dossier (`a2kb new <name> --here`) | [Configuration](docs/configuration.md#using-a-kb-from-a-project-folder) |
| Une surcharge ponctuelle | Variables d'environnement `KB_*`, par exemple `KB_OUTPUT_LANGUAGE=ja` | [Variables d'environnement](docs/configuration.md#environment-variables) |

`a2kb config show` affiche la valeur effective de chaque paramètre et le fichier ou la variable
dont elle provient ; `a2kb doctor` vérifie que le résultat fonctionne. Les secrets (clés de
stockage, clés d'API) ont leur place uniquement dans ce fichier au niveau utilisateur ou dans
des variables d'environnement, jamais dans un dossier de projet.

## Backend Docker en 10 minutes

```bash
cp .env.example .env                  # nommez vos KB (KB_NAMES=research,work), choisissez un LLM
docker compose up -d                  # SeaweedFS (S3) + tâches cron + serveurs MCP ; les clés sont générées
docker compose exec kb claude auth login                                # une fois, si KB_LLM=claude
docker compose exec kb a2kb connect obsidian research --show-secret   # paramètres Remotely Save
docker compose exec kb a2kb connect agent research --show-secret      # `claude mcp add ...` avec URL et jeton
```

Cela génère un stockage S3 avec une clé distincte et restreinte par KB, le bucket, la
configuration, une planification cron et un serveur MCP en lecture seule par KB. Définissez
`DOMAIN=kb.example.com` et utilisez `--profile https` pour obtenir le HTTPS automatique (pour
Obsidian sur un téléphone et pour les agents distants), ou configurez plutôt votre propre
stockage S3. Les services compose s'appellent `kb` et `kb-mcp` ; la commande à l'intérieur est
`a2kb`. La procédure pas à pas, la rotation des clés et le compte rendu de tests se trouvent
dans [docs/docker.md](docs/docker.md).

## Connecter Obsidian

```bash
a2kb connect obsidian notes                # paramètres Remotely Save pour la KB notes
a2kb connect obsidian notes --show-secret  # idem, avec la clé secrète
```

Cela fonctionne pour toutes les configurations : AWS S3, Cloudflare R2, les autres fournisseurs
et le backend Docker (dans ce cas : `docker compose exec kb a2kb connect obsidian <kb>`). La
commande affiche l'endpoint, la région, le bucket, le style d'URL et le préfixe distant de la KB
(par exemple `kbs/notes/`) pour le plugin communautaire **Remotely Save** (« S3 or
compatible »). Une fois le préfixe défini, la racine du coffre est la KB : `raw/`, `wiki/` et
`outputs/` apparaissent comme des dossiers. Pour une KB qui utilise la chaîne d'identifiants
AWS, la fiche affiche des espaces réservés pour la clé, car un appareil doit avoir sa propre
clé, jamais l'identifiant AWS de la machine.

Choisissez la clé pour le plugin selon ce que fait l'appareil :

- **Consultation seule :** une clé en lecture seule (list et read) et un sens de
  synchronisation en récupération uniquement (pull).
- **Édition et Web Clipper :** la synchronisation bidirectionnelle envoie et supprime des
  fichiers ; elle nécessite donc une clé capable de lister, lire, écrire et supprimer dans le
  préfixe de la KB : une clé distincte pour l'appareil, pas celle qu'utilise agent2kb, afin de
  pouvoir la révoquer indépendamment.

Laissez vide le mot de passe de chiffrement du plugin pour la KB (agent2kb lit du Markdown en
clair), et commencez avec un nouveau coffre vide. La compilation et la synchronisation suivent
des horloges distinctes : une page arrive sur l'appareil à la première synchronisation après la
compilation, et la synchronisation ne s'exécute que lorsqu'Obsidian est ouvert. Le guide
complet, avec les deux recettes, l'arborescence des dossiers et une vérification de la première
synchronisation, se trouve dans [docs/obsidian.md](docs/obsidian.md).

Les `[[wikilinks]]` non résolus sont des nœuds fantômes intentionnels qu'une compilation
ultérieure résout ; `assets/obsidian-snippets/dim-ghost-links.css` les estompe, et `a2kb stubs`
peut créer des pages provisoires pour ceux qui sont le plus souvent référencés.

## Connecter vos agents (MCP)

```bash
claude mcp add kb-notes -- a2kb -p notes mcp      # lecture seule : search, read_page, list_recent, kb_info
```

`--allow-llm` ajoute l'outil `ask` et `--allow-write` ajoute `ingest_url` ; le mode HTTP exige
un jeton bearer. Des extraits de configuration pour Codex et Claude Desktop se trouvent dans
[docs/mcp.md](docs/mcp.md). Pour indiquer à un agent quand consulter la KB,
`a2kb scaffold rules --target <file>` écrit une section « KB query discipline » dans son fichier
d'instructions (par exemple `AGENTS.md`).

## Comparaison avec d'autres approches

- **Synchronisation Obsidian simple.** Synchroniser un coffre fournit un stockage et un
  lecteur, mais rien n'écrit les pages, et tous les projets partagent un même dossier. agent2kb
  ajoute le compilateur, l'isolation par projet et le fonctionnement sans surveillance, tandis
  qu'Obsidian reste le lecteur. Pour un coffre local unique que vous souhaitez interroger depuis
  votre éditeur, un plugin qui s'exécute à l'intérieur du coffre est le choix le plus simple.
- **Services de RAG vectoriel.** Ils renvoient les fragments de texte les plus proches d'une
  question. agent2kb compile les sources en pages lisibles avec des liens et leur provenance, de
  sorte que le résultat peut être parcouru, corrigé et comparé, et la recherche s'effectue sur
  ces pages. Les embeddings sont facultatifs.
- **Outils de mémoire de session d'agent.** Ils enregistrent ce qu'un agent a fait et dit.
  agent2kb compile des sources externes, ainsi que les réponses que vous choisissez
  d'enregistrer, en un wiki de référence partagé. Les deux approches sont complémentaires : la
  mémoire de session répond à « qu'est-ce qui a été tenté », et agent2kb répond à « qu'est-ce
  qui est connu ».

## Sécurité et confidentialité

- La compilation et les requêtes envoient le texte des pages au LLM configuré, et une offre
  gratuite de Gemini peut utiliser les prompts pour l'entraînement. Les nouvelles KB gardent
  Jina Reader et l'OCR d'images dans le cloud **désactivés**, et chaque compilation et
  ingestion affiche une bannière indiquant le fournisseur effectif. Pour des contenus sensibles,
  utilisez `llm.provider: ollama`. Détails : [docs/privacy.md](docs/privacy.md).
- Les pages web et les téléchargements de fichiers passent par une protection contre les SSRF
  qui bloque les adresses privées, vérifie chaque redirection et épingle la connexion. **Les URL
  de vidéos ne sont pas couvertes :** `yt-dlp` résout les noms et suit lui-même les
  redirections ; n'ingérez donc pas d'URL de vidéos provenant d'hôtes non fiables sur une
  machine qui peut atteindre des services internes. Via MCP, les URL de vidéos ne sont acceptées
  que depuis YouTube ou les hôtes `--ingest-allow-domain`. Détails :
  [docs/privacy.md](docs/privacy.md#video-urls-yt-dlp) et
  [docs/mcp.md](docs/mcp.md#fetch-safety-ssrf).
- `claude` et `codex` s'exécutent sans outils et sans MCP.
- L'isolation des KB comporte deux couches : une vérification de préfixe dans agent2kb, et des
  identifiants par KB dont la portée est restreinte par le fournisseur de stockage. Seule la
  seconde couche protège contre une clé divulguée.
  **Les versions de SeaweedFS antérieures à 4.34 sont affectées par la CVE-2026-55874**, qui
  permettait à une clé propre à une KB de copier les objets d'une autre KB ; le backend Docker
  fixe la version 4.48. Voir
  [docs/storage-providers.md](docs/storage-providers.md#seaweedfs).
- Signalez les vulnérabilités en privé : [SECURITY.md](SECURITY.md).

## FAQ

**Mes données sont-elles utilisées pour l'entraînement ?** agent2kb n'a aucune télémétrie et
n'envoie de données qu'au stockage et au LLM que vous configurez. Le fait qu'un fournisseur de
LLM s'entraîne ou non sur les prompts dépend de la politique de ce fournisseur : une offre
gratuite de Gemini peut le faire, alors qu'un serveur Ollama local garde les données sur votre
machine. Il vous incombe de détenir les droits sur le contenu que vous ingérez.

**Quels LLM sont pris en charge ?** Une CLI `claude` ou `codex` connectée (aucune clé d'API
requise), Gemini, Groq, OpenRouter et Ollama. Voir [docs/llm-providers.md](docs/llm-providers.md).

**Peut-il fonctionner entièrement en local ?** Oui. Utilisez Ollama pour la compilation et les
embeddings, SeaweedFS ou MinIO pour le stockage, et activez `privacy_mode`. Votre contenu n'est
alors jamais envoyé à un tiers.

**Faut-il un GPU ?** Non. Seul un modèle Ollama local en tire profit, car la compilation via une
CLI ou une API s'exécute à distance. La transcription audio locale (l'extra `whisper`) est
facultative.

**Les personnes peuvent-elles modifier les pages ?** Oui, dans Obsidian ou n'importe quel
éditeur Markdown. Une page n'est régénérée que lorsque sa source brute est recompilée. Pour des
pages écrites à la main qui ne doivent jamais être régénérées, utilisez
`a2kb promote --as wiki-topic`.

**Quelles langues sont prises en charge ?** Les sources peuvent être dans n'importe quelle
langue. Les pages et les réponses sont rédigées en anglais, sauf si vous définissez
`output_language` sur une autre langue, pour une KB ou pour toutes.

**Comment voir ce qui est nouveau ?** Ouvrez `wiki/index.md` : il commence par un récapitulatif
automatique des 7 derniers jours (voir [l'exemple](#un-récapitulatif-des-nouveautés-rédigé-pour-vous)).
Définissez `recent_days` par KB pour changer la fenêtre, ou `0` pour le désactiver. Le digest
hebdomadaire facultatif (`scripts/kb-cron-digest.sh`, ou `KB_CRON_DIGEST=1` dans Docker) écrit
un récapitulatif plus long de la semaine dans `outputs/weekly/`.

## Feuille de route

agent2kb est une version antérieure à la 1.0. La CLI, les formats de configuration et
l'organisation S3 sont figés par des tests de contrat, mais il faut s'attendre à quelques
aspérités.

- Publier le paquet sur PyPI (d'ici là, installez depuis les sources).
- Vérifier d'autres fournisseurs de stockage : seul SeaweedFS a été testé sur un serveur réel,
  et R2, B2, MinIO, AWS et Wasabi suivent leur documentation.
- Rejouer le parcours complet `docker compose` et une synchronisation Obsidian sur plusieurs
  appareils pour cette version, y compris avec de vrais certificats HTTPS (les deux ont été
  utilisés avec des versions antérieures).
- Non prévu : une interface web (la lecture se fait via Obsidian, la CLI ou MCP) et une mémoire
  de session d'agent.

## Documentation

| | |
|---|---|
| [docs/first-run.md](docs/first-run.md) | Premier lancement avec un fichier d'exemple : ingestion, compilation, requête, Obsidian, dépannage |
| [docs/obsidian.md](docs/obsidian.md) | Obsidian et Remotely Save : clés, sens de synchronisation, chiffrement, délais |
| [docs/configuration.md](docs/configuration.md) | Fichier de configuration, KB, rôle admin, variables `KB_*`, langue de sortie |
| [docs/commands.md](docs/commands.md) | Toutes les commandes et options |
| [docs/llm-providers.md](docs/llm-providers.md) | Fournisseurs, sandbox des CLI, routage et repli |
| [docs/storage-providers.md](docs/storage-providers.md) | Recettes AWS S3 et R2, B2, MinIO, SeaweedFS, Wasabi, isolation, erreurs courantes |
| [docs/docker.md](docs/docker.md) | Le backend Docker : compose, Obsidian, agents, HTTPS, rotation des clés |
| [docs/mcp.md](docs/mcp.md) | Serveur MCP, outils, mode HTTP, modèle de sécurité |
| [docs/privacy.md](docs/privacy.md) | Ce que chaque étape envoie à quel tiers |
| [docs/project-workflow.md](docs/project-workflow.md) | Usage quotidien, `source_meta`, cron |
| [docs/compounding-loop.md](docs/compounding-loop.md) | Comment les réponses enregistrées alimentent la compilation suivante |
| [docs/operations.md](docs/operations.md) | Sauvegarde, manifeste, résumés d'exécution, hygiène du lint |
| [CHANGELOG.md](CHANGELOG.md) | Versions et notes de mise à niveau |

## Contribuer

Consultez [CONTRIBUTING.md](CONTRIBUTING.md) pour la mise en place, le contrat de
compatibilité et les règles de code ; les agents de développement doivent lire
[CLAUDE.md](CLAUDE.md). Merci de respecter le [Code de conduite](CODE_OF_CONDUCT.md).

## Marques déposées

agent2kb est un projet open source indépendant. Il n'est ni affilié à, ni approuvé par, ni
sponsorisé par Obsidian, Anthropic, OpenAI, Google, Amazon Web Services, Cloudflare ou toute
autre entreprise ou produit cité dans ce dépôt. Tous les noms de produits, logos et marques
appartiennent à leurs propriétaires respectifs et ne sont utilisés que pour décrire la
compatibilité.

## Licence

agent2kb est distribué sous la [GNU Affero General Public License v3.0](LICENSE)
(`AGPL-3.0-only`). Vous pouvez l'utiliser, le modifier et le redistribuer selon ces conditions ;
si vous le modifiez et permettez à d'autres de l'utiliser via un réseau (par exemple en tant que
service hébergé ou serveur MCP partagé), l'AGPL vous oblige à leur proposer le code source de
votre version modifiée.

**Licence commerciale.** Si les conditions de l'AGPL ne conviennent pas à votre usage, par
exemple pour intégrer agent2kb dans un produit ou un service propriétaire (closed source), une
licence commerciale distincte peut être proposée par les mainteneurs : voir
[agent2kb.com](https://agent2kb.com) ou écrire à [licensing@agent2kb.com](mailto:licensing@agent2kb.com).

Les contributions sont acceptées dans le cadre du
[Contributor Licence Agreement](CLA.md) (accord de licence de contributeur), qui permet au
projet de proposer les deux licences.
