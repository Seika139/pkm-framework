---
name: pkm-lint
description: ユーザーが Wiki の監査、lint、リンクや出典の点検、importance の見直し、削除候補の確認を依頼したときに使う。
---

# Wiki を監査する

既定では読み取り専用で監査し、確認できるファイルと根拠を添えて報告します。`raw/clips/` は保存時点の原典として扱い、現在の Web 情報と混同しません。

## 接続と規約

接続済み PKM MCP がある場合は `wiki_context` を呼び、共通 Framework 規約（`AGENTS.framework.md`）と Storage 固有規約（`AGENTS.md`）の本文・パス、変更契約を使います。ローカル CLI では `pkm wiki context --storage "<Storage root または Vault>"` を実行します。共通規約が欠けている場合は `mise run setup` による修復を案内し、修復までは書き込みません。

## 監査

1. ユーザー指定の範囲を優先します。範囲指定がなければ `wiki/` の管理ページ、全 Topic、存在する分析ページを構造点検し、全ページの意味的な主張を監査したとは扱いません。
2. `wiki/index.md` と `wiki/topics/` を照合し、各 Topic が索引に一度だけ載ること、リンクが解決すること、説明が要旨に合うことを確認します。暫定分析は確定知識と分けます。
3. Topic の `title`、`created`、`updated`、`source_refs`、任意の `importance`、日付形式を確認します。importance は Vault 規約に従う整数 1〜5、未設定は未評価です。各 `source_refs.path` が Vault 相対の `raw/clips/` Markdown を指すこと、任意の URL が原典と合うこと、重要な主張の近くに出典への相対リンクがあることを確認します。
4. Wiki の Markdown リンク、孤立ページ、重複候補を確認し、根拠を添えて報告します。単独 Topic やリンク数だけで誤りと断定しません。
5. `wiki/log.md` は日付、操作種別、変更ファイルへのリンク、短い要約を含む追記形式かを確認します。ログ中の過去リンクは現状で未解決でも歴史的記録として扱います。`wiki/questions.md` は未解決で後から調べる価値のある問いか確認します。
6. `wiki/analyses/` は暫定状態、問い、Vault 相対の出典、不確実性、状態を個別に確認します。通常 Topic と同じ frontmatter や索引掲載を要求しません。
7. 内容監査が対象なら関連 raw を読み、主張の根拠、矛盾、保存時点以降に古くなった可能性、明らかな重複を確認します。Web を確認していない場合、現時点でも有効とは断定しません。

## Importance と忘却

- ingest や query ごとに importance を採点・変更しません。見直しを依頼された場合に限り、Vault 規約の尺度で候補と根拠を報告します。ページ長、出典数、単なる古さ、未計測の検索頻度から推定しません。
- 既定の lint と定期レビューは report-only です。定期レビューも候補を報告・提案するだけで、自動書き込み、削除、commit をしません。importance を書き込むのは、ユーザーが「見直して反映」など明示的に依頼した場合だけです。
- importance だけを変更するときは Topic の `updated` を維持し、`wiki/log.md` に記録しません。
- Topic の忘却と、分析の忘却目的の削除はユーザーの明示依頼がある場合だけです。importance が低いことや古いことだけでは削除せず、重複、陳腐化、今後の用途の欠如を根拠に候補を示します。
- 既存分析を Topic へ昇格する削除は、ユーザーが知識の統合を依頼した ingest/query で分析の要点を Topic に実際に取り込み、同じ changeset に削除を含める場合のライフサイクル処理です。これは Topic の忘却と区別し、未 commit の内容が Git 履歴から戻せるとは説明しません。
- 忘却目的の削除前に対象ファイルの現行内容が commit 済みか、別途保全されていることを確認します。どちらも確認できない、または Git 状態を確認できない場合は削除前にユーザーへ確認し、回復を保証できないことを伝えます。

## 修正

- 監査・報告だけの依頼ではファイルを書き換えません。
- 明示的に修正を頼まれた場合も、依頼範囲の確実な構造修正だけを行います。出典間の矛盾や知識上の判断は独断で解決しません。
- 書き込み前に既存ファイルを全ページ読み、全ページの `sha256` と `total_chars` が一致することを確認します。読み取りは MCP `read(path, max_chars, offset)`、または `pkm read <Vault-relative path> --json --max-chars 200000 --offset <n>` で `next_offset` がなくなるまで行います。修正内容と必要な索引を1つの UTF-8 JSON changeset file にし、`wiki_apply` または `pkm wiki apply --request-file "<request JSON file>" --storage "<Storage root または Vault>"` で適用します。JSON 本文や Markdown を shell command argument に埋め込みません。操作は `lint`、更新の `expected_sha256` は読取時の値、作成時は `null` を使います。暫定分析の削除は `delete_analyses`、Topic の削除は `delete_topics` に、読取時 hash とともに指定します。
- Topic の新規作成・削除、または索引説明の修正時は `wiki/index.md` も同じ changeset で同期します。Topic 削除後に残る Topic・分析・索引・質問のリンクは Writer が検証します。削除した Topic への過去の `wiki/log.md` リンクは許容され、lint による Topic 削除は Writer がログへ追記します。importance だけの変更やその他の lint 変更はログに記録しません。`raw/clips/` は変更しません。
- 適用結果の `ok` と `partial` を確認します。`ok` が true で `partial` も true の場合、Wiki 修正は適用済みなので再送せず、`applied_paths` にある一時ファイルの残存を報告します。`ok` が false の場合は残存パスを確認し、最新内容を読んでから変更を再計画します。
- AI と MCP 自身は commit / push せず、変更を working tree に残します。定時同期が有効なら許可された Wiki の変更も次回同期時に自動 commit・同期されます。定時同期を使わない場合は、ユーザーが diff を確認して Wiki と索引・ログを commit します。Raw capture の手動 commit もユーザーが判断します。

報告にはファイル、該当内容、確認できた根拠、提案する対応を分けて記載します。
