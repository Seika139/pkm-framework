# Storage Wiki 規約

この文書は、この Vault の Wiki を作成・検索・更新する AI と人間向けの規約です。`raw/clips/` の Markdown は原典として扱い、取り込み時も変更しません。Web Clipper による保存だけでは Wiki を更新せず、明示的な ingest の依頼、または検索時に原典を確認した再利用価値のある統合知識が生じた場合に Wiki を更新します。

## Wiki の構造

`wiki/topics/` に、原典をまたいで再利用できる恒久的な統合知識を置きます。人物、概念、比較、情報源などの種別ごとのフォルダは作らず、必要な違いはページの題名と本文で表します。1つのクリップにつき1ページを作るのではなく、既存ページを先に探し、関連する知識があれば統合します。

`wiki/index.md` は Topic ページへのリンクと一文の説明を載せる目録です。本文や出典一覧を複製しません。追跡対象の暫定分析がある場合は、恒久的な Topic と明確に分けた「暫定分析」欄にリンクします。

`wiki/log.md` は Wiki の実質的な更新履歴、`wiki/questions.md` は後で調べる価値のある未解決事項を記録します。単純な検索や lint の実行はログに記録しません。

## Topic ページ

Topic は `wiki/topics/<内容が分かるファイル名>.md` に置き、既存ページを優先して更新します。ファイル名は内容を識別できる簡潔なものにし、同じ主題のページを重複作成しません。

各 Topic は次の YAML frontmatter を持ちます。`tags` は任意で、使用する場合に固定語彙は設けません。

```yaml
---
title: "ページの題名"
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_refs:
  - path: raw/clips/<filename>.md
    url: "https://example.com/source"
tags: []
---
```

`created` は初回作成日、`updated` は知識内容を実質的に変更した日を `YYYY-MM-DD` 形式で記録します。整形、lint、importance だけの変更では `updated` を変更しません。`importance` は任意の整数 `1`〜`5` で、未設定は未評価を表します。`source_refs.path` は Vault ルートからの相対パスとし、`url` は原典のクリップに記録されている場合だけ含めます。`source_refs` はページ全体の出典一覧です。

Importance は後の見直し優先度の目安です。`5` は判断や反復利用を支える中核、`4` は長期的に再利用価値が高い知識、`3` は関心に関連し再利用が見込まれる知識、`2` は用途が限定的または低頻度の知識、`1` は一度きり、重複、陳腐化の候補を表します。ページの長さ、出典数、単なる古さから数値を推定せず、検索頻度を計測していないため反復利用も推測しません。

本文は主題に合わせて構成し、重要な事実や判断の根拠には、対応する原典へ直接たどれる Markdown リンクを近くに付けます。Topic ファイルから `raw/clips/` への相対リンクは通常 `../../raw/clips/<filename>.md` です。出典一覧だけで本文中の根拠を曖昧にしません。

知識を裏づける出典を追跡できない場合は、恒久的な知識として Wiki に保存しません。原典と異なる表現で統合するときは、出典が支える範囲を保ちます。

## 索引と更新ログ

`wiki/index.md` には各 Topic を `- [題名](topics/<filename>.md) — 内容を示す一文。` の形式で載せます。Topic を作成または削除したときは索引も更新します。分析を追跡する必要がある場合だけ、索引に暫定分析用の独立した欄を設けます。

Wiki に実質的な変更を加えた ingest または query は、`wiki/log.md` の末尾に1件1行の箇条書きで追記します。各行に日付、操作種別、変更ページへのリンク、短い変更概要を含めます。lint による Topic の実質削除も Writer が記録します。既存の記録は編集・並べ替えず、検索回答のみ、importance だけの変更、その他の lint、整形だけの変更は記録しません。過去ログのリンクは歴史的記録として扱い、削除後にリンク切れになっても構いません。

## 未解決事項

`wiki/questions.md` には、後で調べる価値がある未解決の問いだけを箇条書きで記録し、関連する Topic があればリンクします。解決した問いは一覧から削除し、結論を該当 Topic に反映して、その実質的な変更を `wiki/log.md` に記録します。

## 暫定分析

`wiki/analyses/` は必要な場合だけ作成します。分析は Git 管理対象とし、各ファイルで暫定であること、問い、出典パス、不確実性、状態を明示します。暫定分析を恒久的な Topic と混在させず、索引に載せる場合も独立した欄に置きます。

分析の Topic への昇格は、ユーザーから知識の統合を依頼された ingest または query の一部として行います。結論と追跡可能な出典を Topic に統合したうえで、対応する分析ファイルを同じ changeset で削除します。これは Topic の忘却とは異なる昇格処理です。commit 済みの状態は Git 履歴に残りますが、未 commit の変更は working tree にあるため回復を保証しません。矛盾する原典がある場合は、出典ごとの相違を Topic に記録できるため、すべての矛盾を暫定分析にする必要はありません。

## 見直しと忘却

ingest と query では importance を毎回採点・変更しません。`pkm-lint` と将来の定期レビューは importance の見直しや候補を報告・提案する用途です。lint は既定で report-only とし、定期レビューも自動書き込み、削除、commit を行いません。importance を適用するのは、ユーザーが「見直して反映」など明示的に依頼した場合だけで、Writer には `operation: lint` を指定します。importance だけを変更するときは Topic の `updated` を保ち、`wiki/log.md` に記録しません。

Topic の忘却はユーザーの明示依頼がある場合だけ行います。importance が低いことや古いことだけでは削除しません。重複、陳腐化、今後の用途の欠如を根拠に削除候補を示します。`delete_topics` の前に対象の現行内容が Git commit 済み、または別途保全されていることを確認します。どちらも確認できない場合は削除前にユーザーへ確認し、回復を保証しません。削除 path は検索結果または Vault の実ファイル名と文字列を完全一致させ、大文字小文字や Unicode の表記を変更しません。Topic の削除では `wiki/index.md` を同じ変更に含め、Writer に残存 Topic・分析・索引・質問からのリンク検証とログ追記をさせます。`raw/clips/` は削除しません。分析ファイルの忘却も明示依頼に限ります。Topic への昇格に伴う分析削除は上の昇格手順に従います。

AI と MCP は Git commit や push を行わず、変更を working tree に残します。ユーザーは `git diff` で Wiki・索引・ログを確認してから手動で commit できます。

明示的にインストールした Storage の定時同期だけは、`pkm-storage-vault/raw/clips/`、`pkm-storage-vault/assets/`、`pkm-storage-vault/wiki/` の変更を自動で commit し、設定済み upstream と同期できます。`.obsidian/`、Storage repo root、`.pkm/`、`.agents/`、その他の変更は自動 commit の対象外です。allowlist 外の変更、既存の staged changes、進行中の Git 操作、設定されていない remote/upstream がある場合、同期は commit 前に停止します。定時同期でも Git hooks を実行し、hook の失敗時は停止します。

定時同期は branch 上の未 push commit をすべて upstream へ push します。remote の履歴と分岐しても競合がなければ merge commit を作れますが、競合解決、reset、autostash、force push は行わず、ignored file を上書きしない merge を使います。ignored file が merge を妨げる場合も同期は停止します。手動で解決し、Git working tree と index が確認できる状態になってから同期を再開してください。

Importance の見直しや忘却候補の定期レビューは引き続き report-only です。定時 Git 同期は allowlist 内の既存変更を配送する処理であり、Wiki の内容を生成・削除したり、importance を変更したりしません。
