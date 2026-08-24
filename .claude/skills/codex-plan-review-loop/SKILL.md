---
name: codex-plan-review-loop
description: Codex CLIで実装計画レビューの改善ループを回す。実装計画ファイルと要件ファイルを指定し、Codexにレビューさせ、指摘をトリアージして計画を修正し、再レビューする。
argument-hint: "<実装計画ファイル> <要件ファイル>"
disable-model-invocation: true
---

# 実装計画レビューループ（レビュアー: Codex CLI）

Codex CLI（`codex exec`）をレビュアーとして、実装計画の「指摘 → 修正」の改善ループを回します。AIが作成した実装計画を、実装に入る前に独立したレビュアーで検証するためのスキルです。

プロンプト生成・Codex実行・結果解析・waiver適用・停滞検出・エビデンス検証は、すべて同梱スクリプトが行います。あなた（このスキルを実行するエージェント）の担当は次のとおりです。

- 手順に沿ってスクリプトを実行する。実行中のエラー対処も自分で行う
- レビュー報告の提示と、ユーザーのフィードバックの受け渡し（手順4・5）
- blocking findings に基づく実装計画ファイルの修正（手順6）

指摘の生成はレビュアー側の役割なので、スクリプトを迂回した手動レビューには切り替えません（waiver・停滞検出・エビデンス検証が失われるため）。

スクリプト: `${CLAUDE_SKILL_DIR}/scripts/plan_review.py`（python3 標準ライブラリのみで動作。`${CLAUDE_SKILL_DIR}` が展開されない環境では、このSKILL.mdがあるディレクトリに読み替える）

## 前提条件

- `codex` コマンドがインストール済み・認証済み
- レビュー対象プロジェクトの作業ディレクトリ（gitリポジトリ内）で実行する（セッション再開が同一ディレクトリに限定されるため、レビューループの途中で作業ディレクトリを変えない）

## 手順

### 1. 対象ファイルの確認

- 第1引数 = planFile（レビュー対象の実装計画ファイル）、第2引数 = promptFile（実装計画の元となった要件ファイル）: $ARGUMENTS
- 不足している場合は、ユーザーに1つずつ確認する

### 2. 開始

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/plan_review.py" start --backend codex --plan "<planFile>" --req "<promptFile>"
```

出力されるJSONの `runDir` を控え、以後のコマンドで使う。

### 3. レビュー実行

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/plan_review.py" review --run "<runDir>"
```

2回目以降（計画修正後）は、修正サマリーのファイルを渡す。

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/plan_review.py" review --run "<runDir>" --changes-file "<runDir>/changes-round<N>.md"
```

review / feedback コマンドは1回に数分〜数十分かかることがある（スクリプト内部のタイムアウトは3600秒）。実行環境のコマンドタイムアウト上限がそれより短い場合は、バックグラウンド実行にして完了を待つ。途中でコマンドが打ち切られた場合は、同じコマンドをそのまま再実行してよい（状態はラウンド完了時にのみ保存されるため、途中終了で壊れない）。

### 4. 出力の読み方と分岐

出力末尾の `===PLAN_REVIEW_STATE===` の次の行にJSONがある。その `nextAction` で分岐する。

| nextAction | やること |
| --- | --- |
| `triage` | レビュー報告を省略せずそのままユーザーに提示し、フィードバックを待つ（手順5へ） |
| `fix_plan` | 計画を修正する（手順6へ） |
| `done` / `stagnated` / `max_rounds` | スクリプトが出力した完了サマリーをユーザーに提示して終了 |

`requiredEvidenceMet` が `false` の場合は、レビュアーが要件・計画から参照される情報源（GitHub上のIssue等・Figma）を確認しないままレビューしたということなので、このラウンドの結果をそのまま採用しない。報告内の検証警告（何が未確認か）をユーザーに伝え、レビューをやり直すか、この結果のまま続行するかを確認する。やり直す場合、ランが継続中なら feedback コマンドで「未確認の情報源を確認したうえで再レビューしてください」と伝え、終了済み（`done` 等）なら start から新しいランを作る（waiver は新しいランに引き継がれない）。なお、情報源の確認実績はレビュアーのセッションが継続している間はラウンドをまたいで引き継がれるため、2回目以降のレビューで再確認が無くてもそれだけで `false` にはならない。

### 5. トリアージ（フィードバックの反映）

feedback コマンドに渡すのは、指摘の取捨・調整に関するフィードバック（waive指定・修正方針・却下理由など）と、手順4で指示する再レビュー依頼だけである。それ以外（中止・質問など）を渡してもレビュアーは活用できず、1ラウンド分の時間を浪費する。

- 「全て修正してください」だけの場合は、feedbackコマンドを呼ばず手順6へ進む
- 中止の指示なら、レビュアーには送らず、その時点の指摘一覧を最終報告として終了する
- レビュー報告への質問なら、報告と計画・要件を自分で読んで答える
- 指摘の取捨・調整の指示は、ファイル（例: `<runDir>/feedback.txt`）に保存して次を実行する

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/plan_review.py" feedback --run "<runDir>" --file "<runDir>/feedback.txt"
```

- 「以後〜は除外」のような指示は、スクリプトが永続waiverとして自動保存する
- feedbackコマンドの出力も手順4と同じ形式なので、同じ分岐で処理する

### 6. 計画ファイルの修正

1. 各 blocking finding の指摘内容に基づき、planFile を直接修正する（promptFile の要件と矛盾しないこと。レビュアー側のCodexにはプロンプトでファイル編集を禁止しているが、念のため修正前に `git status` で想定外の変更がないか確認する）
2. 個別の指摘への対応が終わったら、planFile 全体を見直して次の2点まで修正する（指摘箇所だけを直すと、他の箇所に残った同種の問題や、修正で生じた不整合が次のラウンドで新しい指摘になり、ループが収束しにくくなるため）:
   - 指摘の原因を特定し、同じ原因・同じパターンの問題が他の箇所にないか探して、あればまとめて直す
   - 全体を通読し、修正後の内容と食い違う記述（用語・前提・手順・参照関係）が残っていないか確認して直す
3. 修正内容の要約（指摘への対応に加えて、2で追加した修正も含む）を `<runDir>/changes-round<N>.md` に書き出す
4. 手順3に戻り、`--changes-file` 付きで review を実行する

## 中断からの再開

セッションを跨いで再開する場合は、`status --run "<runDir>"` で状態を確認し、上から順に最初に当てはまる分岐に従う。

1. `endReason` がある: 終了済みのラン。実績（`rounds`・`waivers`）をユーザーに報告する
2. `rounds` が空: 1回目のレビューが完了していない。手順3の review からやり直す
3. `rounds` の最終ラウンドに対応する `<runDir>/changes-round<N>.md` がある: 修正まで済んでいる。手順3の `--changes-file` 付き review から続ける
4. それ以外: 直近の指摘（`<runDir>/state.json` の `lastFindings`）をユーザーに再提示し、トリアージ（手順5）から続ける

## 設定の上書き（任意）

- 一時的な上書き: `start` の引数 `--max-rounds N` / `--stagnation-rounds N` / `--config <path>`
- プロジェクト単位の永続設定: リポジトリルートの `.agents/plan-review.json`（存在すれば自動で読み込む）
  - 使えるキーと既定値はスクリプト冒頭の `DEFAULT_CONFIG` を参照（maxRounds, blockingCategories, blockingSeverities, perspectives, additionalInstructions, codexExtraArgs など）
  - 例: `{"maxRounds": 4, "additionalInstructions": "DB移行の後方互換性を重点的に見る"}`

## トラブルシューティング

| 症状 | 対応 |
| --- | --- |
| `codex` が見つからない | Codex CLIの公式インストール手順の確認をユーザーに案内する |
| 認証エラー（401等） | `codex login status` で状態を確認し、必要なら再ログインを案内する |
| `Not inside a trusted directory` エラー | gitリポジトリ内（プロジェクトの作業ディレクトリ）で実行する |
| サンドボックス内で `Operation not permitted` | Bashのサンドボックスが原因。サンドボックス無しで再実行する |
| resume失敗 | スクリプトが新規セッションに自動フォールバックするため対応不要 |
| スクリプトがエラー終了 | まずエラーメッセージに書かれた解消手順に従い、自分の呼び出し方（引数・パス・実行ディレクトリ）に原因があれば直して再実行する。自力で解消できないエラーのみ、stderr・ログの内容をそのままユーザーに報告する |
