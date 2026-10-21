<p align="center">
  <a href="https://agent2kb.com"><img src="docs/assets/hero.svg" width="100%"
    alt="agent2kb: エージェントチームのためのナレッジコンパイラ"></a>
</p>

<p align="center">
  <b>agent2kb は、エージェントが見つけたあらゆる情報を、出典付きで成長し続ける Markdown Wiki に変えます。</b><br>
  LLM がお客様自身のバケット内でコンパイルし、エージェントは MCP 経由で、
  人は Obsidian や任意の Markdown エディタで読みます。
</p>

<p align="center">
  <a href="https://agent2kb.com"><b>agent2kb.com</b></a> ·
  <a href="#試してみる">試してみる</a> ·
  <a href="docs/first-run.md">初回実行</a> ·
  <a href="#agent2kb-を選ぶ理由">agent2kb を選ぶ理由</a> ·
  <a href="#ドキュメント">ドキュメント</a>
</p>

<p align="center">
  <a href="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml"><img
    src="https://github.com/jcaol/agent2kb/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg"
    alt="ライセンス: AGPL-3.0"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg"
    alt="Python 3.10 以降"></a>
  <img src="https://img.shields.io/badge/MCP-server-6366f1.svg" alt="MCP サーバー">
  <img src="https://img.shields.io/badge/storage-any%20S3-0d9488.svg" alt="任意の S3 ストレージ">
  <img src="https://img.shields.io/badge/runs-fully%20local%20(Ollama)-c2410c.svg"
    alt="Ollama で完全ローカル実行可能">
</p>

<p align="center">
  <a href="README.md">English</a> · <b>日本語</b> · <a href="README.de.md">Deutsch</a> · <a href="README.es.md">Español</a> · <a href="README.pt-BR.md">Português (Brasil)</a> · <a href="README.fr.md">Français</a> · <a href="README.ko.md">한국어</a> · <a href="README.zh-TW.md">繁體中文</a>
</p>

<p align="center"><i>これは翻訳版です。基準となるのは<a href="README.md">英語版 README</a> であり、英語版のほうが新しい場合があります。</i></p>
<!-- Switch on once the package is on PyPI:
[![PyPI](https://img.shields.io/pypi/v/agent2kb.svg)](https://pypi.org/project/agent2kb/)
-->

**エージェントチームのためのヘッドレスなナレッジコンパイラです。** エージェントやボットが生のソース（Web
ページ、動画、PDF、メモ）を投入すると、LLM がそれらを相互リンクされた Markdown ページから成る自己保守型の
Wiki にコンパイルします。エージェントは CLI または MCP を通じて、人は Obsidian やその他の Markdown
エディタでそれを読みます。プロジェクトごとに、任意の S3 互換バケット内に独立した Wiki を持ち、
アプリケーションを起動したままにしておく必要はありません。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pipeline-dark.svg">
  <img src="docs/assets/pipeline-light.svg" width="100%"
    alt="agent2kb の仕組み: a2kb ingest がソースを raw/ に置き、a2kb compile が LLM でそれを
    wiki/ のページに変換し、エージェントは CLI と MCP で Wiki を読み、人は Obsidian で読み、
    outputs/ に保存された回答が次のコンパイルに取り込まれる">
</picture>

<!-- TODO(demo): record a short terminal GIF (ingest -> compile -> query) and save it as
     docs/assets/demo.gif, then uncomment:
![a2kb demo](docs/assets/demo.gif)
-->

- **取り込みは一度だけ、コンパイルは無人で。** cron がコンパイル、lint、バックアップ、ダイジェストを実行し、
  ある LLM が利用できないときはパイプラインが別の LLM にフォールバックします。エージェントに必要なのは `a2kb ingest` だけです。
- **プロジェクトごとに 1 つのナレッジベース（KB）。** 各 KB は専用のプレフィックスを持ち、ストレージ
  プロバイダが許す場合は専用の認証情報も持つため、ある KB で作業するエージェントは他の KB にアクセスできません。
- **エージェントと人のための 1 つの Wiki。** エージェントは MCP サーバーまたは CLI を使い、人は
  `[[wikilinks]]` を含むプレーンな Markdown を Obsidian や任意の Markdown エディタで読みます。保存された回答は
  次のコンパイルに取り込まれるため、Wiki はよく問い合わせされる分野ほど成長します。
- **信頼できないコンテンツとプライバシーを前提とした設計。** Web ページとファイルのダウンロードは
  SSRF ガードを通過します（`yt-dlp` が自ら取得する動画 URL は、ドキュメントに明記された例外です）。
  `claude` と `codex` はツールなしで実行され、新しい KB ではクラウドへのサイドチャネルが無効のままです。
  また、Ollama による完全ローカル構成もサポートしています。

Andrej Karpathy の
[LLM wiki パターン](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) に従い、
それをサーバーサイドのツールとして実現したものです。

### チームが Wiki エディタで目にするもの

<p align="center">
  <img src="docs/assets/vault.svg" width="100%"
    alt="イラスト: Obsidian で開いたコンパイル済みの agent2kb ページ。frontmatter のタグ、
    wikilinks を含む記事、関連ページのグラフビュー、バックリンクパネルが表示されている">
</p>

コンパイルされた各ページは、プロパティ（`source`、`date`、タグ、`source_kind`）、要約、構造化された
セクション、`[[wikilinks]]` を備えたプレーンな Markdown として届きます。そのため Obsidian のグラフビュー、
バックリンク、タグ検索がそのまま機能し、`index.md` の冒頭にはその週に KB が学んだ内容の「過去 7 日間」の
まとめが表示されます。未解決のリンクは、後のコンパイルまたは `a2kb stubs` で埋められるまでゴーストノードとして
残ります。*イラストは [examples/first-note.md](examples/first-note.md) のサンプルから作成したものです。
Vault の接続方法は [docs/obsidian.md](docs/obsidian.md) を参照してください。*

Obsidian は agent2kb がドキュメント化し推奨しているリーダーです。`[[wikilinks]]`、グラフ、バックリンクを
表示でき、Remotely Save プラグインで S3 から同期できるためです。ファイルは通常の Markdown なので、
それを読めるエディタであれば何でも使えます。ただし Obsidian 以外のツールでは S3 から同期する独自の手段が
必要であり、動作は検証されていません。

### 新着内容のまとめを自動で作成

コンパイルのたびに、`wiki/index.md` の冒頭が過去 7 日間のまとめで更新されます。新しいページに共通する
テーマを LLM がそれらのページから短く要約し、その後に各ページへのリンクが続きます。誰も書く必要がなく、
KB の更新に追従するため、休暇から戻ったチームメンバーや週に一度だけ確認する学習者も、KB が何を学んだかを
一目で把握できます。例（架空の内容）:

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

期間は設定で変更できます（`recent_days`: 月次のまとめなら `30`、無効にするなら `0`）。また、オプションの
週次ダイジェストを使うと、より長いまとめが `outputs/weekly/` に書き出されます。

### すべてのデバイスとすべてのエージェントで 1 つの KB

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/devices-dark.svg">
  <img src="docs/assets/devices-light.svg" width="100%"
    alt="S3 バケット内の 1 つの KB を、デスクトップ、iPhone、Android の人が Obsidian を通じて、
    エージェントが CLI と MCP を通じて共有し、その横でコンパイルがスケジュール実行される">
</picture>

KB は 1 台のマシンではなくお客様の S3 バケットに置かれるため、同じコンパイル済み Wiki があらゆるデバイスに
届きます。macOS、Windows、Linux、iPhone と iPad、Android（Remotely Save プラグイン経由）の Obsidian、
そして任意のノート PC、サーバー、CI ランナー上のエージェント（CLI または MCP 経由）です。コンパイルは
ストレージの近くでスケジュール実行されるため、すべてのデバイスの電源が切れていてもページは改善され続けます。
スマートフォンには HTTPS エンドポイント（AWS S3、Cloudflare R2、または HTTPS プロファイルを有効にした
Docker バックエンド）が必要です。[docs/obsidian.md](docs/obsidian.md) を参照してください。メンテナは以前の
バージョンで、セルフホストの SeaweedFS バックエンドに対し、複数のデスクトップおよびモバイルデバイス上の
Obsidian でこの構成を運用してきました。現行リリースの手順は、まだエンドツーエンドで再検証されていません。

## 試してみる

以下の前提条件が整っていれば、最初のページは数分で作成できます。クラウドアカウント、バケット、キーの作成には
もっと時間がかかり、その手順は [ストレージプロバイダ](docs/storage-providers.md) に記載されています。

前提条件: POSIX シェルを備えた Linux または macOS（パッケージが宣言しているプラットフォームです。Windows は
未検証です）、`venv` モジュール付きの Python 3.10 以降（Debian と Ubuntu では
`sudo apt install python3-venv`）、および `git`。さらに次のものが必要です。

- **ストレージ:** S3 互換バケットとそのキー（AWS S3 と Cloudflare R2 の手順:
  [ストレージプロバイダ](docs/storage-providers.md)）、またはストレージを含む
  [Docker バックエンド](#10-分で-docker-バックエンド)。
- **LLM:** インストール済みでログイン済みの `claude` または `codex` CLI
  （`npm i -g @anthropic-ai/claude-code` と `claude auth login`、または `npm i -g @openai/codex`
  と `codex login`）、あるいは API キー（Gemini、Groq、OpenRouter）、もしくはローカルの Ollama サーバー。

### インストール

agent2kb とその依存関係をシステムの Python から分離するため、仮想環境にインストールしてください（多くの
Linux ディストリビューションは、システム全体への `pip install` を `externally-managed-environment`
エラーで拒否します）。

```bash
git clone https://github.com/jcaol/agent2kb.git && cd agent2kb
python3 -m venv .venv                       # 初回のみ: .venv/ に独立した環境を作成
source .venv/bin/activate                   # 新しいターミナルごとに実行（zsh と bash）
python -m pip install --upgrade pip
python -m pip install ".[all]"              # 最初の PyPI リリースまではソースからインストール
a2kb --help                                 # コマンド一覧が表示されるはず
```

`python -m pip` は、`PATH` 上で最初に見つかる `pip` がどれであっても、有効な環境にインストールします。
後で別のターミナルを開いたときは、`a2kb` を使う前にチェックアウト先で再度 `source .venv/bin/activate` を
実行するか、`.venv/bin/a2kb` を直接呼び出してください（例: cron から）。`deactivate` で環境を抜けます。

`[all]` はすべてのプロバイダ SDK、ドキュメントと動画の取り込み機能、MCP サーバーをインストールします。
テキスト、Markdown、Web ページを `claude` または `codex` CLI のログインで扱うだけなら、より小さな
インストールで十分です。

```bash
python -m pip install .                     # コアのみ（確認済み: インストールでき、`a2kb --help` が動作）
python -m pip install ".[mcp]"              # コアとエージェント用 MCP サーバー
```

各機能がコアのインストールに加えて必要とするもの（不足しているものは `a2kb doctor` が一覧表示します）:

| 機能 | Python extra | 外部プログラム |
|---|---|---|
| Markdown、テキスト、Web ページ | なし | なし |
| LLM としての `claude` / `codex` CLI | なし | ログイン済みの CLI |
| Gemini、Groq、OpenRouter、Ollama | `gemini`、`groq`、`openai`、`ollama` | Ollama の場合は Ollama サーバー |
| 埋め込みとリランク（オプション） | `voyage`、`openai`、`gemini` または `ollama`。ベクトル検索の高速化には `fast` | なし |
| `.docx`、`.xlsx` ドキュメント | `media` | なし |
| PDF | `media` | なし |
| その他のドキュメント形式（例: `.epub`） | なし | `pandoc` |
| 動画 URL（YouTube など） | `media`（yt-dlp） | `ffmpeg` |
| ローカルでの音声・動画の文字起こし | `whisper`（重い。`all` には**含まれません**） | `ffmpeg` |
| MCP サーバー（`a2kb mcp`） | `mcp` | なし |

extra は組み合わせられます: `python -m pip install ".[mcp,media]"`。Python パッケージが `ffmpeg` や
`pandoc` をインストールすることはありません。システムのパッケージマネージャを使ってください
（`apt install ffmpeg pandoc`、`brew install ffmpeg pandoc`）。

### 最初の KB

```bash
a2kb setup                        # ウィザード: ストレージ、LLM、言語、プライバシー、最初の KB（notes）
a2kb doctor                       # 設定、S3 アクセス、LLM ログイン、外部ツールを確認

a2kb ingest --file examples/first-note.md       # リポジトリに同梱の小さなサンプル
a2kb compile                      # raw/ -> wiki/（差分コンパイル。--force ですべて再コンパイル）
a2kb query "What is the codename of the alpha project?"
```

[初回実行チュートリアル](docs/first-run.md) では、同じ手順を各ステップの期待される出力（raw ファイル名、
Wiki ページ、出典付きの回答）とともに説明し、Obsidian を接続し、よくある問題の復旧表も載せています。
ご自身のソースを取り込むには、`a2kb ingest --url <url>`、`--file <path>` または `--folder <dir>` を
使います。

KB がまだ存在しない状態でコマンドを実行すると、`a2kb` は `a2kb setup` の起動を提案します（ターミナルでの
み。スクリプト、cron、エージェントには通常のエラーが返ります）。

`a2kb doctor` は、`claude` または `codex` のログインがないことを失敗として報告します。ログインするまでは
これが想定どおりの動作です。API キーと Ollama ではログインは不要です。KB が 1 つだけのうちは `-p`
オプションを省略できます。複数の KB がある場合は `a2kb -p notes ...` を使ってください。`a2kb list` で KB の
一覧を表示でき、`a2kb new myproject --here` は `.kb/config.yaml` ポインタを書き出すので、そのプロジェクト
フォルダでは `-p` なしで `a2kb` が動作します。パッケージ名は `agent2kb` で、インストールされるコマンドは
`a2kb` だけです（`alias kb=a2kb` で短い形にすることもできます）。

## agent2kb を選ぶ理由

エージェントチームの知識は、チャットログ、走り書きのメモ、ブラウザのタブに蓄積されていきます。それをすべて
ベクトルストアに入れても、返ってくるのは理解ではなく断片です。1 つの共有 Vault ではプロジェクトが混在し、
一貫性を保つ仕組みもありません。そしてどちらの方法でも、その知識に頼る人がそれを監査する手段がありません。

agent2kb は作業を 3 つの段階に分けます。**収集**は意図的にシンプルです。どんなプロセスでも `ingest` を
実行すれば、ソースを `raw/` に置けます。**理解**はコンパイルの段階です。LLM がソースを、タイトル、要約、
タグ、リンクを備えたページに変換し、変更があった部分だけを書き直し、自ら導き出した内容にはラベルを付けて、
食い違いがある場合は生のソースが優先されるようにします。**利用**は同じファイルに対する 2 つのインターフェース
を通じて行われます。エージェント向けの `a2kb query` と MCP、そして人向けの Obsidian です。すべてはお客様
自身のバケットに Markdown として保存されるため、読む、比較する、バックアップする、手で編集することが
できます。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pillars-dark.svg">
  <img src="docs/assets/pillars-light.svg" width="100%"
    alt="agent2kb の違い: 1. 検索するだけでなくコンパイルする、2. プロジェクトごとに独立した KB、
    3. エージェントと人で 1 つの Wiki、4. 積み重なる知識、5. 自分のバケットでロックインなし、
    6. 信頼できないコンテンツにも安全">
</picture>

### 比較

| | ベクトルストア / RAG | 共有ノート Vault | エージェントのセッションメモリ | **agent2kb** |
|---|:---:|:---:|:---:|:---:|
| 断片ではなく読めるページ | ❌ チャンク | ✅ 手書き | ❌ 会話記録 | ✅ **LLM がコンパイル** |
| 手作業なしで一貫性を維持 | ❌ | ❌ | ❌ | ✅ **差分コンパイル + lint** |
| プロジェクトごとに分離・独立した知識 | ⚠️ インデックス単位 | ❌ 1 つの Vault | ⚠️ エージェント単位 | ✅ **KB ごとに 1 つのプレフィックスとキー** |
| エージェントと人が同じソースを使う | ❌ エージェントのみ | ⚠️ 人のみ | ❌ エージェントのみ | ✅ **MCP + Markdown エディタ** |
| すべての回答が出典を示す | ⚠️ | ❌ | ❌ | ✅ |
| 自分が所有するストレージ上のプレーンなファイル | ❌ | ✅ | ❌ | ✅ **任意の S3 バケット** |
| 第三者を一切介さずに実行可能 | ⚠️ | ✅ | ❌ | ✅ **Ollama + ローカル S3** |

すべてのファイルは YAML frontmatter（`title`、`source`、`date`、`tags`、`source_kind`）を持ちます。
`source_kind` は信頼度を記録します。生のソースと精選されたトピックは一次情報、コンセプトページと保存された
回答は派生情報です。[docs/compounding-loop.md](docs/compounding-loop.md) を参照してください。

## 機能

| | |
|---|---|
| **取り込み** | URL、YouTube などの動画、音声、PDF/DOCX/XLSX、画像（OCR）、Markdown、Obsidian Web Clipper のクリップ。重複検出、取得前の安全性チェック |
| **コンパイル** | `[[wikilinks]]`、コンセプトページ、ゴーストリンクのスタブを伴う raw から wiki への差分コンパイル。「過去 7 日間」のまとめで始まるインデックスと、オプションの週次ダイジェスト。英語または指定した任意の言語で出力 |
| **クエリ** | キーワード検索（オプションで埋め込みとリランク）、出典付きの回答、検索語の拡張。`outputs/` に保存された回答が知識の積み重ねのループを閉じる |
| **エージェントアクセス** | MCP サーバー（`a2kb mcp`、stdio またはベアラートークンで保護された HTTP）。デフォルトは読み取り専用で、`ask` と `ingest_url` はオプトイン |
| **分離** | クライアントが強制する KB ごとのプレフィックス。プロバイダがスコープを設定できる場合は KB ごとの認証情報 |
| **LLM** | `claude` / `codex` CLI のログイン（サンドボックス化、フォールバック CLI 付き）、Gemini、Groq、OpenRouter、Ollama。大量のコンパイルと補助ジョブで別々のモデルを指定可能 |
| **運用** | `a2kb doctor`、`lint`、`backup` と `restore`、コンパイルごとの実行サマリー、`scripts/` 内の cron ラッパー |
| **Docker バックエンド** | `docker compose up` 1 回で、KB ごとのキーを持つ SeaweedFS、cron、KB ごとの MCP サーバー、オプションの HTTPS |

## クイックスタート（ローカル）

[試してみる](#試してみる) で一連の手順をすべて説明しており、[初回実行チュートリアル](docs/first-run.md)
では各ステップの期待される結果を示しています。補足事項:

- `a2kb setup --non-interactive` はすべてをフラグと `KB_*` 変数から受け取ります。CI やプロビジョニング向けです
  （[フラグ](docs/configuration.md#non-interactive-setup)）。
- ストレージのプリセット: AWS S3、Cloudflare R2、Backblaze B2、MinIO、SeaweedFS、Wasabi、その他
  （[ストレージプロバイダ](docs/storage-providers.md)）。
- コンパイル、lint、バックアップ、スタブはスケジュール実行されます。
  [Wiki を常にコンパイル済みに保つ](#wiki-を常にコンパイル済みに保つ) を参照してください。
- すべてのコマンドとフラグ: [docs/commands.md](docs/commands.md)。

## Wiki を常にコンパイル済みに保つ

`a2kb ingest` はソースを `raw/` に保存するだけで、ページは `a2kb compile` の実行時に作られます。手動で
実行することもできますが、通常の構成では**スケジュール実行**するため、エージェントや人が取り込んだものは、
誰も待つことなくページになります。

| agent2kb の実行方法 | コンパイルをスケジュールするのは |
|---|---|
| Docker バックエンド | 自動で設定済み: 15 分ごとにコンパイル、毎日 lint、1 日 2 回バックアップ、毎日スタブ（`KB_CRON_DIGEST=1` で週次ダイジェストを追加） |
| ネイティブインストール（Linux、macOS） | `scripts/` 内のラッパーを使い、ご自身で cron エントリを追加 |
| たまに使う場合 | 取り込み後にご自身で `a2kb compile` を実行 |

ネイティブインストール向けの最小限の crontab（`crontab -e`。パスは適宜変更してください）:

```cron
# 15 分ごと: 新規または変更されたソースをコンパイル（変更がなければ LLM は使わない）
*/15 * * * * /path/to/agent2kb/scripts/kb-cron-compile.sh /path/to/project notes
# 毎日: 構造の lint と、よくリンクされるのに存在しないトピックのプレースホルダーページ
0 9 * * *    /path/to/agent2kb/scripts/kb-cron-lint.sh  /path/to/project notes
0 3 * * *    /path/to/agent2kb/scripts/kb-cron-stubs.sh /path/to/project notes
# 1 日 2 回: KB のローカルバックアップアーカイブ
30 2,14 * * * /path/to/agent2kb/scripts/kb-cron-backup.sh /path/to/project notes
```

ラッパーはプロジェクトフォルダ内の `kb-*.log` にログを出力し、`--strict` を付けてコンパイルを呼び出すため、
コンパイルが失敗すると監視用に 0 以外の終了コードを返します。実行が重なっても安全です。ある KB の
コンパイルがストレージ上のリースを保持している間、同じ KB の別の実行はその回をスキップし（`--strict` では
終了コード 75）、次の回が作業を引き継ぎます。完全なテンプレートは `scripts/kb-crontab.example` にあり、
スケジュールの詳細は [docs/project-workflow.md](docs/project-workflow.md#cron) にあります。

**どの LLM がコンパイルするかは設定で決まり、スケジュールの一部ではありません。** cron は `a2kb compile`
を実行するだけで、誰が作業するかは KB の `llm:` ブロックが決めます。

| `llm.provider` | コンパイルするもの | コストとプライバシー |
|---|---|---|
| `command: claude` または `codex` を指定した `cli` | ログイン済みの Claude Code または Codex CLI（サンドボックス内でツールなしで実行） | ご自身のサブスクリプションを使用。コンテンツはそのプロバイダに送信される |
| `ollama` | ローカルの Ollama モデル（`model: llama3.3:70b`、または pull 済みの任意のモデル） | 完全ローカル。モデルを実行できるマシンが必要 |
| `gemini`、`groq`、`openrouter` | ご自身のキーを使ったそのプロバイダの API | 課金（または無料枠）の API 利用。コンテンツはそのプロバイダに送信される |

`provider: cli` の場合、`fallback_command` で 2 つ目の CLI（例: `claude` の後ろに `codex`）を指定でき、
1 つ目のクレジットが尽きたりログアウトしたりすると、その実行の残りを引き継ぎます。`model` と `aux_model` は
ページのコンパイル用と軽いジョブ（コンセプトページ、lint レポート、ダイジェスト）用のモデルを選びます。
どちらもデフォルトはプロバイダの推奨モデルです。詳細: [docs/llm-providers.md](docs/llm-providers.md)。

## 設定とカスタマイズ

すべての設定はユーザーレベルの 1 つのファイル `~/.kb/config.yaml` で行います（モード 0600。
`a2kb setup` が書き出し、`KB_CONFIG` で別の場所を指すこともできます）。`defaults:` の下の設定はすべての KB
に適用され、ある KB の下に同じキーを書くと、その KB だけで上書きされます。

```yaml
version: 2
storage: { bucket: my-kb, region: auto, endpoint: "https://<account>.r2.cloudflarestorage.com" }
llm:
  provider: cli
  command: claude
  fallback_command: codex
defaults:
  output_language: en          # デフォルトは英語。任意の言語名を指定可能
  privacy_mode: false          # true: 取り込み時のクラウド呼び出しを一切行わない
  allow_jina_reader: false     # true: 読み取りにくい Web ページを jina.ai 経由で取得
  allow_cloud_vision: false    # true: OCR のため画像をクラウドモデルに送信
kbs:
  notes: {}                    # デフォルトを使用
  research:
    output_language: Spanish   # この KB はページと回答をスペイン語で書く
  client-work:
    llm: { provider: cli, command: codex }   # この KB は代わりに Codex CLI でコンパイル
```

最も手軽な始め方は、すでにログインしている `claude` または `codex` CLI を使うことです。API キーも追加の
サービスも不要です。コンテンツをマシンの外に決して出してはならない KB では、`llm: { provider: ollama }`
を設定し、Ollama でローカルモデルを実行してください（モデルを実行できるマシンが必要です。
[LLM プロバイダ](docs/llm-providers.md#ollama) を参照）。

| 変更したいもの | 設定 | 詳細 |
|---|---|---|
| ページと回答の言語 | `output_language`（KB ごと、または `defaults` 内） | [出力言語](docs/configuration.md#output-language) |
| インデックスの「過去 N 日間」のまとめ | `recent_days`（デフォルト 7、`0` = 無効） | [最近のページのまとめ](docs/configuration.md#recent-pages-recap) |
| コンパイルと回答に使う LLM | `llm:` ブロック（KB ごとに指定可） | [LLM プロバイダ](docs/llm-providers.md) |
| ネットワーク外に出てよいもの | `privacy_mode`、`allow_jina_reader`、`allow_cloud_vision` | [プライバシー](docs/privacy.md) |
| KB の保存先 | `storage:`（共有）または KB 独自の `storage:` と `prefix` | [ストレージプロバイダ](docs/storage-providers.md) |
| フォルダが使う KB | そのフォルダの `kb: <name>` を含む `.kb/config.yaml`（`a2kb new <name> --here`） | [設定](docs/configuration.md#using-a-kb-from-a-project-folder) |
| 一時的な上書き | `KB_*` 環境変数（例: `KB_OUTPUT_LANGUAGE=ja`） | [環境変数](docs/configuration.md#environment-variables) |

`a2kb config show` はすべての設定の有効な値と、それがどのファイルまたは変数から来たかを表示します。
`a2kb doctor` はその結果が機能するかを確認します。シークレット（ストレージキー、API キー）はこのユーザー
レベルのファイルまたは環境変数にのみ置き、プロジェクトフォルダには決して置かないでください。

## 10 分で Docker バックエンド

```bash
cp .env.example .env                  # KB に名前を付け（KB_NAMES=research,work）、LLM を選ぶ
docker compose up -d                  # SeaweedFS (S3) + cron ジョブ + MCP サーバー。キーは自動生成
docker compose exec kb claude auth login                                # 初回のみ、KB_LLM=claude の場合
docker compose exec kb a2kb connect obsidian research --show-secret   # Remotely Save の設定
docker compose exec kb a2kb connect agent research --show-secret      # URL とトークン付きの `claude mcp add ...`
```

これにより、KB ごとにスコープを限定した個別のキーを持つ S3 ストレージ、バケット、設定、cron スケジュール、
KB ごとに 1 つの読み取り専用 MCP サーバーが生成されます。`DOMAIN=kb.example.com` を設定して
`--profile https` を使うと HTTPS が自動で設定されます（スマートフォンの Obsidian やリモートのエージェント
向け）。代わりにご自身の S3 ストレージを設定することもできます。compose のサービス名は `kb` と `kb-mcp`
で、その中のコマンドは `a2kb` です。手順の詳細、キーのローテーション、テスト記録は
[docs/docker.md](docs/docker.md) にあります。

## Obsidian を接続する

```bash
a2kb connect obsidian notes                # KB notes 用の Remotely Save 設定
a2kb connect obsidian notes --show-secret  # 同上、シークレットキーを含む
```

これはすべての構成で機能します: AWS S3、Cloudflare R2、その他のプロバイダ、Docker バックエンド
（その場合: `docker compose exec kb a2kb connect obsidian <kb>`）。コミュニティプラグイン
**Remotely Save**（「S3 or compatible」）向けに、エンドポイント、リージョン、バケット、URL スタイル、KB の
リモートプレフィックス（例: `kbs/notes/`）を表示します。プレフィックスを設定すると Vault のルートが KB に
なり、`raw/`、`wiki/`、`outputs/` がフォルダとして表示されます。AWS の認証情報チェーンを使う KB では、
キーの欄にプレースホルダーが表示されます。デバイスには専用のキーを与えるべきであり、マシンの AWS ログインを
渡すべきではないためです。

プラグイン用のキーは、デバイスの用途に応じて選んでください。

- **閲覧のみ:** 読み取り専用キー（list と read）と、pull のみの同期方向。
- **編集と Web Clipper:** 双方向同期はファイルのアップロードと削除を行うため、KB のプレフィックス内で
  list、read、write、delete ができるキーが必要です。agent2kb が使うキーではなく、デバイス専用の別のキーにして、
  単独で失効させられるようにしてください。

KB ではプラグインの暗号化パスワードを空のままにし（agent2kb はプレーンな Markdown を読みます）、新しい空の
Vault から始めてください。コンパイルと同期は別々のタイミングで動きます。ページはコンパイル後の最初の同期で
デバイスに届き、同期は Obsidian が開いている間だけ実行されます。両方の手順、フォルダ構成、最初の同期の確認を
含む完全なガイドは [docs/obsidian.md](docs/obsidian.md) にあります。

未解決の `[[wikilinks]]` は意図的なゴーストノードで、後のコンパイルで解決されます。
`assets/obsidian-snippets/dim-ghost-links.css` でそれらを薄く表示でき、`a2kb stubs` で最も頻繁に参照される
ものについてプレースホルダーページを作成できます。

## エージェントを接続する（MCP）

```bash
claude mcp add kb-notes -- a2kb -p notes mcp      # 読み取り専用: search, read_page, list_recent, kb_info
```

`--allow-llm` は `ask` ツールを、`--allow-write` は `ingest_url` を追加します。HTTP モードではベアラー
トークンが必要です。Codex と Claude Desktop の設定例は [docs/mcp.md](docs/mcp.md) にあります。
エージェントにいつ KB を参照すべきかを指示するには、`a2kb scaffold rules --target <file>` を使うと、その
指示ファイル（例: `AGENTS.md`）に「KB query discipline」セクションが書き込まれます。

## 他のアプローチとの比較

- **Obsidian の同期だけを使う場合。** Vault を同期すればストレージとリーダーは手に入りますが、ページを書く
  ものはなく、すべてのプロジェクトが 1 つのフォルダを共有します。agent2kb はコンパイラ、プロジェクトごとの
  分離、無人運用を加え、Obsidian は引き続きリーダーとして使います。エディタから問い合わせたい単一の
  ローカル Vault であれば、Vault 内で動作するプラグインのほうがシンプルな選択です。
- **ベクトル RAG サービス。** これらは質問に最も近いテキストの断片を返します。agent2kb はソースをリンクと
  来歴を備えた読めるページにコンパイルするため、結果を閲覧、修正、比較でき、検索はそれらのページに対して
  行われます。埋め込みはオプションです。
- **エージェントのセッションメモリツール。** これらはエージェントが何をし、何を言ったかを記録します。
  agent2kb は外部のソースと、保存すると選んだ回答を、共有のリファレンス Wiki にコンパイルします。2 つの
  アプローチは補完し合います。セッションメモリは「何を試したか」に答え、agent2kb は「何がわかっているか」に
  答えます。

## セキュリティとプライバシー

- コンパイルとクエリは、設定された LLM にページのテキストを送信します。Gemini の無料枠はプロンプトを学習に
  使用する可能性があります。新しい KB では Jina Reader とクラウド画像 OCR は**無効**のままで、すべての
  コンパイルと取り込みで、実際に使われるプロバイダ名を示すバナーが表示されます。機密性の高い資料には
  `llm.provider: ollama` を使ってください。詳細: [docs/privacy.md](docs/privacy.md)。
- Web ページとファイルのダウンロードは SSRF ガードを通過します。このガードはプライベートアドレスをブロックし、
  すべてのリダイレクトを確認し、接続先を固定します。**動画 URL は対象外です:** `yt-dlp` は名前解決と
  リダイレクトの追跡を自ら行うため、内部サービスに到達できるマシンでは、信頼できないホストの動画 URL を
  取り込まないでください。MCP 経由では、動画 URL は YouTube または `--ingest-allow-domain` で指定した
  ホストからのみ受け付けます。詳細:
  [docs/privacy.md](docs/privacy.md#video-urls-yt-dlp) および
  [docs/mcp.md](docs/mcp.md#fetch-safety-ssrf)。
- `claude` と `codex` はツールなし、MCP なしで実行されます。
- KB の分離は 2 層です。agent2kb によるプレフィックスのチェックと、ストレージプロバイダがスコープを限定する
  KB ごとの認証情報です。漏洩したキーから守るのは 2 層目だけです。
  **4.34 より古い SeaweedFS は CVE-2026-55874 の影響を受けます。** これにより KB ごとのキーで別の KB の
  オブジェクトをコピーできてしまいました。Docker バックエンドはバージョン 4.48 に固定しています。
  [docs/storage-providers.md](docs/storage-providers.md#seaweedfs) を参照してください。
- 脆弱性は非公開で報告してください: [SECURITY.md](SECURITY.md)。

## よくある質問

**データは学習に使われますか？** agent2kb にはテレメトリがなく、データはお客様が設定したストレージと LLM に
のみ送信されます。LLM プロバイダがプロンプトを学習に使うかどうかは、そのプロバイダのポリシーによって決まり
ます。Gemini の無料枠では使われる可能性がありますが、ローカルの Ollama サーバーならデータはお客様のマシンに
とどまります。取り込むコンテンツの権利を保有している責任は、引き続きお客様にあります。

**どの LLM に対応していますか？** ログイン済みの `claude` または `codex` CLI（API キー不要）、Gemini、
Groq、OpenRouter、Ollama です。[docs/llm-providers.md](docs/llm-providers.md) を参照してください。

**完全にローカルで実行できますか？** はい。コンパイルと埋め込みに Ollama、ストレージに SeaweedFS または
MinIO を使い、`privacy_mode` を有効にしてください。そうすれば、コンテンツが第三者に送信されることは
ありません。

**GPU は必要ですか？** いいえ。GPU の恩恵を受けるのはローカルの Ollama モデルだけです。CLI や API による
コンパイルはリモートで実行されるためです。ローカルでの音声の文字起こし（`whisper` extra）はオプションです。

**人がページを編集できますか？** はい、Obsidian や任意の Markdown エディタで編集できます。ページが再生成
されるのは、その raw ソースが再コンパイルされたときだけです。決して再生成されてはならない手書きのページには
`a2kb promote --as wiki-topic` を使ってください。

**どの言語に対応していますか？** ソースはどの言語でもかまいません。ページと回答は、KB ごとまたはすべての KB
に対して `output_language` を別の言語に設定しない限り、英語で書かれます。

**新着内容はどうやって確認しますか？** `wiki/index.md` を開いてください。冒頭に過去 7 日間の自動まとめが
あります（[例](#新着内容のまとめを自動で作成) を参照）。期間を変更するには KB ごとに `recent_days` を
設定し、無効にするには `0` にします。オプションの週次ダイジェスト（`scripts/kb-cron-digest.sh`、または
Docker では `KB_CRON_DIGEST=1`）は、その週のより長いまとめを `outputs/weekly/` に書き出します。

## ロードマップ

agent2kb は 1.0 より前のリリースです。CLI、設定形式、S3 レイアウトはコントラクトテストで固定されていますが、
粗削りな部分があることを想定してください。

- パッケージを PyPI に公開する（それまではソースからインストール）。
- さらに多くのストレージプロバイダを検証する: 実際のサーバーに対してテストされているのは SeaweedFS だけで、
  R2、B2、MinIO、AWS、Wasabi はそれぞれのドキュメントに従っています。
- このリリースで、実際の HTTPS 証明書を含め、`docker compose` の一連の流れと複数デバイスでの Obsidian
  同期を再実行する（どちらも以前のバージョンでは使用されていました）。
- 予定していないもの: Web インターフェース（閲覧は Obsidian、CLI または MCP で行います）と、エージェントの
  セッションメモリ。

## ドキュメント

| | |
|---|---|
| [docs/first-run.md](docs/first-run.md) | サンプルファイルでの初回実行: 取り込み、コンパイル、クエリ、Obsidian、復旧 |
| [docs/obsidian.md](docs/obsidian.md) | Obsidian と Remotely Save: キー、同期方向、暗号化、タイミング |
| [docs/configuration.md](docs/configuration.md) | 設定ファイル、KB、admin ロール、`KB_*` 変数、出力言語 |
| [docs/commands.md](docs/commands.md) | すべてのコマンドとフラグ |
| [docs/llm-providers.md](docs/llm-providers.md) | プロバイダ、CLI のサンドボックス化、ルーティングとフォールバック |
| [docs/storage-providers.md](docs/storage-providers.md) | AWS S3 と R2 の手順、B2、MinIO、SeaweedFS、Wasabi、分離、よくあるエラー |
| [docs/docker.md](docs/docker.md) | Docker バックエンド: compose、Obsidian、エージェント、HTTPS、キーのローテーション |
| [docs/mcp.md](docs/mcp.md) | MCP サーバー、ツール、HTTP モード、セキュリティモデル |
| [docs/privacy.md](docs/privacy.md) | 各段階がどの第三者に何を送信するか |
| [docs/project-workflow.md](docs/project-workflow.md) | 日常的な使い方、`source_meta`、cron |
| [docs/compounding-loop.md](docs/compounding-loop.md) | 保存された回答が次のコンパイルにどう取り込まれるか |
| [docs/operations.md](docs/operations.md) | バックアップ、マニフェスト、実行サマリー、lint による整備 |
| [CHANGELOG.md](CHANGELOG.md) | リリースとアップグレードの注意事項 |

## コントリビュート

セットアップ、互換性コントラクト、コードのルールについては [CONTRIBUTING.md](CONTRIBUTING.md) を参照して
ください。コーディングエージェントは [CLAUDE.md](CLAUDE.md) を読んでください。
[行動規範](CODE_OF_CONDUCT.md) に従ってください。

## 商標

agent2kb は独立したオープンソースプロジェクトです。Obsidian、Anthropic、OpenAI、Google、Amazon Web
Services、Cloudflare、その他このリポジトリで言及されている企業や製品とは提携関係になく、それらによる承認や
後援も受けていません。すべての製品名、ロゴ、商標はそれぞれの所有者に帰属し、互換性を説明する目的でのみ
使用しています。

## ライセンス

agent2kb は [GNU Affero General Public License v3.0](LICENSE)（`AGPL-3.0-only`）の下でライセンスされて
います。その条件の下で使用、改変、再配布できます。改変したうえでネットワーク越しに他者に使用させる場合
（例: ホスティングサービスや共有 MCP サーバーとして）、AGPL により、改変版のソースをそれらの利用者に提供する
ことが求められます。

**商用ライセンス。** 例えば agent2kb をクローズドソースの製品やサービスに組み込むなど、AGPL の条件が
ご利用方法に合わない場合は、メンテナから別途商用ライセンスを取得できる可能性があります。
[agent2kb.com](https://agent2kb.com) を参照するか、[licensing@agent2kb.com](mailto:licensing@agent2kb.com) までご連絡
ください。

コントリビューションは [コントリビューターライセンス契約](CLA.md) の下で受け付けており、これによりプロジェクトは
両方のライセンスを提供できます。
