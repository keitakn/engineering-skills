#!/usr/bin/env python3
"""GitHub PRコードレビューループの駆動スクリプト。

Codex CLI または Claude Code CLI をレビュアーとして呼び出し、
PRメタデータ取得・必読リンク収集・プロンプト生成・実行・結果解析・
waiver適用・停滞検出・エビデンス検証（必読リンクを実際に読んだか）を行う。
呼び出し側エージェントの仕事は、このスクリプトの実行、レビュー報告の提示、
blocking指摘に基づくコード修正だけにする。

Python 3.9+ / 標準ライブラリのみで動作する（git / gh は外部コマンドとして使用）。

サブコマンド:
  start    PRを解析してレビューランを初期化し runDir を作る
  review   レビューを1ラウンド実行する
  feedback 人間のフィードバックをレビュアーに反映する
  status   現在の状態を表示する
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid

FINDINGS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "summary", "detail", "severity", "category", "lineRef"],
                "properties": {
                    "id": {"type": "string"},
                    "summary": {"type": "string"},
                    "detail": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "category": {
                        "type": "string",
                        "enum": [
                            "correctness",
                            "spec_mismatch",
                            "security",
                            "tests",
                            "performance",
                            "speculative_future",
                            "unnecessary_fallback",
                            "code_quality",
                            "other",
                        ],
                    },
                    "lineRef": {"type": ["string", "null"]},
                },
            },
        }
    },
}

DEFAULT_CONFIG = {
    "maxRounds": 8,
    "stagnationRounds": 2,
    "blockingCategories": [
        "correctness",
        "spec_mismatch",
        "security",
        "tests",
    ],
    # blockingCategories に該当してもこのseverity以外は参考情報に回す
    # （些末な指摘で修正ループを回さないため）
    "blockingSeverities": ["high", "medium"],
    "autoWaiveCategories": ["speculative_future", "unnecessary_fallback"],
    "requireHumanOnFirstRound": True,
    "requireHumanOnNewHighSeverity": True,
    "perspectives": {
        "correctness": "変更されたコードにバグ・論理誤り・境界条件の見落としがないか",
        "spec_mismatch": "PR説明文とリンクされたIssue等の要件に対して、実装内容が一致しているか",
        "security": "セキュリティ上の問題（入力検証漏れ、権限チェック漏れ、秘密情報の露出等）がないか",
        "tests": "変更内容に対するテストが不足・欠落していないか、既存テストが誤って変更されていないか",
        "performance": "パフォーマンス上の懸念（N+1、不要なループ、大量データでの劣化等）がないか",
        "code_quality": "可読性・重複・既存のコード規約からの逸脱がないか",
    },
    "additionalInstructions": "",
    "reviewTimeoutSeconds": 3600,
    "remoteName": "origin",
    "diffStatMaxLines": 200,
    # レビュアーCLIに追加で渡す引数（例: モデルや推論の強さの指定）
    "codexExtraArgs": [],
    "claudeExtraArgs": [],
}

PROJECT_CONFIG_PATH = os.path.join(".agents", "pr-review.json")

CLAUDE_ALLOWED_TOOLS = (
    "Read Grep Glob WebSearch WebFetch "
    "Bash(gh *) Bash(git diff *) Bash(git log *) Bash(git show *) "
    "Bash(git status *) Bash(rg *)"
)
CLAUDE_DISALLOWED_TOOLS = (
    "Edit Write MultiEdit NotebookEdit "
    "Bash(rm *) Bash(git reset *) Bash(git checkout *) Bash(git commit *) Bash(git push *)"
)

WAIVER_KEYWORDS = ("以後", "今後", "以降", "今回以降")

STATE_MARKER = "===PR_REVIEW_STATE==="

# 必読リンクとして扱うGitHub URL（issues / pull / discussions）
# 注意: 必読リンクの判定規則（以下の正規表現と、後述の fetch_closing_issues 〜
# extract_web_urls の収集関数群）は
# .claude/skills/github-pr-review-draft/scripts/pr_review_draft.py と意図的に
# 重複させている。変更する場合は両方を同期すること。
GITHUB_LINK_RE = re.compile(
    r"https?://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull|discussions)/(\d+)"
)
# owner/repo#123 形式のクロスリポジトリ参照
CROSS_REF_RE = re.compile(r"(?<![\w./-])([\w.-]+)/([\w.-]+)#(\d+)\b")
# #123 形式の同一リポジトリ参照（hexカラー等の誤検出は後段の存在確認で除外する）
BARE_REF_RE = re.compile(r"(?<![\w/&#])#(\d+)\b")
# 同一リポジトリ参照の存在確認をAPIで行う上限（誤検出候補が異常に多い本文への保険）
MAX_BARE_REF_CHECKS = 15


# ---------------------------------------------------------------------------
# 状態と設定
# ---------------------------------------------------------------------------

def state_path(run_dir):
    return os.path.join(run_dir, "state.json")


def load_state(run_dir):
    with open(state_path(run_dir), encoding="utf-8") as f:
        return json.load(f)


def save_state(run_dir, state):
    tmp = state_path(run_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state_path(run_dir))


def load_config(explicit_path):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    path = explicit_path or (PROJECT_CONFIG_PATH if os.path.exists(PROJECT_CONFIG_PATH) else None)
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                overrides = json.load(f)
        except (OSError, ValueError) as e:
            die("設定ファイル {} を読み込めません: {}".format(path, e))
        unknown = sorted(set(overrides) - set(DEFAULT_CONFIG))
        if unknown:
            die("設定ファイル {} に未知のキーがあります: {}".format(path, ", ".join(unknown)))
        config.update(overrides)
        config["_configPath"] = path
    return config


def read_file(path, label):
    if not os.path.isfile(path):
        die("{} が見つかりません: {}".format(label, path))
    with open(path, encoding="utf-8") as f:
        return f.read()


def die(message):
    print("ERROR: {}".format(message), file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# git / gh ヘルパー
# ---------------------------------------------------------------------------

def run_capture(cmd, timeout=300):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        die("コマンドが見つかりません: {}（インストール状況を確認してください）".format(cmd[0]))
    except subprocess.TimeoutExpired:
        die("コマンドが {} 秒でタイムアウトしました: {}".format(timeout, " ".join(cmd)))


def git_out(args, desc):
    proc = run_capture(["git"] + args)
    if proc.returncode != 0:
        die("{} に失敗しました: {}".format(desc, proc.stderr.strip()[:500]))
    return proc.stdout.strip()


def gh_json(args, desc):
    proc = run_capture(["gh"] + args)
    if proc.returncode != 0:
        die("{} に失敗しました: {}".format(desc, proc.stderr.strip()[:500]))
    try:
        return json.loads(proc.stdout)
    except ValueError:
        die("{} の出力をJSONとして解析できません。出力先頭: {}".format(desc, proc.stdout[:300]))


def parse_pr_arg(pr_arg):
    """PRのURLまたは番号を (selector, owner, repo, number) に解析する。

    番号のみの場合 owner/repo は None（カレントリポジトリから解決する）。
    """
    m = re.match(r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", pr_arg.strip())
    if m:
        return pr_arg.strip(), m.group(1), m.group(2), int(m.group(3))
    if re.fullmatch(r"\d+", pr_arg.strip()):
        return pr_arg.strip(), None, None, int(pr_arg.strip())
    die("PRの指定を解析できません（PRのURLまたは番号を渡してください）: {}".format(pr_arg))


# ---------------------------------------------------------------------------
# 必読リンクの収集
# 注意: このブロックは github-pr-review-draft/scripts/pr_review_draft.py と
# 意図的に重複させている。変更時は両方を同期すること。
# ---------------------------------------------------------------------------

def fetch_closing_issues(owner, repo, number, warnings):
    """PRの「Development」欄にリンクされたIssueをGraphQLで取得する。"""
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){pullRequest(number:$number){"
        "closingIssuesReferences(first:20){nodes{number url title}}}}}"
    )
    proc = run_capture([
        "gh", "api", "graphql",
        "-f", "query={}".format(query),
        "-F", "owner={}".format(owner),
        "-F", "name={}".format(repo),
        "-F", "number={}".format(number),
    ])
    if proc.returncode != 0:
        warnings.append(
            "closingIssuesReferencesの取得に失敗しました（本文中のリンクのみで続行します）: {}"
            .format(proc.stderr.strip()[:200]))
        return []
    try:
        data = json.loads(proc.stdout)
        nodes = data["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
    except (ValueError, KeyError, TypeError):
        warnings.append("closingIssuesReferencesの応答を解析できませんでした（続行します）")
        return []
    return [
        {"repo": "{}/{}".format(owner, repo), "number": n["number"],
         "url": n["url"], "title": n.get("title") or "",
         "source": "closingIssuesReferences"}
        for n in nodes or []
    ]


def issue_exists(repo, number):
    """issues APIで存在確認する（IssueにもPRにもヒットする）。戻り値は (url, title) か None。"""
    proc = run_capture(["gh", "api", "repos/{}/issues/{}".format(repo, number)])
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        return data.get("html_url") or "", data.get("title") or ""
    except ValueError:
        return None


def extract_links_from_text(text, self_repo, self_number, source, warnings):
    """テキストからGitHubのIssue/PR/Discussion参照を抽出する。"""
    links = []
    for m in GITHUB_LINK_RE.finditer(text):
        owner, repo, kind, number = m.group(1), m.group(2), m.group(3), int(m.group(4))
        links.append({
            "repo": "{}/{}".format(owner, repo), "number": number,
            "url": m.group(0), "title": "", "source": source,
        })
    for m in CROSS_REF_RE.finditer(text):
        owner, repo, number = m.group(1), m.group(2), int(m.group(3))
        links.append({
            "repo": "{}/{}".format(owner, repo), "number": number,
            "url": "https://github.com/{}/{}/issues/{}".format(owner, repo, number),
            "title": "", "source": source,
        })
    # #123 形式はhexカラー等の誤検出があり得るため、存在確認できたものだけ採用する
    bare = sorted({int(m.group(1)) for m in BARE_REF_RE.finditer(text)})
    if len(bare) > MAX_BARE_REF_CHECKS:
        warnings.append(
            "#番号 形式の参照候補が{}件と多いため、先頭{}件のみ存在確認しました"
            .format(len(bare), MAX_BARE_REF_CHECKS))
        bare = bare[:MAX_BARE_REF_CHECKS]
    for number in bare:
        if number == self_number:
            continue
        found = issue_exists(self_repo, number)
        if found:
            url, title = found
            links.append({
                "repo": self_repo, "number": number,
                "url": url, "title": title, "source": source,
            })
    # PR自身への参照は除外する
    return [
        l for l in links
        if not (l["repo"].lower() == self_repo.lower() and l["number"] == self_number)
    ]


def dedup_links(links):
    seen, result = set(), []
    for l in links:
        key = (l["repo"].lower(), l["number"])
        if key in seen:
            continue
        seen.add(key)
        result.append(l)
    return result


def extract_web_urls(text):
    """GitHub以外のURL（Webドキュメント・Figma等）を抽出する。"""
    urls = re.findall(r"https?://[^\s)>\"'\]]+", text)
    return [u for u in urls if not re.match(r"https?://(?:www\.)?github\.com/", u)]


# ---------------------------------------------------------------------------
# プロンプト生成
# ---------------------------------------------------------------------------

def gh_instruction(backend):
    reason = (
        "情報源の確認実績は gh コマンドの使用から機械的に検出されるため、"
        "他の手段で読むと確認と認められず、そのラウンドの結果が採用されません。"
    )
    if backend == "codex":
        return (
            "GitHub上の情報（Issue、PR、ソースコード等）を参照する際は、必ず `gh` CLI"
            "（`gh issue view`, `gh pr view`, `gh api` 等）を使用してください。"
            "ビルトインのGitHub連携（codex_apps/github_*）は使用しないでください。" + reason
        )
    return (
        "GitHub上の情報（Issue、PR、ソースコード等）を参照する際は、必ず `gh` CLI"
        "（`gh issue view`, `gh pr view`, `gh api` 等）を使用してください。" + reason
    )


def waiver_section(waivers):
    if not waivers:
        return ""
    lines = ["", "## これまでのレビューでの調整事項", "",
             "以下の観点は対象外と判断されています。これらに該当する指摘は出力しないでください。", ""]
    for w in waivers:
        lines.append("- {} (パターン: {})".format(w["reason"], w["match"]))
    return "\n".join(lines) + "\n"


def output_requirement():
    return (
        "## 出力要件\n\n"
        "指定されたJSON Schemaに適合するJSONだけを出力してください。"
        '指摘がない場合は {"findings": []} を返してください。\n'
        "lineRef には対象コードの位置を `ファイルパス:行番号`（変更後の行番号）の形式で書いてください。\n"
    )


def diff_stat(state):
    config = state["config"]
    out = git_out(
        ["diff", "--stat", "{}...HEAD".format(state["pr"]["baseRef"])],
        "git diff --stat の実行",
    )
    lines = out.splitlines()
    limit = config["diffStatMaxLines"]
    if len(lines) > limit:
        lines = lines[:limit] + ["...（他{}行は省略。全量は git diff --stat で確認）".format(len(lines) - limit)]
    return "\n".join(lines)


def required_links_section(state):
    links = state["requiredLinks"]
    if not links:
        return ("## 必ず確認する情報源\n\n"
                "このPRの本文にリンクされたIssue等はありません。\n")
    lines = [
        "## 必ず確認する情報源（必読）",
        "",
        "以下のリンクはこのPRの要件・背景です。コードのレビューを始める前に、"
        "必ず gh CLI（`gh issue view <URL> --comments` 等）で内容を確認してください。"
        "確認せずにレビュー結果を出力した場合、その結果は採用されません。"
        "リンク先が削除・権限不足等で読めなかった場合は、gh での取得を試みたうえで"
        "レビューを続行し、そのリンクを前提にできなかったことを関連する指摘の detail に"
        "書いてください（読めないリンクのために停止する必要はありません）。",
        "",
    ]
    for l in links:
        title = " — {}".format(l["title"]) if l["title"] else ""
        lines.append("- {}{}（出典: {}）".format(l["url"], title, l["source"]))
    return "\n".join(lines) + "\n"


def reference_links_section(state):
    refs = state["referenceLinks"]
    if not refs:
        return ""
    lines = ["## 参考リンク（レビューに必要と判断した場合に確認）", ""]
    for u in refs[:20]:
        lines.append("- {}".format(u))
    if len(refs) > 20:
        lines.append("- ...（他{}件）".format(len(refs) - 20))
    return "\n".join(lines) + "\n"


def pr_header_section(state):
    pr = state["pr"]
    return "\n".join([
        "## レビュー対象のPR",
        "",
        "- URL: {}".format(pr["url"]),
        "- タイトル: {}".format(pr["title"]),
        "- ベースブランチ: {}".format(pr["baseRef"]),
        "- headブランチ: {}（このリポジトリにチェックアウト済み）".format(pr["headRefName"]),
        "",
        "### PR説明文",
        "",
        pr["body"] or "（説明文なし）",
        "",
    ])


def review_procedure_section(state):
    config = state["config"]
    perspectives = "\n".join(
        "   - {}: {}".format(k, v) for k, v in config["perspectives"].items()
    )
    parts = [
        "## レビュー手順",
        "",
        "1. 変更差分を確認してください: `git diff {}...HEAD`".format(state["pr"]["baseRef"]),
        "",
        "2. 差分だけで判断せず、変更が参照・影響する周辺コードも Read / Grep で読み、"
        "影響範囲を確認してください。",
        "",
        "3. 以下の観点でレビューを行ってください:",
        perspectives,
        "",
        "4. " + gh_instruction(state["backend"]),
        "",
        "5. レビュー対象のファイルを編集しないでください。指摘の出力だけを行ってください。",
    ]
    if config.get("additionalInstructions"):
        parts += ["", "6. 追加指示:", config["additionalInstructions"]]
    return "\n".join(parts)


def finding_criteria_section():
    """指摘の採用条件。

    敵対的検証（反証を試みる姿勢）と対にして、根拠を示せる指摘だけに絞る。
    姿勢だけを強めると些末な指摘や防御的コードの要求が増えるため、
    採用条件・severity基準・除外種別は必ずこの節でセットで渡す。
    """
    return "\n".join([
        "## 指摘の採用条件",
        "",
        "指摘として出力してよいのは、根拠を具体的に示せるものだけです。"
        "指摘の種類ごとに、示すべき根拠は次のとおりです。",
        "",
        "- 動作・セキュリティ・パフォーマンスの問題: 失敗シナリオ"
        "（どの入力・状態・操作で、何がどう壊れる・劣化するか）",
        "- 要件との不一致: PR説明文・リンクされたIssue等のどの記述と、実装のどこが食い違うか",
        "- テストの不足: 変更された動作のうち、プロジェクトの既存のテスト慣習に照らして"
        "テストされるべきなのにテストが無いのはどこか（既存テストの誤った変更を含む）",
        "- コード品質: 変更されたコードのどこが、プロジェクトのどの規約・慣習・既存実装の"
        "書き方と食い違うか（プロジェクトに根拠のない一般論の好みは根拠にならない）",
        "",
        "各指摘の detail には、該当するコードの箇所とこの根拠を書いてください。"
        "確信の持てない懸念・裏取りできなかった推測は出力しないでください。"
        "反証を試みて壊せなかった場合、指摘ゼロは正当な結果です。指摘を捻り出す必要はありません。",
        "",
        "次の種類の指摘は、実際に問題が起きる根拠を示せないため出力しないでください:",
        "",
        "- speculative_future: 「将来必要になるかもしれない」だけの指摘",
        "- unnecessary_fallback: 現実に到達しない入力・状態への防御やフォールバックの要求",
        "",
        "severity は次の基準で付けてください:",
        "",
        "- high: 本番で誤動作・データ破損・脆弱性につながる",
        "- medium: 特定の条件で誤動作する、要件と食い違う、"
        "または変更の中心的な動作にテストが無い",
        "- low: 動作には影響しない改善（コード品質・周辺的なテスト不足はここ）",
        "",
    ])


def build_initial_prompt(state, changes_summary=None):
    """初回レビュー用の完全なプロンプト。

    changes_summary が渡された場合は、2回目以降のラウンドでresumeに失敗して
    新規セッションになったとき用（必読リンク・レビュー手順を含む完全版に、
    直前の修正内容を添える）。
    """
    parts = [
        "あなたはGitHub Pull Requestの独立レビュアーです。このPRの変更はAIエージェントが"
        "作成した一次成果物で、人間による検収の前に、要件どおり正しく動くことの反証を試みる"
        "敵対的検証があなたの役割です。以下のPRの変更内容をレビューしてください。",
        "",
        pr_header_section(state),
        required_links_section(state),
        reference_links_section(state),
        "## 変更ファイル一覧（git diff --stat）",
        "",
        "```",
        diff_stat(state),
        "```",
        "",
    ]
    if changes_summary is not None:
        parts += [
            "## 直前のラウンドで加えられた修正の概要",
            "",
            "このPRはレビューと修正のループの途中です。以下は直前のレビュー指摘に対する修正内容です。",
            "",
            changes_summary or "（修正サマリーの提供なし。差分全体を確認してください）",
            "",
        ]
    parts += [
        review_procedure_section(state),
        finding_criteria_section(),
        waiver_section(state["waivers"]),
        output_requirement(),
    ]
    return "\n".join(parts)


def build_rereview_prompt(state, changes_summary):
    parts = [
        "PRのコードが修正されました。修正後の状態を改めてレビューしてください。",
        "",
        "## 修正内容の概要",
        "",
        changes_summary or "（修正サマリーの提供なし。差分全体を再確認してください）",
        "",
        "## 変更ファイル一覧（git diff --stat、修正後の最新状態）",
        "",
        "```",
        diff_stat(state),
        "```",
        "",
        "## レビュー指示",
        "",
        "1. `git diff {}...HEAD` で最新の差分を確認してください（前回から変わっています）。".format(
            state["pr"]["baseRef"]),
        "2. 前回と同じ観点でレビューし、残存する指摘があれば出力してください。",
        "3. 修正により解消された指摘は含めないでください。",
        "4. 新たに気づいた指摘があれば追加してください。",
        "5. 必読リンクの内容を前回のセッションで確認済みの場合、再取得は不要ですが、"
        "要件との照合は改めて行ってください。",
        "",
        gh_instruction(state["backend"]),
        waiver_section(state["waivers"]),
        output_requirement(),
    ]
    return "\n".join(parts)


def build_feedback_prompt(state, findings, feedback_text, fresh_session=False):
    """フィードバック反映用のプロンプト。

    fresh_session=True はresumeに失敗して新規セッションになったとき用。
    新規セッションのレビュアーは必読リンクを読んだ記憶が無いため、
    必読リンク・参考リンクの節を含める。
    """
    listed = []
    for f in findings:
        listed.append("### {} [{}] {}\n{}\n{}".format(
            f["id"], f["severity"].upper(), f["category"], f["summary"], f["detail"]))
    parts = [
        "あなたはGitHub Pull Requestの独立レビュアーです。前回のレビューに対して人間から"
        "フィードバックがあったため、調整後のレビュー結果を出力してください。",
        "",
        pr_header_section(state),
    ]
    if fresh_session:
        parts += [
            required_links_section(state),
            reference_links_section(state),
            finding_criteria_section(),
        ]
    parts += [
        "## 前回のレビュー指摘",
        "",
        "\n\n".join(listed) if listed else "（指摘なし）",
        "",
        "## 人間からのフィードバック",
        "",
        feedback_text,
        "",
        "## 指示",
        "",
        "1. waiveまたは対象外と指示された指摘は含めないでください",
        "2. フィードバックで修正方針が示された指摘は、その方針を反映して調整してください。"
        "ただし「これから修正する」と表明されただけの指摘は、コード自体はまだ修正されていない"
        "ため、そのまま残してください",
        "3. フィードバックで言及されていない指摘はそのまま残してください",
        "4. 新たに気づいた指摘があれば追加してください",
        "5. " + gh_instruction(state["backend"]),
    ]
    if fresh_session:
        parts.append(
            "6. レビュー対象のファイルを編集しないでください。指摘の出力だけを行ってください。")
    parts += [
        waiver_section(state["waivers"]),
        output_requirement(),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# レビュアーCLIの実行
# ---------------------------------------------------------------------------

def run_subprocess(cmd, prompt, timeout, stdout_path, stderr_path):
    with open(stdout_path, "w", encoding="utf-8") as out, \
            open(stderr_path, "w", encoding="utf-8") as err:
        try:
            proc = subprocess.run(
                cmd, input=prompt, stdout=out, stderr=err,
                text=True, timeout=timeout,
            )
        except FileNotFoundError:
            die("コマンドが見つかりません: {}（インストール状況を確認してください）".format(cmd[0]))
        except subprocess.TimeoutExpired:
            die("レビュアーの実行が {} 秒でタイムアウトしました。ログ: {}".format(timeout, stderr_path))
    return proc.returncode


def run_codex(state, run_dir, prompt, tag, fresh_prompt=None):
    config = state["config"]
    schema_path = os.path.join(run_dir, "findings-schema.json")
    last_path = os.path.join(run_dir, "last-message-{}.json".format(tag))
    jsonl_path = os.path.join(run_dir, "codex-{}.jsonl".format(tag))
    stderr_path = os.path.join(run_dir, "codex-{}.stderr.txt".format(tag))

    common = [
        "--json",
        "-c", 'sandbox_mode="workspace-write"',
        "-c", "sandbox_workspace_write.network_access=true",
        "--output-schema", schema_path,
        "-o", last_path,
    ] + list(config.get("codexExtraArgs") or []) + ["-"]

    def invoke(resume_id, prompt_text):
        if resume_id:
            cmd = ["codex", "exec", "resume", resume_id] + common
        else:
            cmd = ["codex", "exec"] + common
        return run_subprocess(cmd, prompt_text, config["reviewTimeoutSeconds"], jsonl_path, stderr_path)

    resumed = bool(state.get("threadId"))
    rc = invoke(state.get("threadId"), prompt)
    if rc != 0 and state.get("threadId"):
        # resume失敗時は新規セッションにフォールバックする。
        # 新規セッションのレビュアーは前ラウンドの記憶を持たないため、
        # 完全版プロンプト（fresh_prompt）があればそちらを使う
        state["threadId"] = None
        resumed = False
        fallback_prompt = fresh_prompt or prompt
        if fresh_prompt:
            with open(os.path.join(run_dir, "prompt-{}-fresh.txt".format(tag)),
                      "w", encoding="utf-8") as f:
                f.write(fresh_prompt)
        rc = invoke(None, fallback_prompt)
    state["lastRunResumed"] = resumed
    if rc != 0:
        die("codex exec が終了コード {} で失敗しました。stderr: {}".format(rc, stderr_path))

    thread_id = None
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
            if event.get("type") == "turn.failed":
                die("Codexのターンが失敗しました: {}（ログ: {}）".format(
                    json.dumps(event, ensure_ascii=False)[:500], jsonl_path))
    if thread_id:
        state["threadId"] = thread_id

    if not os.path.isfile(last_path):
        die("Codexの最終メッセージファイルがありません: {}".format(last_path))
    with open(last_path, encoding="utf-8") as f:
        return f.read(), jsonl_path


def run_claude(state, run_dir, prompt, tag, fresh_prompt=None):
    config = state["config"]
    jsonl_path = os.path.join(run_dir, "claude-{}.jsonl".format(tag))
    stderr_path = os.path.join(run_dir, "claude-{}.stderr.txt".format(tag))

    def session_env_dir(session_id):
        # Claude Code が ~/.claude/session-env/<id> を作れない環境（サンドボックス内など）
        # で Bash ツールが EPERM で失敗する問題の回避。事前に作っておく。
        d = os.path.join(os.path.expanduser("~"), ".claude", "session-env", session_id)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass

    def invoke(resume_id, prompt_text):
        cmd = ["claude", "-p",
               "--output-format", "stream-json", "--verbose",
               "--json-schema", json.dumps(FINDINGS_SCHEMA),
               "--permission-mode", "dontAsk",
               "--allowedTools", CLAUDE_ALLOWED_TOOLS,
               "--disallowedTools", CLAUDE_DISALLOWED_TOOLS]
        if resume_id:
            session_env_dir(resume_id)
            cmd += ["--resume", resume_id]
        else:
            new_id = str(uuid.uuid4())
            session_env_dir(new_id)
            cmd += ["--session-id", new_id]
        cmd += list(config.get("claudeExtraArgs") or [])
        return run_subprocess(cmd, prompt_text, config["reviewTimeoutSeconds"], jsonl_path, stderr_path)

    resumed = bool(state.get("sessionId"))
    rc = invoke(state.get("sessionId"), prompt)
    result_event = parse_claude_stream(jsonl_path)
    if (rc != 0 or result_event is None or result_event.get("is_error")) and state.get("sessionId"):
        # resume失敗時は新規セッションにフォールバックする。
        # 新規セッションのレビュアーは前ラウンドの記憶を持たないため、
        # 完全版プロンプト（fresh_prompt）があればそちらを使う
        state["sessionId"] = None
        resumed = False
        fallback_prompt = fresh_prompt or prompt
        if fresh_prompt:
            with open(os.path.join(run_dir, "prompt-{}-fresh.txt".format(tag)),
                      "w", encoding="utf-8") as f:
                f.write(fresh_prompt)
        rc = invoke(None, fallback_prompt)
        result_event = parse_claude_stream(jsonl_path)
    state["lastRunResumed"] = resumed
    if rc != 0:
        die("claude -p が終了コード {} で失敗しました。stderr: {}".format(rc, stderr_path))
    if result_event is None:
        die("claude -p の出力に result イベントがありません。ログ: {}".format(jsonl_path))
    if result_event.get("is_error"):
        die("claude -p が is_error=true を返しました。result: {} / permission_denials: {}".format(
            str(result_event.get("result"))[:500],
            json.dumps(result_event.get("permission_denials", []), ensure_ascii=False)[:500]))
    if result_event.get("session_id"):
        state["sessionId"] = result_event["session_id"]

    structured = result_event.get("structured_output")
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False), jsonl_path
    return str(result_event.get("result") or ""), jsonl_path


def parse_claude_stream(jsonl_path):
    if not os.path.isfile(jsonl_path):
        return None
    result_event = None
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                result_event = event
    return result_event


# ---------------------------------------------------------------------------
# 結果の解析と分類
# ---------------------------------------------------------------------------

def parse_findings(raw_text):
    text = raw_text.strip()
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    data = None
    for c in candidates:
        try:
            data = json.loads(c)
            break
        except ValueError:
            continue
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        die("レビュアーの出力からfindingsを抽出できませんでした。出力先頭: {}".format(text[:300]))
    findings = []
    for i, f in enumerate(data["findings"], start=1):
        if not isinstance(f, dict):
            continue
        findings.append({
            "id": str(f.get("id") or "finding-{}".format(i)),
            "summary": str(f.get("summary") or "").strip(),
            "detail": str(f.get("detail") or "").strip(),
            "severity": str(f.get("severity") or "medium").lower(),
            "category": str(f.get("category") or "other"),
            "lineRef": f.get("lineRef"),
        })
    return findings


def fingerprint(finding):
    normalized = finding["summary"].lower()
    normalized = re.sub(r"[0-9]+", "<NUM>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    key = "{}:{}".format(finding["category"], normalized)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def classify(findings, config, waivers):
    blocking, non_blocking, waived = [], [], []
    # 旧バージョンで作成されたランの state.json には blockingSeverities が無いため補完する
    blocking_severities = config.get(
        "blockingSeverities", DEFAULT_CONFIG["blockingSeverities"])
    for f in findings:
        if f["category"] in config["autoWaiveCategories"]:
            waived.append(f)
            continue
        matched = False
        haystack = (f["summary"] + " " + f["detail"]).casefold()
        for w in waivers:
            if w["match"].casefold() in haystack:
                matched = True
                break
        if matched:
            waived.append(f)
        elif (f["category"] in config["blockingCategories"]
              and f["severity"] in blocking_severities):
            blocking.append(f)
        else:
            non_blocking.append(f)
    return blocking, non_blocking, waived


def extract_waivers(feedback_text):
    waivers = []
    if not any(k in feedback_text for k in WAIVER_KEYWORDS):
        return waivers
    quoted = re.findall(r"「([^」]+)」|『([^』]+)』", feedback_text)
    matches = [a or b for a, b in quoted] or [feedback_text.strip()]
    for m in matches:
        waivers.append({"match": m, "action": "ignore", "reason": feedback_text.strip()})
    return waivers


# ---------------------------------------------------------------------------
# エビデンス検証
# ---------------------------------------------------------------------------

def link_mentioned(text, link):
    """コマンド文字列等が対象リンクに言及しているかを判定する。"""
    if link["url"] and link["url"] in text:
        return True
    n = str(link["number"])
    if re.search(r"#" + n + r"\b", text):
        return True
    if re.search(r"\b" + n + r"\b", text):
        return True
    return False


def evidence_check(state, jsonl_path):
    """レビュアーの実行ログから、必読リンクの確認と情報源の使用状況を検出する。

    検出はコマンド文字列への言及ベースであり、内容を深く読んだことまでは保証しない。
    「読まずに済ませたラウンドを弾く」ための仕組みとして使う。

    確認実績はレビュアーのセッションが継続している間（resume成功時）は
    ラウンドをまたいで引き継ぐ。前ラウンドで確認済みのリンクを再取得しないのは
    正当な振る舞いのため。新規セッション（初回・resumeフォールバック時）は
    レビュアーに記憶が無いため引き継がない。
    """
    required = state["requiredLinks"]
    carry = (state.get("evidenceCarry") or {}) if state.get("lastRunResumed") else {}
    carried_links = set(carry.get("consultedLinkKeys") or [])
    consulted = {self_key(l): False for l in required}

    gh_used = False
    builtin_github_used = False
    figma_used = False
    web_used = False

    def note_gh_text(text):
        nonlocal gh_used
        gh_used = True
        for l in required:
            if not consulted[self_key(l)] and link_mentioned(text, l):
                consulted[self_key(l)] = True

    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if state["backend"] == "codex":
                item = event.get("item") or {}
                item_type = item.get("type")
                # コマンド文字列のフィールド名には依存せず、item全体から検出する
                item_json = json.dumps(item, ensure_ascii=False)
                if item_type == "command_execution":
                    if re.search(r"(^|[\s'\"/;|&(])gh\s", item_json):
                        note_gh_text(item_json)
                    if re.search(r"(^|[\s'\"/;|&(])(curl|wget)\s", item_json):
                        web_used = True
                if item_type == "mcp_tool_call":
                    if "figma" in item_json.lower():
                        figma_used = True
                if item_type == "web_search":
                    web_used = True
                # ビルトインGitHub連携の検出は、ツール呼び出しitemの識別子フィールドに限定する。
                # 行全体の部分一致だと、レビュー中に読んだファイル内容（aggregated_output）や
                # 指示文の復唱に含まれる「codex_apps/github」という文字列で誤検出するため。
                if item_type and "tool_call" in item_type:
                    tool_identity = " ".join(
                        str(item.get(key) or "") for key in ("server", "tool", "name")
                    )
                    if "codex_apps" in tool_identity and "github" in tool_identity:
                        builtin_github_used = True
            else:
                if event.get("type") == "assistant":
                    for block in (event.get("message") or {}).get("content") or []:
                        if block.get("type") != "tool_use":
                            continue
                        name = str(block.get("name") or "")
                        if name == "Bash":
                            command = str((block.get("input") or {}).get("command") or "")
                            if re.search(r"(^|[\s'\"/;|&])gh\s", command):
                                note_gh_text(command)
                            if re.search(r"(^|[\s'\"/;|&])(curl|wget)\s", command):
                                web_used = True
                        if "figma" in name.lower():
                            figma_used = True
                        if name in ("WebSearch", "WebFetch"):
                            web_used = True
                            url = str((block.get("input") or {}).get("url") or "")
                            if url:
                                for l in required:
                                    if not consulted[self_key(l)] and link_mentioned(url, l):
                                        consulted[self_key(l)] = True

    # 今ラウンドの確認と、セッション継続中の過去ラウンドの確認実績をマージする
    effective = {k: consulted[k] or (k in carried_links) for k in consulted}
    figma_effective = figma_used or bool(carry.get("figmaUsed"))
    web_effective = web_used or bool(carry.get("webUsed"))

    missing = [l for l in required if not effective[self_key(l)]]
    required_met = (not missing) and (not state["needsFigma"] or figma_effective)

    state["evidenceCarry"] = {
        "consultedLinkKeys": sorted(k for k, v in effective.items() if v),
        "figmaUsed": figma_effective,
        "webUsed": web_effective,
    }
    return {
        "requiredLinks": [
            {"url": l["url"], "consulted": effective[self_key(l)],
             "carriedOver": effective[self_key(l)] and not consulted[self_key(l)]}
            for l in required
        ],
        "missingLinks": [l["url"] for l in missing],
        "ghUsed": gh_used,
        "needsFigma": state["needsFigma"],
        "figmaUsed": figma_effective,
        "figmaCarriedOver": figma_effective and not figma_used,
        "needsWebDocs": bool(state["referenceLinks"]),
        "webUsed": web_effective,
        "webCarriedOver": web_effective and not web_used,
        "builtinGithubAppUsed": builtin_github_used,
        "requiredMet": required_met,
    }


def self_key(link):
    return "{}#{}".format(link["repo"].lower(), link["number"])


# ---------------------------------------------------------------------------
# 報告の生成
# ---------------------------------------------------------------------------

def render_finding(f):
    lines = [
        "#### {} [{}] {}".format(f["id"], f["severity"].upper(), f["category"]),
        "**指摘:** {}".format(f["summary"]),
        "**詳細:** {}".format(f["detail"]),
    ]
    if f.get("lineRef"):
        lines.append("**該当箇所:** {}".format(f["lineRef"]))
    return "\n".join(lines)


def render_report(state, round_no, blocking, non_blocking, waived, evidence, next_action):
    backend_name = "Codex" if state["backend"] == "codex" else "Claude Code"
    lines = ["## {}レビュー結果 (Round {})".format(backend_name, round_no), ""]

    lines.append("### Blocking findings（修正が必要: {}件）".format(len(blocking)))
    lines.append("")
    for f in blocking:
        lines += [render_finding(f), ""]

    lines.append("### Non-blocking findings（参考情報: {}件）".format(len(non_blocking)))
    lines.append("")
    for f in non_blocking:
        lines += [render_finding(f), ""]

    lines.append("### Auto-waived / waived（除外: {}件）".format(len(waived)))
    lines.append("")

    def mark(used, carried=False):
        if not used:
            return "確認なし"
        if carried:
            return "確認あり（前ラウンドで確認済み・セッション継続中）"
        return "確認あり"

    lines.append("### エビデンス（レビュアーの情報源確認）")
    lines.append("")
    if evidence["requiredLinks"]:
        lines.append("- 必読リンク（PRにリンクされたIssue等、確認必須）:")
        for l in evidence["requiredLinks"]:
            lines.append("  - {}: {}".format(
                l["url"], mark(l["consulted"], l.get("carriedOver"))))
    else:
        lines.append("- PRにリンクされたIssue等はありません（確認必須のリンクなし）")
    if evidence["needsFigma"]:
        lines.append("- Figma URLあり → Figma参照: {}（必須）".format(
            mark(evidence["figmaUsed"], evidence.get("figmaCarriedOver"))))
    if evidence["needsWebDocs"]:
        lines.append("- 参考リンクあり → Web検索・取得: {}（参考）".format(
            mark(evidence["webUsed"], evidence.get("webCarriedOver"))))
    lines.append("")

    warnings = []
    if not evidence["requiredMet"]:
        missing = list(evidence["missingLinks"])
        if evidence["needsFigma"] and not evidence["figmaUsed"]:
            missing.append("Figma情報源の参照")
        warnings.append(
            "**警告:** 必読の情報源が未確認です（{}）。このレビュー結果を採用せず、"
            "再実行するかユーザーに確認してください。".format("、".join(missing)))
    if evidence["needsWebDocs"] and not evidence["webUsed"]:
        warnings.append(
            "**参考:** PRに参考リンクがありますが、Web検索・取得の使用を"
            "検出できませんでした（必須ではないため続行可能です）。")
    if evidence["builtinGithubAppUsed"]:
        warnings.append(
            "**警告:** ビルトインGitHub連携（codex_apps/github_*）の使用を検出しました。"
            "リンク先を辿れず情報が不足している恐れがあります。"
            "~/.codex/config.toml でGitHubプラグインを無効化できているか確認してください。")
    if warnings:
        lines += ["### 検証警告", ""] + warnings + [""]

    guidance = {
        "triage": ("---\n上記のレビュー報告を省略せずユーザーに提示し、フィードバックを待って"
                   "ください。例:\n"
                   "- 「finding-1は対象外としてください」→ feedbackコマンドで反映\n"
                   "- 「以後、命名に関する指摘は除外でお願いします」→ feedbackコマンドで反映"
                   "（waiverとして自動保存されます）\n"
                   "- 「全て修正をお願いします」→ コードを修正して次のreviewへ"),
        "fix_code": ("---\nblocking findingsに基づいてコードを修正し、プロジェクトのテスト・"
                     "lintを実行して通ることを確認したうえで、修正サマリーをファイルに書き出し、"
                     "`review --run <runDir> --changes-file <path>` を実行してください。"),
        "done": "---\nblocking findingsは0件です。レビューループは完了です。",
        "stagnated": ("---\n同じblocking findingsが{}ラウンド続いたため停滞と判定しました。"
                      "残った指摘をユーザーに報告して終了してください。"
                      .format(state["config"]["stagnationRounds"])),
        "max_rounds": "---\n最大ラウンド数に到達しました。残った指摘をユーザーに報告して終了してください。",
    }
    lines.append(guidance[next_action])
    return "\n".join(lines)


def render_summary(state):
    lines = ["## レビューループ完了", ""]
    status = "completed" if state.get("endReason") == "no_blocking_findings" else "stopped"
    lines.append("- ステータス: {}".format(status))
    lines.append("- 終了理由: {}".format(state.get("endReason")))
    lines.append("- 対象PR: {}".format(state["pr"]["url"]))
    lines.append("- 総ラウンド数: {}".format(len(state["rounds"])))
    lines.append("- 適用Waiver: {}件".format(len(state["waivers"])))
    lines.append("")
    lines.append("### ラウンド別サマリー")
    for r in state["rounds"]:
        lines.append("- Round {}: {}件 → {}件blocking（除外{}件）".format(
            r["round"], r["totalCount"], r["blockingCount"], r["waivedCount"]))
    return "\n".join(lines)


def emit(state, run_dir, report, machine):
    print(report)
    print()
    print(STATE_MARKER)
    machine["runDir"] = run_dir
    print(json.dumps(machine, ensure_ascii=False))


# ---------------------------------------------------------------------------
# ラウンド共通処理
# ---------------------------------------------------------------------------

def require_repo_root(state):
    top = os.path.realpath(git_out(["rev-parse", "--show-toplevel"], "gitリポジトリの確認"))
    if top != os.path.realpath(state["repoRoot"]):
        die("開始時と異なるリポジトリ・ディレクトリで実行されています。"
            "開始時のリポジトリ {} で実行してください。".format(state["repoRoot"]))


def run_reviewer(state, run_dir, prompt, tag, fresh_prompt=None):
    prompt_path = os.path.join(run_dir, "prompt-{}.txt".format(tag))
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)
    if state["backend"] == "codex":
        return run_codex(state, run_dir, prompt, tag, fresh_prompt)
    return run_claude(state, run_dir, prompt, tag, fresh_prompt)


def process_results(state, run_dir, raw_text, jsonl_path, round_no, is_feedback):
    config = state["config"]
    findings = parse_findings(raw_text)
    blocking, non_blocking, waived = classify(findings, config, state["waivers"])

    evidence = evidence_check(state, jsonl_path)

    fps = sorted(fingerprint(f) for f in blocking)
    high_fps = sorted(fingerprint(f) for f in blocking if f["severity"] == "high")
    new_high = [fp for fp in high_fps if fp not in state["seenHighFingerprints"]]
    state["seenHighFingerprints"] = sorted(set(state["seenHighFingerprints"]) | set(high_fps))

    round_entry = {
        "round": round_no,
        "totalCount": len(findings),
        "blockingCount": len(blocking),
        "waivedCount": len(waived),
        "blockingFingerprints": fps,
        "headOid": git_out(["rev-parse", "HEAD"], "HEADの取得"),
    }
    if is_feedback and state["rounds"] and state["rounds"][-1]["round"] == round_no:
        state["rounds"][-1] = round_entry
    else:
        state["rounds"].append(round_entry)
    state["lastFindings"] = {"blocking": blocking, "nonBlocking": non_blocking}

    # 停滞検出: 直近 stagnationRounds 回のblocking fingerprint集合が同一
    n = config["stagnationRounds"]
    recent = [r["blockingFingerprints"] for r in state["rounds"][-n:]]
    stagnated = (
        len(recent) >= n and len(set(map(tuple, recent))) == 1 and bool(recent[0])
    )

    needs_triage = False
    if not is_feedback:
        if round_no == 1 and config["requireHumanOnFirstRound"]:
            needs_triage = True
        elif round_no > 1 and new_high and config["requireHumanOnNewHighSeverity"]:
            needs_triage = True

    if not blocking:
        next_action = "done"
        state["endReason"] = "no_blocking_findings"
    elif stagnated:
        next_action = "stagnated"
        state["endReason"] = "stagnation"
    elif needs_triage:
        next_action = "triage"
    elif round_no >= config["maxRounds"]:
        next_action = "max_rounds"
        state["endReason"] = "max_rounds"
    else:
        next_action = "fix_code"

    if next_action in ("triage", "fix_code"):
        state["currentRound"] = round_no + 1

    save_state(state["runDir"], state)

    report = render_report(state, round_no, blocking, non_blocking, waived, evidence, next_action)
    if next_action in ("done", "stagnated", "max_rounds"):
        report += "\n\n" + render_summary(state)
    machine = {
        "round": round_no,
        "blocking": len(blocking),
        "nonBlocking": len(non_blocking),
        "waived": len(waived),
        "nextAction": next_action,
        "requiredEvidenceMet": evidence["requiredMet"],
        "requiredLinksUnread": len(evidence["missingLinks"]),
    }
    emit(state, run_dir, report, machine)


# ---------------------------------------------------------------------------
# サブコマンド
# ---------------------------------------------------------------------------

def cmd_start(args):
    config = load_config(args.config)
    if args.max_rounds is not None:
        config["maxRounds"] = args.max_rounds
    if args.stagnation_rounds is not None:
        config["stagnationRounds"] = args.stagnation_rounds
    remote = config["remoteName"]
    warnings = []

    repo_root = os.path.realpath(git_out(["rev-parse", "--show-toplevel"], "gitリポジトリの確認"))

    selector, url_owner, url_repo, _ = parse_pr_arg(args.pr)
    pr = gh_json(
        ["pr", "view", selector, "--json",
         "number,title,body,url,state,baseRefName,headRefName,headRefOid,comments"],
        "PRメタデータの取得（gh pr view）",
    )
    m = re.match(r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/\d+", pr["url"])
    if not m:
        die("PRのURLを解析できません: {}".format(pr["url"]))
    owner, repo = m.group(1), m.group(2)
    self_repo = "{}/{}".format(owner, repo)
    number = pr["number"]

    if pr["state"] != "OPEN":
        die("PRがopenではありません（state: {}）。openなPRを指定してください。".format(pr["state"]))

    # ガードレール: カレントリポジトリがPRのリポジトリと一致すること
    local_repo = gh_json(["repo", "view", "--json", "nameWithOwner"],
                         "カレントリポジトリの確認（gh repo view）")["nameWithOwner"]
    if local_repo.lower() != self_repo.lower():
        die("カレントリポジトリ（{}）がPRのリポジトリ（{}）と一致しません。"
            "対象リポジトリのローカルチェックアウトで実行してください。".format(local_repo, self_repo))

    # ガードレール: PRのheadブランチがチェックアウトされていること
    current_branch = git_out(["rev-parse", "--abbrev-ref", "HEAD"], "現在のブランチの確認")
    if current_branch != pr["headRefName"]:
        die("現在のブランチ（{}）がPRのheadブランチ（{}）と一致しません。"
            "先に `gh pr checkout {}` を実行してください。".format(
                current_branch, pr["headRefName"], pr["url"]))

    # ガードレール: 作業ツリーが汚れていないこと（未追跡ファイルは許容）
    if not args.allow_dirty:
        dirty = git_out(["status", "--porcelain", "--untracked-files=no"], "作業ツリーの確認")
        if dirty:
            die("作業ツリーに未コミットの変更があります。コミットまたは退避してから開始するか、"
                "--allow-dirty を付けてください。\n{}".format(dirty[:500]))

    # ベースブランチを取得して差分が取れることを確認する
    fetch = run_capture(["git", "fetch", remote, pr["baseRefName"]])
    if fetch.returncode != 0:
        die("ベースブランチの取得（git fetch {} {}）に失敗しました: {}".format(
            remote, pr["baseRefName"], fetch.stderr.strip()[:500]))
    base_ref = "{}/{}".format(remote, pr["baseRefName"])
    git_out(["rev-parse", "--verify", base_ref], "ベースブランチ {} の確認".format(base_ref))
    if not git_out(["diff", "--stat", "{}...HEAD".format(base_ref)], "差分の確認"):
        die("{}...HEAD の差分が空です。チェックアウト状態を確認してください。".format(base_ref))

    head_oid = git_out(["rev-parse", "HEAD"], "HEADの取得")
    if head_oid != pr["headRefOid"]:
        warnings.append(
            "ローカルHEAD（{}）がPRのhead（{}）と一致しません。"
            "ローカルの方が新しい場合は問題ありません。".format(head_oid[:12], pr["headRefOid"][:12]))

    # 必読リンクの収集: PR本文 + closingIssuesReferences（必須）、コメント（参考）
    body = pr.get("body") or ""
    comment_bodies = "\n".join(
        (c.get("body") or "") for c in (pr.get("comments") or [])
    )
    required = dedup_links(
        fetch_closing_issues(owner, repo, number, warnings)
        + extract_links_from_text(body, self_repo, number, "body", warnings)
    )
    comment_links = [
        l for l in dedup_links(
            extract_links_from_text(comment_bodies, self_repo, number, "comments", warnings))
        if self_key(l) not in {self_key(r) for r in required}
    ]
    reference_links = [l["url"] for l in comment_links] + extract_web_urls(body + "\n" + comment_bodies)
    needs_figma = bool(re.search(r"https?://(?:www\.)?figma\.com/", body + "\n" + comment_bodies))

    run_dir = tempfile.mkdtemp(prefix="pr-review-")
    schema_path = os.path.join(run_dir, "findings-schema.json")
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(FINDINGS_SCHEMA, f, ensure_ascii=False, indent=2)

    state = {
        "backend": args.backend,
        "repoRoot": repo_root,
        "pr": {
            "url": pr["url"],
            "number": number,
            "repo": self_repo,
            "title": pr["title"],
            "body": body,
            "baseRefName": pr["baseRefName"],
            "baseRef": base_ref,
            "headRefName": pr["headRefName"],
            "headRefOid": pr["headRefOid"],
        },
        "requiredLinks": required,
        "referenceLinks": reference_links,
        "needsFigma": needs_figma,
        "config": config,
        "runDir": run_dir,
        "currentRound": 1,
        "threadId": None,
        "sessionId": None,
        "lastRunResumed": False,
        "evidenceCarry": {},
        "waivers": [],
        "rounds": [],
        "seenHighFingerprints": [],
        "lastFindings": {"blocking": [], "nonBlocking": []},
        "endReason": None,
    }
    save_state(run_dir, state)
    print(json.dumps({
        "runDir": run_dir,
        "backend": args.backend,
        "pr": {"url": pr["url"], "number": number, "title": pr["title"],
               "baseRef": base_ref, "headRefName": pr["headRefName"]},
        "requiredLinks": [
            {"url": l["url"], "title": l["title"], "source": l["source"]} for l in required
        ],
        "referenceLinkCount": len(reference_links),
        "configSource": config.pop("_configPath", "defaults"),
        "maxRounds": config["maxRounds"],
        "warnings": warnings,
    }, ensure_ascii=False))
    save_state(run_dir, state)
    print("次のコマンド: review --run {}".format(run_dir), file=sys.stderr)


def cmd_review(args):
    state = load_state(args.run)
    if state.get("endReason"):
        die("このランは終了済みです（{}）。新しく start からやり直してください。".format(state["endReason"]))
    require_repo_root(state)
    round_no = state["currentRound"]
    if round_no > state["config"]["maxRounds"]:
        die("最大ラウンド数を超えています。")

    changes = args.changes or ""
    if args.changes_file:
        changes = read_file(args.changes_file, "changes-file")

    if round_no == 1:
        prompt = build_initial_prompt(state)
        fresh_prompt = None
    else:
        prompt = build_rereview_prompt(state, changes)
        # resumeに失敗して新規セッションになった場合用の完全版プロンプト
        fresh_prompt = build_initial_prompt(state, changes_summary=changes)

    raw_text, jsonl_path = run_reviewer(
        state, args.run, prompt, "round{}".format(round_no), fresh_prompt)
    process_results(state, args.run, raw_text, jsonl_path, round_no, is_feedback=False)


def cmd_feedback(args):
    state = load_state(args.run)
    require_repo_root(state)
    feedback_text = args.text or ""
    if args.file:
        feedback_text = read_file(args.file, "フィードバックファイル")
    if not feedback_text.strip():
        die("フィードバックが空です。--text か --file で渡してください。")

    new_waivers = extract_waivers(feedback_text)
    state["waivers"].extend(new_waivers)

    last_round = state["rounds"][-1]["round"] if state["rounds"] else 1
    findings = state["lastFindings"]["blocking"] + state["lastFindings"]["nonBlocking"]
    prompt = build_feedback_prompt(state, findings, feedback_text)
    # resumeに失敗して新規セッションになった場合用（必読リンクの節を含む）
    fresh_prompt = build_feedback_prompt(state, findings, feedback_text, fresh_session=True)

    raw_text, jsonl_path = run_reviewer(
        state, args.run, prompt, "feedback-round{}".format(last_round), fresh_prompt)
    if new_waivers:
        print("永続waiverを{}件追加しました。".format(len(new_waivers)), file=sys.stderr)
    process_results(state, args.run, raw_text, jsonl_path, last_round, is_feedback=True)


def cmd_status(args):
    state = load_state(args.run)
    print(json.dumps({
        "backend": state["backend"],
        "pr": {"url": state["pr"]["url"], "title": state["pr"]["title"],
               "baseRef": state["pr"]["baseRef"]},
        "requiredLinks": [l["url"] for l in state["requiredLinks"]],
        "currentRound": state["currentRound"],
        "rounds": state["rounds"],
        "waivers": state["waivers"],
        "endReason": state["endReason"],
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="PRを解析してレビューランを初期化する")
    p.add_argument("--backend", choices=["codex", "claude"], required=True)
    p.add_argument("--pr", required=True, help="レビュー対象PRのURLまたは番号")
    p.add_argument("--config", help="設定ファイルのパス（既定: ./.agents/pr-review.json）")
    p.add_argument("--max-rounds", type=int)
    p.add_argument("--stagnation-rounds", type=int)
    p.add_argument("--allow-dirty", action="store_true",
                   help="作業ツリーに未コミットの変更があっても開始する")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("review", help="レビューを1ラウンド実行する")
    p.add_argument("--run", required=True, help="startが出力したrunDir")
    p.add_argument("--changes", help="前ラウンドでコードに加えた修正のサマリー")
    p.add_argument("--changes-file", help="修正サマリーを書いたファイル")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("feedback", help="人間のフィードバックをレビューに反映する")
    p.add_argument("--run", required=True)
    p.add_argument("--text", help="フィードバック本文")
    p.add_argument("--file", help="フィードバック本文を書いたファイル")
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("status", help="現在の状態を表示する")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
