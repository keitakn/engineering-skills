# engineering-skills

私（keitakn）が普段の開発で使っているエージェントスキルを公開しています。SKILL.md 形式なので、Claude Code、Codex、Cursor のようにこの形式に対応したエージェントで使えます。特定のプロジェクトや組織の知識には依存しません。

## スキル一覧

| スキル | 主な実行環境 | こういうときに使う                                                                                                       |
|---|---|-----------------------------------------------------------------------------------------------------------------|
| [code-comments](.claude/skills/code-comments) | どのツールでも | AIに実装を任せると、コードをなぞるだけのコメントが増えていく。それを止めたいときに。コードには How、テストには What、コミットログには Why、コメントには Why not（あえてやらなかったこと）だけを書かせる |
| [code-naming](.claude/skills/code-naming) | どのツールでも | AIは `getUserData` のような微妙な名前の関数名を書いてくるので、名前だけで何をするのか分かりやすくする                                                     |
| [codex-plan-review-loop](.claude/skills/codex-plan-review-loop) | Claude Code | Claude Codeで作った実装計画をCodexでレビューする時に使う。実装前に指摘と修正のループを回してから着手する                                                    |
| [codex-pr-review-loop](.claude/skills/codex-pr-review-loop) | Claude Code | Claude Codeで作った Pull Request をCodexでレビューする、PRを出す前に使う。Codex が指摘し、取捨選択は人間が行い、修正と再レビューはループが回す                      |
| [explain-visually](.claude/skills/explain-visually) | どのツールでも（Claude Code 推奨） | 長文の実装計画や、他のメンバーが AI で作った PR などを読み解いて、理解を早めたいときに。図と短い文で1枚のHTMLに組み直し、ブラウザで開く                                      |
| [github-pr-review-draft](.claude/skills/github-pr-review-draft) | どのツールでも（Claude Code 推奨） | 他の開発者の PR レビューを AI に手伝わせたいが、勝手にコメントを投稿されては困るときに。AI がやるのは下読みとコメント案まで。GitHub に出るのは人間が承認した文面だけ                     |
| [claude-plan-review-loop](.codex/skills/claude-plan-review-loop) | Codex | codex-plan-review-loop の逆方向。Codex で開発していて、実装計画のレビューを Claude Code に任せたいときに                                       |
| [claude-pr-review-loop](.codex/skills/claude-pr-review-loop) | Codex | codex-pr-review-loop の逆方向。Codex で作った PR を Claude Code に検収させたいときに                                                |

前提条件（必要な CLI や認証）は各スキルの SKILL.md に書いてあります。

explain-visually と github-pr-review-draft は、シェルと `gh` や python3 を使えるエージェントならどれでも動きます。ただ、私が使っていて一番結果が良いのは Claude Code です。

### 対のスキルについて

codex-plan-review-loop と claude-plan-review-loop、codex-pr-review-loop と claude-pr-review-loop は、実行側とレビュアーを入れ替えた対です。同じ駆動スクリプト（`plan_review.py` / `pr_review.py`）を使う設計のため、両方のディレクトリに同じ内容のスクリプトが入っています。片方だけ使っても問題ありません。

## 導入方法

スキルはディレクトリ単位で自己完結しています。使いたいものだけ選んで、各ツールがスキルを探す場所に置いてください。

まずこのリポジトリを clone します。

```bash
git clone https://github.com/keitakn/engineering-skills.git
cd engineering-skills
```

ここから先のコマンドは、すべてこの clone した engineering-skills ディレクトリの中で実行してください。各コマンドはリポジトリの場所を `git rev-parse` で取得するので、`~/.claude/skills` などの置き先ディレクトリへ移動する必要はありません。

### Claude Code

個人用スキルは `~/.claude/skills/` から読み込まれます。シンボリックリンクも公式にサポートされているので、リンクで置いておくと `git pull` だけで更新が反映されます。

```bash
# clone した engineering-skills リポジトリの中で実行
mkdir -p ~/.claude/skills

REPO="$(git rev-parse --show-toplevel)"
ln -sfn "$REPO/.claude/skills/code-comments"          ~/.claude/skills/code-comments
ln -sfn "$REPO/.claude/skills/code-naming"            ~/.claude/skills/code-naming
ln -sfn "$REPO/.claude/skills/codex-plan-review-loop" ~/.claude/skills/codex-plan-review-loop
ln -sfn "$REPO/.claude/skills/codex-pr-review-loop"   ~/.claude/skills/codex-pr-review-loop
ln -sfn "$REPO/.claude/skills/explain-visually"       ~/.claude/skills/explain-visually
ln -sfn "$REPO/.claude/skills/github-pr-review-draft" ~/.claude/skills/github-pr-review-draft
```

レビューループ系と explain-visually と github-pr-review-draft は `disable-model-invocation: true` を付けているので、`/スキル名` で明示的に呼んだときだけ動きます。code-comments と code-naming は明示的に呼べるほか、関連する作業でエージェントが自動的にも参照します。

### Codex

ユーザーグローバルのスキルは `~/.agents/skills/` から読み込まれます。`~/.codex/skills/` も後方互換で読み込まれますが、現行の公式ドキュメントに書かれている場所は `~/.agents/skills/` です。シンボリックリンクのサポートは公式ドキュメントに明記されています。

```bash
# clone した engineering-skills リポジトリの中で実行
mkdir -p ~/.agents/skills

REPO="$(git rev-parse --show-toplevel)"
ln -sfn "$REPO/.codex/skills/claude-plan-review-loop" ~/.agents/skills/claude-plan-review-loop
ln -sfn "$REPO/.codex/skills/claude-pr-review-loop"   ~/.agents/skills/claude-pr-review-loop

# ツールを選ばないスキルも同じ要領で使えます
ln -sfn "$REPO/.claude/skills/code-comments"          ~/.agents/skills/code-comments
ln -sfn "$REPO/.claude/skills/code-naming"            ~/.agents/skills/code-naming
ln -sfn "$REPO/.claude/skills/explain-visually"       ~/.agents/skills/explain-visually
ln -sfn "$REPO/.claude/skills/github-pr-review-draft" ~/.agents/skills/github-pr-review-draft
```

呼び出しは `$スキル名` のメンションか `/skills` です。

### Cursor

Cursor 2.4（2026年1月）から Agent Skills に対応しています。読み込み場所はユーザーグローバルの `~/.cursor/skills/` のほか、互換のための `~/.claude/skills/` や `~/.agents/skills/` などです。

ただし、エディタ版の Cursor にはシンボリックリンクを辿らない[既知の問題](https://forum.cursor.com/t/cursor-doesnt-follow-symlinks-to-discover-skills/149693)があります（Cursor CLI は対応済み）。上のようなリンク配置だとエディタからスキルが見えないことがあるため、Cursor で使う分はコピーで置くのが確実です。

```bash
# clone した engineering-skills リポジトリの中で実行
mkdir -p ~/.cursor/skills

REPO="$(git rev-parse --show-toplevel)"
cp -R "$REPO/.claude/skills/code-comments"          ~/.cursor/skills/code-comments
cp -R "$REPO/.claude/skills/code-naming"            ~/.cursor/skills/code-naming
cp -R "$REPO/.claude/skills/codex-plan-review-loop" ~/.cursor/skills/codex-plan-review-loop
cp -R "$REPO/.claude/skills/codex-pr-review-loop"   ~/.cursor/skills/codex-pr-review-loop
cp -R "$REPO/.claude/skills/explain-visually"       ~/.cursor/skills/explain-visually
cp -R "$REPO/.claude/skills/github-pr-review-draft" ~/.cursor/skills/github-pr-review-draft
cp -R "$REPO/.codex/skills/claude-plan-review-loop" ~/.cursor/skills/claude-plan-review-loop
cp -R "$REPO/.codex/skills/claude-pr-review-loop"   ~/.cursor/skills/claude-pr-review-loop
```

コピーは `git pull` では更新されません。更新するときは、置いたスキルのディレクトリを `rm -rf ~/.cursor/skills/<スキル名>` で消してから、同じ cp コマンドで入れ直してください（ディレクトリが残ったまま cp すると、その中に入れ子でコピーされてしまうため）。呼び出しはチャットで `/スキル名` です。

## 参考リンク

各ツールのスキルの仕組みは公式ドキュメントを参照してください。

- Claude Code: https://code.claude.com/docs/en/skills
- Codex: https://developers.openai.com/codex/skills
- Cursor: https://cursor.com/docs/skills
- Agent Skills 標準: https://agentskills.io

## ライセンス

[MIT License](LICENSE)
