---
name: codex-pr-review-loop
description: Codex CLIでGitHub PRのコードレビューループを回す。PRのURLを指定し、Codexにレビューさせ、指摘をトリアージしてコードを修正し、再レビューする。PRにリンクされたIssue等は必読として機械的に検証する。
argument-hint: "<PRのURLまたは番号>"
disable-model-invocation: true
---

# PRコードレビューループ（レビュアー: Codex CLI）

Codex CLI（`codex exec`）をレビュアーとして、GitHub Pull Requestのコードに対する「指摘 → 修正 → 再レビュー」の改善ループを回します。AIが作成したコードを、人間の検収前に独立したレビュアーで検証するためのスキルです。

PRメタデータの取得・必読リンクの収集・プロンプト生成・Codex実行・結果解析・waiver適用・停滞検出・エビデンス検証は、すべて同梱スクリプトが行います。あなた（このスキルを実行するエージェント）の担当は次のとおりです。

- 手順に沿ってスクリプトを実行する。その前準備（チェックアウト・設定ファイル）と、実行中のエラー対処も自分で行う
- レビュー報告の提示と、ユーザーのフィードバックの受け渡し（手順4・5）
- blocking findings に基づくコードの修正（手順6）

指摘の生成はレビュアー側の役割なので、スクリプトを迂回した手動レビューには切り替えません（waiver・停滞検出・エビデンス検証が失われるため）。

スクリプト: `${CLAUDE_SKILL_DIR}/scripts/pr_review.py`（python3 標準ライブラリのみで動作。git / gh を外部コマンドとして使用。`${CLAUDE_SKILL_DIR}` が展開されない環境では、このSKILL.mdがあるディレクトリに読み替える）

## 前提条件

- `codex` コマンドがインストール済み・認証済み
- `gh` コマンドがインストール済み・認証済み（対象リポジトリへのアクセス権があること）
- レビュー対象のPRがGitHub上に存在する（open状態）
- レビュー対象リポジトリのローカルチェックアウトの中で実行する（セッション再開が同一ディレクトリに限定されるため、レビューループの途中で作業ディレクトリを変えない）

## 手順

### 1. 前準備（PRブランチのチェックアウト）

- ユーザーの指示からPRのURL（または番号）を特定する: $ARGUMENTS 。不足している場合はユーザーに確認する
- 対象リポジトリのローカルチェックアウトに移動し、PRのブランチをチェックアウトする:

```bash
gh pr checkout <PRのURL>
```

- すでにPRのブランチにいる場合はそのままでよい。作業ツリーに未コミットの変更が残っている場合は、ユーザーに扱い（コミット・退避・破棄）を確認する

### 2. 開始

レビューには推論の強さ `model_reasoning_effort="max"` を既定で使う（グローバル設定が `ultra` だと大きなプロンプトで無応答になるため。ユーザーが別の値を指定した場合はそちらを使う）。次の規則で設定ファイルを決めてから start を実行する。

- 対象リポジトリに `.agents/pr-review.json` が無い場合: 次の内容の一時ファイルを作り、`--config` で渡す

```bash
cat > "<一時ファイル（例: /tmp/pr-review-config.json）>" <<'EOF'
{"codexExtraArgs": ["-c", "model_reasoning_effort=\"max\""]}
EOF
```

- `.agents/pr-review.json` があり `codexExtraArgs` が未設定または空配列の場合: その JSON に上記の `codexExtraArgs` を追加した一時ファイルを作り、`--config` で渡す（`--config` を渡すと `.agents/pr-review.json` は読み込まれなくなるため、内容を引き継ぐ）
- `.agents/pr-review.json` に空でない `codexExtraArgs` が設定済みの場合: プロジェクトの設定を尊重し、`--config` を付けずに実行する（例外: その設定が原因で無応答になった場合はトラブルシューティングの対応を優先する）

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/pr_review.py" start --backend codex --pr "<PRのURL>" --config "<一時ファイル>"
```

- 出力されるJSONの `runDir` を控え、以後のコマンドで使う。`configSource` が意図した設定ファイルになっていることを確認する
- `requiredLinks` に、PR本文とGitHubの「Development」欄から収集した必読リンク（Issue等）が列挙される。ユーザーが指定した関連Issueがここに含まれていない場合は、レビュー前にユーザーに報告する
- `warnings` が空でない場合は内容をユーザーに伝える
- ブランチ不一致・作業ツリーの汚れ・リポジトリ不一致はここでエラーになる。エラーメッセージの指示に従って解消する

### 3. レビュー実行

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/pr_review.py" review --run "<runDir>"
```

2回目以降(コード修正後)は、修正サマリーのファイルを渡す。

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/pr_review.py" review --run "<runDir>" --changes-file "<runDir>/changes-round<N>.md"
```

review / feedback コマンドは1回に数分〜数十分かかることがある（スクリプト内部のタイムアウトは3600秒）。実行環境のコマンドタイムアウト上限がそれより短い場合は、バックグラウンド実行にして完了を待つ。途中でコマンドが打ち切られた場合は、同じコマンドをそのまま再実行してよい（状態はラウンド完了時にのみ保存されるため、途中終了で壊れない）。

### 4. 出力の読み方と分岐

出力末尾の `===PR_REVIEW_STATE===` の次の行にJSONがある。その `nextAction` で分岐する。

| nextAction | やること |
| --- | --- |
| `triage` | レビュー報告を省略せずそのままユーザーに提示し、フィードバックを待つ（手順5へ） |
| `fix_code` | コードを修正する（手順6へ） |
| `done` / `stagnated` / `max_rounds` | スクリプトが出力した完了サマリーをユーザーに提示して終了する（未pushコミットが残る場合の報告は手順6の5のとおり） |

`requiredEvidenceMet` が `false` の場合（`requiredLinksUnread` が1以上の場合を含む）は、レビュアーが必読リンクかFigma等の必須情報源を確認しないままレビューしたということなので、このラウンドの結果をそのまま採用しない。報告内の検証警告（何が未確認か）をユーザーに伝え、レビューをやり直すか、この結果のまま続行するかを確認する。やり直す場合、ランが継続中なら feedback コマンドで「未確認の必読リンク・情報源を確認したうえで再レビューしてください」と伝え、終了済み（`done` 等）なら start から新しいランを作る（waiver は新しいランに引き継がれない）。

### 5. トリアージ（フィードバックの反映）

feedback コマンドに渡すのは、指摘の取捨・調整に関するフィードバック（waive指定・修正方針・却下理由など）と、手順4で指示する再レビュー依頼だけである。それ以外（中止・質問など）を渡してもレビュアーは活用できず、1ラウンド分の時間を浪費する。

- 「全て修正してください」だけの場合は、feedbackコマンドを呼ばず手順6へ進む
- 中止の指示なら、レビュアーには送らず、その時点の指摘一覧を最終報告として終了する（未pushコミットの報告は手順6の5のとおり）
- レビュー報告への質問なら、報告とコードを自分で読んで答える
- 指摘の取捨・調整の指示は、ファイル（例: `<runDir>/feedback.txt`）に保存して次を実行する

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/pr_review.py" feedback --run "<runDir>" --file "<runDir>/feedback.txt"
```

- 「以後〜は除外」のような指示は、スクリプトが永続waiverとして自動保存する
- feedbackコマンドの出力も手順4と同じ形式なので、同じ分岐で処理する

### 6. コードの修正

1. 各 blocking finding の指摘内容に基づき、コードを直接修正する（PR説明文・リンクされたIssueの要件と矛盾しないこと。レビュアー側のCodexにはプロンプトでファイル編集を禁止しているが、念のため修正前に `git status` で想定外の変更がないか確認する）
2. 個別の指摘への対応が終わったら、PRの変更全体を見直して次の2点まで修正する（指摘箇所だけを直すと、他の箇所に残った同種の問題や、修正で生じた不整合が次のラウンドで新しい指摘になり、ループが収束しにくくなるため）:
   - 指摘の原因を特定し、同じ原因・同じパターンの問題が変更範囲の他の箇所（修正した箇所の呼び出し元・類似の実装を含む）にないか探して、あればまとめて直す
   - PRの差分全体を通読し、修正と整合しない箇所（命名の不一致・重複・使われなくなったコード・コメントやPR説明文とのずれ）が残っていないか確認して直す
3. プロジェクトのテスト・lint・型チェックを実行し、通ることを確認する
4. 修正内容の要約（指摘への対応に加えて、2で追加した修正も含む）を `<runDir>/changes-round<N>.md` に書き出す
5. 修正はローカルコミットにしてよいが、`git push` はユーザーの指示があるまで行わない。ループを終了するとき（完了・中止のいずれも）、未pushのコミットが残っていれば、その旨（コミット数と内容の要約）とpushするかの確認を最終報告に含める（黙って終えると、PRが未更新のまま完了したと誤解されるため）
6. 手順3に戻り、`--changes-file` 付きで review を実行する

## 中断からの再開

セッションを跨いで再開する場合は、`status --run "<runDir>"` で状態を確認し、上から順に最初に当てはまる分岐に従う。

1. `endReason` がある: 終了済みのラン。実績（`rounds`・`waivers`）をユーザーに報告する
2. `rounds` が空: 1回目のレビューが完了していない。手順3の review からやり直す
3. `rounds` の最終ラウンドに対応する `<runDir>/changes-round<N>.md` がある: 修正まで済んでいる。手順3の `--changes-file` 付き review から続ける
4. それ以外: 直近の指摘（`<runDir>/state.json` の `lastFindings`）をユーザーに再提示し、トリアージ（手順5）から続ける

## 必読リンクの扱い

- 必読: PR本文中のIssue/PR/Discussionへの参照（URL・`#123`・`owner/repo#123`）と、GitHubの「Development」欄にリンクされたIssue。未確認だった場合の扱いは手順4のとおり
- 参考（確認は任意）: PRコメント中のリンクと、GitHub以外のURL（Webドキュメント等）。ただし figma.com のURLは必須情報源として扱われる（未参照だと `requiredEvidenceMet` が `false` になる）
- 確認実績は、レビュアーのセッションが継続している間はラウンドをまたいで引き継がれる（前のラウンドで確認済みのリンクを2回目以降のレビューで再確認しなくても `requiredEvidenceMet` は `false` にならない）。セッションの再開に失敗して新規セッションになったラウンドでは、レビュアーに前回の記憶が無いため引き継がれず、改めて確認が必要になる
- 検証は「レビュアーが gh コマンド等でそのリンクに言及したか」の機械検出であり、内容を深く読んだことまでは保証しない
- レビュー報告に「ビルトインGitHub連携（codex_apps/github_*）の使用を検出」という警告が出た場合は、その内容をユーザーに伝え、`~/.codex/config.toml` でGitHubプラグインを無効化できているかの確認を案内する

## git worktree の利用（ユーザーの指示があった場合のみ）

ユーザーが worktree の利用を指示した場合は、現在のチェックアウトを汚さないよう、専用の worktree でループ全体を実行する。

```bash
git worktree add --detach <リポジトリ外のパス（例: ../<repo>-pr<N>）>
cd <worktreeのパス>
gh pr checkout <PRのURL>
```

- 以後の start / review / feedback をすべて worktree 内で実行する（途中でディレクトリを変えない）
- PRのブランチが元のチェックアウト側で使用中だと `gh pr checkout` が失敗する。その場合はユーザーに報告して指示を仰ぐ
- ループ完了後、不要になったら `git worktree remove <パス>` を案内する

## 設定の上書き（任意）

- 一時的な上書き: `start` の引数 `--max-rounds N` / `--stagnation-rounds N` / `--config <path>` / `--allow-dirty`
- プロジェクト単位の永続設定: リポジトリルートの `.agents/pr-review.json`（存在すれば自動で読み込む）
  - 使えるキーと既定値はスクリプト冒頭の `DEFAULT_CONFIG` を参照（maxRounds, blockingCategories, blockingSeverities, perspectives, additionalInstructions, codexExtraArgs など）
  - 例: `{"maxRounds": 4, "additionalInstructions": "認可まわりの変更を重点的に見る"}`

## トラブルシューティング

| 症状 | 対応 |
| --- | --- |
| `codex` が見つからない | Codex CLIの公式インストール手順の確認をユーザーに案内する |
| 認証エラー（401等） | `codex login status` / `gh auth status` で状態を確認し、必要なら再ログインを案内する |
| `Not inside a trusted directory` エラー | gitリポジトリ内（プロジェクトの作業ディレクトリ）で実行する |
| サンドボックス内で `Operation not permitted` や外部ネットワークへの接続失敗 | Bashのサンドボックスが原因。サンドボックス無しで再実行する |
| start が「ブランチが一致しません」で失敗 | `gh pr checkout <PRのURL>` を実行してから再度 start する |
| start が「未コミットの変更があります」で失敗 | ユーザーに扱いを確認する。意図的に残す場合のみ `--allow-dirty` を付ける |
| `turn.failed` で失敗 | スクリプトが該当イベントとログパスを表示する。内容をそのままユーザーに報告する |
| レビューが長時間無応答（`<runDir>/codex-round<N>.jsonl`、feedback 実行時は `codex-feedback-round<N>.jsonl` に item イベントが10分以上増えない） | `model_reasoning_effort` が高すぎると大きなプロンプトで発生することがある（`ultra` で発生を確認済み）。実行を止めてユーザーに報告し、手順2の設定ファイルで effort を1段下げて（例: `max` → `xhigh`）start からやり直す。プロジェクト設定の `codexExtraArgs` が原因の場合も、ユーザーに報告のうえ一時ファイルの `--config` で上書きしてよい（その設定のままでは解消できないため、この対応が手順2の「プロジェクトの設定を尊重」より優先する） |
| resume失敗 | スクリプトが新規セッションに自動フォールバックするため対応不要 |
| スクリプトがエラー終了 | まずエラーメッセージに書かれた解消手順に従い、自分の呼び出し方（引数・パス・実行ディレクトリ）に原因があれば直して再実行する。自力で解消できないエラーのみ、stderr・ログの内容をそのままユーザーに報告する |
