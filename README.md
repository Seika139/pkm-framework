# PKM Framework

個人で管理するデジタルな知識情報を人間とAIが扱いやすい形式で保存・管理するためのフレームワークです。
このリポジトリ自体は知識の管理を行わず、知識管理のためのツールや仕組みを提供します。

## 機能

- Obsidian vault の `raw/clips/` と `wiki/` にある Markdown を SQLite FTS5 で検索します。
- `pkm index` は全件再構築、CLI / MCP の通常検索は追加・変更・削除を差分反映します。
- `pkm search` / `pkm read` の CLI と検索・読み取り・Wiki 書き込みに対応する stdio MCP server を提供します。
- CLI と MCP は共通の WikiWriter を使い、変更前に Vault 境界・期待 SHA-256・Wiki の出典とリンク・索引を検証します。
- 同梱 Skills を `pkm skills install --target <dir>` で配置します。
- Storage 用の mise task/script と共通 Vault 規約を Python distribution に同梱し、Storage の bootstrap から固定された Framework 版で配置・更新できます。

Markdown が正本で、SQLite は `.pkm/cache/index.sqlite` に置く再生成可能な索引です。FTS5 trigram は日本語を含む3文字以上の検索語を部分一致で検索します。3文字未満の語を含む場合はタイトル・URL・パス・本文の部分一致にフォールバックします。

## 開発環境

`mise.toml` で固定した uv `0.12.23` と mise が必要です。uv が Python 3.12 を管理し、mise task は Bash script を使うため、Windows では WSL 上で実行してください。

```bash
mise install
mise run grant-permissions
uv sync
uv run pkm --help
```

## CLI と MCP

`--storage` には Storage repo root または `.pkm-storage` を含む vault directory を指定します。省略時は `PKM_STORAGE_DIR`、または現在のディレクトリと親ディレクトリから `.pkm-storage` を探します。Storage repo root を指定した場合は、その直下にあるマーカーファイル付きの vault directory を使います。複数の候補がある場合は vault directory を直接指定してください。

```bash
uv run pkm index --storage /path/to/pkm-storage
uv run pkm search "仙台旅行" --storage /path/to/pkm-storage
uv run pkm read raw/clips/example.md --storage /path/to/pkm-storage --json
uv run pkm wiki context --storage /path/to/pkm-storage
uv run pkm storage verify --storage /path/to/pkm-storage
uv run pkm skills install --target /path/to/pkm-storage/.agents/skills
uv run pkm mcp --storage /path/to/pkm-storage
```

`pkm read --json` と MCP `read` は、返却内容の `truncated` と、ファイル全体のバイト列に対する `sha256`（`sha256_scope: full_file_bytes`）、`total_chars`、`next_offset` を返します。全文を読むには `offset=0` から始め、`next_offset` が `null` になるまで同じ `max_chars` で読み進めます。ページ間で SHA-256 と `total_chars` が一致し、連結した文字数が `total_chars` と一致することを確認してください。各レスポンスの本文は一部分なので、複数ページを連結して全文を得るまでは編集元にしません。

Wiki の適用は同じ changeset を CLI / MCP に渡します。既存ファイルは全文 read で得た SHA-256 を指定し、新規作成は `null` を指定します。要求に Storage root を含めません。選択 Vault は CLI 実行時または MCP server 起動時に固定されます。CLI では JSON 本文を shell 引数へ埋め込まず、UTF-8 JSON ファイルを `--request-file` で渡してください。

```bash
uv run pkm wiki apply --storage /path/to/pkm-storage --request-file /path/to/wiki-changeset.json
```

MCP server には `uv run --project <storage>/.pkm pkm mcp --storage <storage>` を stdio server として登録し、`UV_PROJECT_ENVIRONMENT=<storage>/.pkm/runtime` を設定してください。ツールは `search`、ページ単位の `read`、`wiki_context`、`wiki_apply` です。`wiki_context` は共通 `AGENTS.framework.md` と Storage 固有 `AGENTS.md` の本文・パス、変更契約を返します。共通規約が欠けていれば `wiki_apply` は書き込みを拒否します。`wiki_apply` は CLI と同じ WikiWriter service と changeset validator を呼びます。Topic frontmatter は任意で整数 `importance: 1`〜`5` を持てます。Topic を削除する changeset は `delete_topics` と同期後の `wiki/index.md` を含み、残る Wiki からのリンクを検証します。Wiki 内リンクには Vault 相対 Markdown ファイルまたはディレクトリ、`http` / `https` / `mailto` URL、アンカーを使えます。絶対パス、Vault 外へ出る相対パス、プロトコル相対 URL、その他の URL scheme は拒否されます。MCP / AI は commit・push せず、変更を working tree に残します。

Writer は SQLite の `BEGIN IMMEDIATE` を使い、同じ Storage の PKM プロセス間で書き込みを直列化します。ロック DB `.pkm/cache/wiki-writer.sqlite` は symlink の事前検査後に絶対パスで開き、dir_fd には固定しません。同じ Storage 配下を書き換えられる外部プロセスが `.pkm/cache` またはロック DB を競合変更できる場合、相互排他は保証されません。また、この競合で Storage 外に cache ディレクトリや空のロック DB が作成される可能性があります。changeset 全体を検証してからファイルごとに一時ファイル経由で置換し、期待 hash を適用直前にも確認します。POSIX では symlink を拒否したディレクトリ descriptor に相対操作を固定します。これは複数ファイルを一括 atomically に置換する機能ではなく、Obsidian など外部プロセスによる同時編集もロックしません。hash 再確認と個々の rename / unlink の間に外部プロセスがファイル内容を変更する競合、または親ディレクトリを Vault 外へ移動する競合は完全には防げません。dir_fd が使えない Windows 等の環境ではパス検査後に通常の path 操作を行うため、TOCTOU への保護は弱くなります。I/O 失敗では rollback を試み、結果に `partial` と rollback 後も変更が残る `applied_paths` を返します。`applied_paths` には残った Markdown、作成ディレクトリ、一時ファイルが含まれます。書込みが適用済みで一時ファイルの cleanup のみ失敗した場合は `ok: true` と `partial: true` で返します。その場合、変更セットを再適用せず、報告された一時ファイルを確認してください。事前検証で拒否した場合は書込みなしとして `partial: false` になります。Wiki の FTS 再構築は書き込み成功条件に含めず、既存の検索時差分更新に任せます。Writer は Markdown の形式・パス・hash・リンクを検証し、意味の正しさや出典が主張を支持するかは AI の判断に委ねます。

差分更新で走査・読み込みに失敗した場合は索引 DB の変更を rollback します。全件再構築では一時 DB を作成し、成功後にだけ既存索引と置き換えます。

## Skills

原本は `src/pkm_framework/skills/` にあり、Python distribution に同梱します。`pkm-ingest`、`pkm-query`、`pkm-lint` を Framework 管理対象として manifest に記録します。`mise run skills-install` はこれらを Storage の `.agents/skills/` に配置し、以前に Framework が管理していたが現在は廃止された Skill のみを取り除きます。他の Skill や追加ファイルは保持します。

Storage 用の `mise/tasks/`・`mise/scripts/` と `pkm-storage-vault/AGENTS.framework.md` は `src/pkm_framework/storage_resources/manifest.json` に列挙したファイルだけを管理します。`mise/tasks/setup.sh`、Storage 固有 task/script、個人用 `AGENTS.md` はこの manifest の対象外です。インストーラは既存ファイルを上書きする前に前回配布 hash を確認し、初回は既知の Framework 配布内容と一致した場合だけ所有権を取得します。編集済み・未所有の同名 path を検知した場合は停止し、同じ名前空間の個人ファイルを保持します。`.gitignore` の予約 marker block もこの manifest から生成し、block 外の個人設定は維持します。更新は共有 Storage write lock を使い、ファイルごとの atomic replace と失敗時 rollback を行います。割り込み後の復旧は各ファイルがインストール直後または既に復元済みの hash/mode と一致するときだけ行い、別の編集を検知した場合は journal を保持して自動復旧を止めます。

Storage の安定 bootstrap は追跡対象の `mise/tasks/setup.sh` と `.pkm/setup_bootstrap.py` です。bootstrap は実行中の setup を排他し、symlink/non-regular marker を拒否してから incomplete marker を作成します。uv lock に固定した Framework を `uv sync --locked` で同期し、共通リソースと Skills を配置します。通常の Framework task も `uv run --locked` を使い、task 実行時に lockfile を変更しません。setup が失敗すると `.pkm/setup-incomplete` が残り、Framework task と CLI / MCP の Wiki 書き込み readiness check は処理を止めます。`mise run setup` の再実行で復旧してください。Framework の更新は Storage の dependency pin と `uv.lock` の更新を通じて選択します。

## 構成

```text
pkm-framework/
└── src/pkm_framework/
    ├── cli.py
    ├── index.py
    ├── mcp_server.py
    ├── storage.py
    ├── managed_resources.py
    ├── storage_resources/
    └── skills/
```
