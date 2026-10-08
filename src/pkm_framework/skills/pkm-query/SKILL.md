---
name: pkm-query
description: 保存済みクリップや Wiki を使った質問、比較、説明、手順、調査に答え、原典に基づく再利用可能な統合を Wiki に反映するときに使う。
---

# PKM を検索して回答する

単純 lookup は回答だけにし、原典を確認した再利用可能な統合は既定で Wiki に反映します。

## 接続と規約

接続済み PKM MCP がある場合は最初に `wiki_context` を呼び、返された共通 Framework 規約（`AGENTS.framework.md`）と Storage 固有規約（`AGENTS.md`）、変更契約を使います。ローカル CLI では `pkm wiki context --storage "<Storage root または Vault>"` を実行します。両規約の本文とパスが返ることを確認し、共通規約が欠けている場合は `mise run setup` による修復を案内して Wiki を変更しません。複数 Vault がある場合は対象を明示します。

## 手順

1. ユーザーが「回答だけ」「Wiki に書かないで」と明示した場合は保存せず、回答以外の Wiki ファイルを変更しません。
2. `wiki/index.md`、関連 Topic、索引掲載の暫定分析、`raw/clips/` を検索します。初回検索が不十分なら用語を言い換えて再検索し、重要な Wiki と原典クリップを読みます。MCP の `read(path, max_chars, offset)` または `pkm read <Vault-relative path> --json --max-chars 200000 --offset <n>` を使います。`next_offset` がある間は続けて読み、全ページの SHA-256 と `total_chars` が一致し、連結した文字数が `total_chars` になることを確認します。SHA-256 はファイル全体のバイト列に対する値です。暫定分析は確定知識と区別し、結論に影響する主張は原典で確かめます。
3. 回答を組み立て、単一事実の確認、短い抜粋、ページ案内など統合を生まない lookup は保存しません。複数資料を結ぶ説明・比較・判断・手順など、後の質問にも使える非自明な統合は、ユーザーが拒否しておらず原典を追跡できる場合に保存します。
4. 保存する場合は既存 `wiki/topics/` を確認して重複を避けます。適切なページがなければ `wiki/topics/<内容が分かる名前>.md` を作り、Vault 規約の frontmatter、出典パス、記録 URL、重要な主張の近くの相対 Markdown 出典リンクを付けます。既存ページでは `created` を保持し、実質変更時だけ `updated` を当日にします。importance は通常の query では採点・変更せず、既存値を保持し、新規ページには原則設定しません。明示的な importance 見直しと反映は `pkm-lint` の手順で扱います。確かめられない外部知識だけの回答は保存しません。
5. 価値はあるが不確実性が高く恒久 Topic にできない統合だけを `wiki/analyses/` に保存します。問い、出典パス、不確実性、状態を記し、索引に掲載する場合は「暫定分析」欄に分けます。原典間の相違を Topic に記録できるなら分析を作る必要はありません。既存分析の Topic への昇格は、ユーザーから知識の統合を依頼された ingest/query の一部で内容を Topic に取り込む場合に行い、分析ファイル削除を同じ changeset に含めます。未 commit の変更は Git 履歴から回復できるとは限りません。
6. 原典を追跡でき、後で調べる価値がある未解決の問いは `wiki/questions.md` に記録します。Topic を作成・削除した場合、または索引説明が実質変更後の内容と合わない場合は `wiki/index.md` を同期し、全 Topic を一度ずつ掲載します。
7. 更新する既存ファイルは全ページを読み、全ページの SHA-256 と `total_chars` が一致することを確認します。完全な Markdown と必要な索引・質問ファイルを1つの UTF-8 JSON changeset file にし、接続済み MCP の `wiki_apply` に渡します。ローカルでは `pkm wiki apply --request-file "<request JSON file>" --storage "<Storage root または Vault>"` を実行します。JSON 本文や Markdown を shell command argument に埋め込みません。作成時の `expected_sha256` は `null`、更新時は直前に全文読取で得た SHA-256 を指定します。明示的に依頼された Topic 昇格では、分析削除に `delete_analyses` と読取時 hash を使い、変更後の索引も同じ changeset に含めます。changeset に Storage root を含めません。
8. 適用結果の `ok`、変更パス、`partial` を確認します。`ok` が true で `partial` も true の場合、Wiki 変更は適用済みなので再送せず、`applied_paths` にある一時ファイルの残存を報告します。`ok` が false または stale hash の場合は残存パスを確認してから最新ファイルを読み直し、現在の内容と hash に基づき再計画します。検索回答だけの場合は Wiki の作業履歴を追加しません。

## 回答

回答には根拠にした Vault 相対パスと原典クリップに記録された URL を示し、Wiki を更新した場合は更新パスと統合した内容を簡潔に伝えます。

## Git

AI と MCP 自身は commit / push せず、変更を working tree に残します。定時同期が有効なら許可された Wiki の変更も次回同期時に自動 commit・同期されます。定時同期を使わない場合は、ユーザーが diff を確認して Wiki と索引・ログを commit します。Raw capture の手動 commit もユーザーが判断します。
