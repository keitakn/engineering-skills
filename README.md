# engineering-skills

私（keitakn）がエンジニアリングを行う際に利用しているスキル一覧です。

AIコーディングエージェント（Claude Code / Codex CLI）向けのエージェントスキルを公開しています。特定のプロジェクト・組織の知識には依存しません。

## スキル一覧

| スキル | 利用するツール | 説明 |
|---|---|---|
| [code-comments](.claude/skills/code-comments) | Claude Code | コード / テストコード / コミットログ / コードコメントのどこに何を書くかを決める。コードには How、テストコードには What、コミットログには Why、コードコメントには Why not を書き分ける |
| [code-naming](.claude/skills/code-naming) | Claude Code | 識別子の命名。`get` の濫用をやめ、何をして値を得るのかが名前から読み取れる状態にする |
| [codex-plan-review-loop](.claude/skills/codex-plan-review-loop) | Claude Code | Codex CLI をレビュアーとして、実装計画の「指摘 → 修正」の改善ループを回す |
| [codex-pr-review-loop](.claude/skills/codex-pr-review-loop) | Claude Code | Codex CLI をレビュアーとして、自作 Pull Request の「指摘 → 修正 → 再レビュー」のループを回す |
| [explain-visually](.claude/skills/explain-visually) | Claude Code | 長大な設計文書・PR・Issue を読み解き、図と短い文を組み合わせた解説HTMLを生成してブラウザで開く |
| [github-pr-review-draft](.claude/skills/github-pr-review-draft) | Claude Code | 他の開発者の PR を下読みしてレビューコメント案を作り、人間が承認した内容だけを保留（PENDING）レビュー経由で投稿する |
| [claude-plan-review-loop](.codex/skills/claude-plan-review-loop) | Codex CLI | Claude Code をレビュアーとして、実装計画の「指摘 → 修正」の改善ループを回す |
| [claude-pr-review-loop](.codex/skills/claude-pr-review-loop) | Codex CLI | Claude Code をレビュアーとして、自作 Pull Request の「指摘 → 修正 → 再レビュー」のループを回す |

## 構成

- `.claude/skills/` 配下が Claude Code 用、`.codex/skills/` 配下が Codex CLI 用です
- 各スキルディレクトリは自己完結しており、必要なものだけを選んで使えます
- 前提条件（必要な CLI・認証など）は各スキルの SKILL.md に記載しています

### 対のスキルについて

`codex-plan-review-loop`（Claude Code から Codex CLI を呼ぶ）と `claude-plan-review-loop`（Codex CLI から Claude Code を呼ぶ）、および `codex-pr-review-loop` と `claude-pr-review-loop` は、レビュアーと実行側を入れ替えた対のスキルです。同じ駆動スクリプト（`plan_review.py` / `pr_review.py`）を使う設計のため、両方のディレクトリに同じ内容のスクリプトが含まれています。どちらか一方だけでも利用できます。

## 導入方法

このリポジトリを clone し、使いたいスキルのディレクトリごとに、各ツールのスキルディレクトリへシンボリックリンクを貼ります（コピーでも動作します。シンボリックリンクにすると `git pull` だけで更新が反映されます）。

```bash
cd /path/to/engineering-skills

mkdir -p ~/.claude/skills ~/.codex/skills

# 例: code-naming（Claude Code 用）
ln -sfn "$(git rev-parse --show-toplevel)/.claude/skills/code-naming" ~/.claude/skills/code-naming

# 例: claude-pr-review-loop（Codex CLI 用）
ln -sfn "$(git rev-parse --show-toplevel)/.codex/skills/claude-pr-review-loop" ~/.codex/skills/claude-pr-review-loop
```

## ライセンス

[MIT License](LICENSE)
