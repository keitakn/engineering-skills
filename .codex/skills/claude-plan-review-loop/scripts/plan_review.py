#!/usr/bin/env python3
"""実装計画レビューループの駆動スクリプト。

Codex CLI または Claude Code CLI をレビュアーとして呼び出し、
プロンプト生成・実行・結果解析・waiver適用・停滞検出・エビデンス検証を行う。
呼び出し側エージェントの仕事は、このスクリプトの実行、レビュー報告の提示、
blocking指摘に基づく計画ファイルの修正だけにする。

Python 3.9+ / 標準ライブラリのみで動作する。

サブコマンド:
  start    レビューランを初期化して runDir を作る
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
                            "missing_acceptance_criteria",
                            "migration_risk",
                            "security",
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
        "missing_acceptance_criteria",
        "migration_risk",
    ],
    # blockingCategories に該当してもこのseverity以外は参考情報に回す
    # （些末な指摘で修正ループを回さないため）
    "blockingSeverities": ["high", "medium"],
    "autoWaiveCategories": ["speculative_future", "unnecessary_fallback"],
    "requireHumanOnFirstRound": True,
    "requireHumanOnNewHighSeverity": True,
    "perspectives": {
        "correctness": "実装計画の内容が要件と一致しているか",
        "spec_mismatch": "仕様との不一致がないか",
        "missing_acceptance_criteria": "受け入れ基準の漏れがないか",
        "migration_risk": "マイグレーションリスクがないか",
        "security": "セキュリティ上の懸念がないか",
        "performance": "パフォーマンス上の懸念がないか",
    },
    "additionalInstructions": "",
    "reviewTimeoutSeconds": 3600,
    # レビュアーCLIに追加で渡す引数（例: モデルや推論の強さの指定）
    "codexExtraArgs": [],
    "claudeExtraArgs": [],
}

PROJECT_CONFIG_PATH = os.path.join(".agents", "plan-review.json")

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

STATE_MARKER = "===PLAN_REVIEW_STATE==="


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
    print("エラー: {}".format(message), file=sys.stderr)
    sys.exit(1)


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
    )


def finding_criteria_section():
    """指摘の採用条件。

    敵対的検証（反証を試みる姿勢）と対にして、根拠を示せる指摘だけに絞る。
    姿勢だけを強めると些末な指摘や過剰に防御的な計画の要求が増えるため、
    採用条件・severity基準・除外種別は必ずこの節でセットで渡す。
    """
    return "\n".join([
        "## 指摘の採用条件",
        "",
        "指摘として出力してよいのは、根拠を具体的に示せるものだけです。"
        "指摘の種類ごとに、示すべき根拠は次のとおりです。",
        "",
        "- 要件との不一致: 要件のどの記述と、計画のどの部分が食い違うか（要件にある事項の欠落を含む）",
        "- 破綻シナリオ: 計画のどの手順が、どの前提・制約・既存実装のもとで実装不能や手戻りになるか",
        "- 記述の曖昧さ: 計画のどの記述が、どのように複数解釈できて実装時の判断がつかないか",
        "",
        "各指摘の detail には、計画・要件・既存実装の該当箇所とこの根拠を書いてください。"
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
        "- high: このまま実装すると要件を満たせない、または大きな手戻りになる",
        "- medium: 特定の条件で要件と食い違う、または手戻りの恐れがある",
        "- low: 実装は可能だが、記述の明確化・整理で改善できる",
        "",
    ])


def build_initial_prompt(state, plan_text, req_text, changes_summary=None):
    """初回レビュー用の完全なプロンプト。

    changes_summary が渡された場合は、2回目以降のラウンドでresumeに失敗して
    新規セッションになったとき用（レビュー観点を含む完全版に、
    直前の修正内容を添える）。
    """
    config = state["config"]
    perspectives = "\n".join(
        "   - {}: {}".format(k, v) for k, v in config["perspectives"].items()
    )
    parts = [
        "あなたは実装計画の独立レビュアーです。この実装計画はAIエージェントが作成した"
        "一次成果物で、人間による検収の前に、要件を満たすこと・実装時に破綻しないことの"
        "反証を試みる敵対的検証があなたの役割です。以下の実装計画をレビューしてください。",
        "",
        "## レビュー対象の実装計画",
        "",
        plan_text,
        "",
        "## 実装計画の元となった要件（プロンプト）",
        "",
        req_text,
        "",
    ]
    if changes_summary is not None:
        parts += [
            "## 直前のラウンドで計画に加えられた修正の概要",
            "",
            "この計画はレビューと修正のループの途中です。以下は直前のレビュー指摘に対する修正内容です。",
            "",
            changes_summary or "（修正サマリーの提供なし。計画全文を確認してください）",
            "",
        ]
    parts += [
        "## レビュー指示",
        "",
        "1. 以下の観点でレビューを行ってください:",
        perspectives,
        "",
        "2. " + gh_instruction(state["backend"]),
        "",
        "3. レビュー対象のファイルを編集しないでください。指摘の出力だけを行ってください。",
    ]
    if config.get("additionalInstructions"):
        parts += ["", "4. 追加指示:", config["additionalInstructions"]]
    parts += [finding_criteria_section(),
              waiver_section(state["waivers"]), output_requirement()]
    return "\n".join(parts)


def build_rereview_prompt(state, plan_text, req_text, changes_summary):
    parts = [
        "実装計画が修正されました。修正後の計画を改めてレビューしてください。",
        "",
        "## 修正後の実装計画",
        "",
        plan_text,
        "",
        "## 実装計画の元となった要件（プロンプト）",
        "",
        req_text,
        "",
        "## 修正内容の概要",
        "",
        changes_summary or "（修正サマリーの提供なし。計画全文を再確認してください）",
        "",
        "## レビュー指示",
        "",
        "前回と同じ観点でレビューし、残存する指摘があれば出力してください。",
        "修正により解消された指摘は含めないでください。",
        "新たに気づいた指摘があれば追加してください。",
        "",
        gh_instruction(state["backend"]),
        waiver_section(state["waivers"]),
        output_requirement(),
    ]
    return "\n".join(parts)


def build_feedback_prompt(state, plan_text, req_text, findings, feedback_text,
                          fresh_session=False):
    """フィードバック反映用のプロンプト。

    fresh_session=True はresumeに失敗して新規セッションになったとき用。
    新規セッションのレビュアーは初回プロンプトの採用条件・編集禁止を
    受け取った記憶が無いため、それらを含める。
    """
    listed = []
    for f in findings:
        listed.append("### {} [{}] {}\n{}\n{}".format(
            f["id"], f["severity"].upper(), f["category"], f["summary"], f["detail"]))
    parts = [
        "あなたは実装計画の独立レビュアーです。前回のレビューに対して人間からフィードバックが"
        "あったため、調整後のレビュー結果を出力してください。",
        "",
        "## レビュー対象の実装計画",
        "",
        plan_text,
        "",
        "## 実装計画の元となった要件（プロンプト）",
        "",
        req_text,
        "",
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
        "ただし「これから修正する」と表明されただけの指摘は、計画自体はまだ修正されていない"
        "ため、そのまま残してください",
        "3. フィードバックで言及されていない指摘はそのまま残してください",
        "4. 新たに気づいた指摘があれば追加してください",
        "5. " + gh_instruction(state["backend"]),
    ]
    if fresh_session:
        parts.append(
            "6. レビュー対象のファイルを編集しないでください。指摘の出力だけを行ってください。")
        parts.append(finding_criteria_section())
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

def evidence_check(state, jsonl_path, plan_text, req_text):
    """レビュアーの実行ログから、情報源の使用状況を検出する。

    使用実績はレビュアーのセッションが継続している間（resume成功時）は
    ラウンドをまたいで引き継ぐ。前ラウンドで確認済みの情報源を再取得しないのは
    正当な振る舞いのため。新規セッション（初回・resumeフォールバック時）は
    レビュアーに記憶が無いため引き継がない。
    """
    combined = plan_text + "\n" + req_text
    needs_gh = bool(re.search(r"https?://github\.com/", combined))
    needs_figma = bool(re.search(r"https?://(?:www\.)?figma\.com/", combined))
    # GitHub/Figma以外のURL（Webドキュメント等）。こちらは必須ではなく参考扱い
    other_urls = [
        u for u in re.findall(r"https?://[^\s)>\"']+", combined)
        if not re.match(r"https?://(?:www\.)?(?:github\.com|figma\.com)/", u)
    ]

    gh_used = False
    builtin_github_used = False
    figma_used = False
    web_used = False

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
                        gh_used = True
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
                                gh_used = True
                            if re.search(r"(^|[\s'\"/;|&])(curl|wget)\s", command):
                                web_used = True
                        if "figma" in name.lower():
                            figma_used = True
                        if name in ("WebSearch", "WebFetch"):
                            web_used = True

    # 今ラウンドの使用と、セッション継続中の過去ラウンドの使用実績をマージする
    carry = (state.get("evidenceCarry") or {}) if state.get("lastRunResumed") else {}
    gh_effective = gh_used or bool(carry.get("ghUsed"))
    figma_effective = figma_used or bool(carry.get("figmaUsed"))
    web_effective = web_used or bool(carry.get("webUsed"))

    required_met = (not needs_gh or gh_effective) and (not needs_figma or figma_effective)

    state["evidenceCarry"] = {
        "ghUsed": gh_effective,
        "figmaUsed": figma_effective,
        "webUsed": web_effective,
    }
    return {
        "needsGh": needs_gh,
        "ghUsed": gh_effective,
        "ghCarriedOver": gh_effective and not gh_used,
        "needsFigma": needs_figma,
        "figmaUsed": figma_effective,
        "figmaCarriedOver": figma_effective and not figma_used,
        "needsWebDocs": bool(other_urls),
        "webUsed": web_effective,
        "webCarriedOver": web_effective and not web_used,
        "builtinGithubAppUsed": builtin_github_used,
        "requiredMet": required_met,
    }


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
        lines.append("**計画の該当箇所:** {}".format(f["lineRef"]))
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
            return "使用なし"
        if carried:
            return "使用あり（前ラウンドで確認済み・セッション継続中）"
        return "使用あり"

    lines.append("### エビデンス（レビュアーの情報源確認）")
    lines.append("")
    if evidence["needsGh"]:
        lines.append("- GitHub URLあり → gh CLI: {}（必須）".format(
            mark(evidence["ghUsed"], evidence.get("ghCarriedOver"))))
    if evidence["needsFigma"]:
        lines.append("- Figma URLあり → Figma参照: {}（必須）".format(
            mark(evidence["figmaUsed"], evidence.get("figmaCarriedOver"))))
    if evidence["needsWebDocs"]:
        lines.append("- WebドキュメントのURLあり → Web検索・取得: {}（参考）".format(
            mark(evidence["webUsed"], evidence.get("webCarriedOver"))))
    if not (evidence["needsGh"] or evidence["needsFigma"] or evidence["needsWebDocs"]):
        lines.append("- 計画・要件に外部URLはありません（確認必須の情報源なし）")
    lines.append("")

    warnings = []
    if not evidence["requiredMet"]:
        missing = []
        if evidence["needsGh"] and not evidence["ghUsed"]:
            missing.append("gh CLIによるGitHub参照")
        if evidence["needsFigma"] and not evidence["figmaUsed"]:
            missing.append("Figma情報源の参照")
        warnings.append(
            "**警告:** 必須の情報源が未確認です（{}）。このレビュー結果を採用せず、"
            "再実行するかユーザーに確認してください。".format("、".join(missing)))
    if evidence["needsWebDocs"] and not evidence["webUsed"]:
        warnings.append(
            "**参考:** 計画・要件にWebドキュメントのURLがありますが、Web検索・取得の使用を"
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
                   "- 「以後、互換性に関する指摘は除外でお願いします」→ feedbackコマンドで反映"
                   "（waiverとして自動保存されます）\n"
                   "- 「全て修正をお願いします」→ 計画ファイルを修正して次のreviewへ"),
        "fix_plan": ("---\nblocking findingsに基づいて計画ファイルを修正し、修正サマリーを"
                     "ファイルに書き出してから `review --run <runDir> --changes-file <path>` "
                     "を実行してください。"),
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

    plan_text = read_file(state["planFile"], "planFile")
    req_text = read_file(state["reqFile"], "reqFile")
    evidence = evidence_check(state, jsonl_path, plan_text, req_text)

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
        next_action = "fix_plan"

    if next_action in ("triage", "fix_plan"):
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

    plan_text = read_file(args.plan, "planFile")
    read_file(args.req, "promptFile")
    if not plan_text.strip():
        die("planFile が空です: {}".format(args.plan))

    run_dir = tempfile.mkdtemp(prefix="plan-review-")
    schema_path = os.path.join(run_dir, "findings-schema.json")
    with open(schema_path, "w", encoding="utf-8") as f:
        json.dump(FINDINGS_SCHEMA, f, ensure_ascii=False, indent=2)

    state = {
        "backend": args.backend,
        "planFile": os.path.abspath(args.plan),
        "reqFile": os.path.abspath(args.req),
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
        "planFile": state["planFile"],
        "reqFile": state["reqFile"],
        "configSource": config.pop("_configPath", "defaults"),
        "maxRounds": config["maxRounds"],
    }, ensure_ascii=False))
    save_state(run_dir, state)
    print("次のコマンド: review --run {}".format(run_dir), file=sys.stderr)


def cmd_review(args):
    state = load_state(args.run)
    if state.get("endReason"):
        die("このランは終了済みです（{}）。新しく start からやり直してください。".format(state["endReason"]))
    round_no = state["currentRound"]
    if round_no > state["config"]["maxRounds"]:
        die("最大ラウンド数を超えています。")

    changes = args.changes or ""
    if args.changes_file:
        changes = read_file(args.changes_file, "changes-file")

    plan_text = read_file(state["planFile"], "planFile")
    req_text = read_file(state["reqFile"], "reqFile")
    if round_no == 1:
        prompt = build_initial_prompt(state, plan_text, req_text)
        fresh_prompt = None
    else:
        prompt = build_rereview_prompt(state, plan_text, req_text, changes)
        # resumeに失敗して新規セッションになった場合用の完全版プロンプト
        fresh_prompt = build_initial_prompt(state, plan_text, req_text, changes_summary=changes)

    raw_text, jsonl_path = run_reviewer(
        state, args.run, prompt, "round{}".format(round_no), fresh_prompt)
    process_results(state, args.run, raw_text, jsonl_path, round_no, is_feedback=False)


def cmd_feedback(args):
    state = load_state(args.run)
    feedback_text = args.text or ""
    if args.file:
        feedback_text = read_file(args.file, "フィードバックファイル")
    if not feedback_text.strip():
        die("フィードバックが空です。--text か --file で渡してください。")

    new_waivers = extract_waivers(feedback_text)
    state["waivers"].extend(new_waivers)

    last_round = state["rounds"][-1]["round"] if state["rounds"] else 1
    findings = state["lastFindings"]["blocking"] + state["lastFindings"]["nonBlocking"]
    plan_text = read_file(state["planFile"], "planFile")
    req_text = read_file(state["reqFile"], "reqFile")
    prompt = build_feedback_prompt(state, plan_text, req_text, findings, feedback_text)
    # resume失敗時の新規セッション用（採用条件・編集禁止を含む完全版）
    fresh_prompt = build_feedback_prompt(
        state, plan_text, req_text, findings, feedback_text, fresh_session=True)

    raw_text, jsonl_path = run_reviewer(
        state, args.run, prompt, "feedback-round{}".format(last_round), fresh_prompt)
    if new_waivers:
        print("永続waiverを{}件追加しました。".format(len(new_waivers)), file=sys.stderr)
    process_results(state, args.run, raw_text, jsonl_path, last_round, is_feedback=True)


def cmd_status(args):
    state = load_state(args.run)
    print(json.dumps({
        "backend": state["backend"],
        "planFile": state["planFile"],
        "reqFile": state["reqFile"],
        "currentRound": state["currentRound"],
        "rounds": state["rounds"],
        "waivers": state["waivers"],
        "endReason": state["endReason"],
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="レビューランを初期化する")
    p.add_argument("--backend", choices=["codex", "claude"], required=True)
    p.add_argument("--plan", required=True, help="レビュー対象の実装計画ファイル")
    p.add_argument("--req", required=True, help="実装計画の元となった要件ファイル")
    p.add_argument("--config", help="設定ファイルのパス（既定: ./.agents/plan-review.json）")
    p.add_argument("--max-rounds", type=int)
    p.add_argument("--stagnation-rounds", type=int)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("review", help="レビューを1ラウンド実行する")
    p.add_argument("--run", required=True, help="startが出力したrunDir")
    p.add_argument("--changes", help="前ラウンドで計画に加えた修正のサマリー")
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
