#!/usr/bin/env python3
"""他者PRレビュー支援（github-pr-review-draft）の駆動スクリプト。

エージェント（対話セッション）がPRを下読みしてレビューコメント案を作り、
人間が承認した内容だけをGitHubに投稿するためのスクリプト。
正確さが要る操作（文脈収集・必読ゲート・行アンカー検証・根拠照合・
本文リンター・用語検査・保留レビューの作成/送信/削除）をここで決定的に行う。

安全構造の要点:
  - 公開行為は submit サブコマンド一点のみ。stage までは他人に見えない
  - submit が送る event は承認記録に焼き込まれた COMMENT / APPROVE のみ。
    承認内容はディスク上のJSONに固定し、正規化ハッシュを必須引数として照合する
  - 送信直前にGitHub上の保留レビュー実体を再取得して承認内容と照合する
  - 鮮度ゲート: head SHA・PRのopen状態・コメント数が stage 時点から
    変わっていたら送信を拒否する

Python 3.9+ / 標準ライブラリのみで動作する（git / gh は外部コマンドとして使用）。

サブコマンド:
  start    PRを解析して実行ディレクトリとworktreeを作り、文脈を収集する
  reading  必読対象の状態（読了/読めなかった）を記録する
  check    必読ゲートの充足状況を表示する
  draft    コメント案JSONを検証し、投稿物ビューを生成する
  lgtm     Approve送信に添えるLGTM画像をLGTMeowから取得して選ぶ
  approve  トリアージ結果（投稿/見送り）を確定し、承認ハッシュを発行する
  stage    承認済みコメント案を保留（PENDING）レビューとして作成する
  submit   保留レビューを送信して公開する（唯一の公開行為）
  abandon  自分が作成した保留レビューを削除する
  status   現在の状態を表示する
"""

import argparse
import difflib
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone

DEFAULT_CONFIG = {
    "remoteName": "origin",
    # 本文中で許可する @メンション（既定は全面禁止）
    "allowedMentions": [],
    # 実行ディレクトリの置き場所
    "stateDir": "~/.local/state/github-pr-review-draft",
    # ローカルcloneが無い場合のclone先
    "cloneDir": "~/.cache/github-pr-review-draft/clones",
    # 用語検査で警告する語数の上限（多すぎる警告はかえって読まれないため）
    "maxTermWarnings": 30,
}

# コメント種別 → バッジ（バッジはスクリプトだけが生成する）
BADGES = {
    "must": "https://img.shields.io/badge/review-must-red.svg",
    "imo": "https://img.shields.io/badge/review-imo-orange.svg",
    "ask": "https://img.shields.io/badge/review-ask-blue.svg",
    "nits": "https://img.shields.io/badge/review-nits-green.svg",
    "suggestion": "https://img.shields.io/badge/review-suggestion-blue.svg",
}

# Approve送信時にまとめ文の末尾へ添えるLGTM画像（画像記法はスクリプトだけが生成する）
LGTM_API_URL = "https://lgtmeow.com/api/lgtm-images"
LGTM_PAGE_URL = "https://lgtmeow.com"
LGTM_IMAGE_URL_PREFIX = "https://lgtm-images.lgtmeow.com/"


def lgtm_markdown(image_url):
    return "[![LGTMeow]({})]({})".format(image_url, LGTM_PAGE_URL)

# --- 必読リンク判定 ---------------------------------------------------------
# 注意: このブロックは codex-pr-review-loop/scripts/pr_review.py と意図的に
# 重複させている。判定規則を変更する場合は両方を同期すること。
GITHUB_LINK_RE = re.compile(
    r"https?://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull|discussions)/(\d+)"
)
CROSS_REF_RE = re.compile(r"(?<![\w./-])([\w.-]+)/([\w.-]+)#(\d+)\b")
BARE_REF_RE = re.compile(r"(?<![\w/&#])#(\d+)\b")
MAX_BARE_REF_CHECKS = 15
# --- 必読リンク判定ここまで -------------------------------------------------

KIND_LIST = ("must", "imo", "ask", "nits", "suggestion")

CJK_RE = re.compile(r"[぀-ヿ一-鿿]")

# 用語検査の対象外にする一般語（完全一致・小文字比較）
GENERAL_TERMS = {
    "api", "apis", "ci", "cd", "url", "urls", "uri", "json", "yaml", "toml",
    "sql", "http", "https", "html", "css", "xml", "csv", "tsv", "pdf",
    "github", "git", "issue", "issues", "pull", "request", "commit", "diff",
    "branch", "merge", "rebase", "push", "fetch", "clone", "tag", "release",
    "null", "true", "false", "none", "nil", "int", "str", "bool", "float",
    "lint", "linter", "test", "tests", "todo", "fixme", "readme", "doc",
    "docs", "bug", "fix", "feat", "wip", "lgtm", "nits", "imo", "ask",
    "the", "and", "for", "this", "that", "with", "not", "are", "was",
    "review", "must", "suggestion", "comment", "comments", "error", "errors",
    "log", "logs", "debug", "info", "warn", "warning", "config", "value",
    "values", "key", "keys", "file", "files", "path", "line", "code",
}


# ---------------------------------------------------------------------------
# 汎用ヘルパー
# ---------------------------------------------------------------------------

def die(message):
    print("ERROR: {}".format(message), file=sys.stderr)
    sys.exit(1)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_capture(cmd, timeout=600, cwd=None):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        die("コマンドが見つかりません: {}（インストール状況を確認してください）".format(cmd[0]))
    except subprocess.TimeoutExpired:
        die("コマンドが {} 秒でタイムアウトしました: {}".format(timeout, " ".join(cmd)))


def git_out(repo_dir, args, desc):
    proc = run_capture(["git", "-C", repo_dir] + args)
    if proc.returncode != 0:
        die("{} に失敗しました: {}".format(desc, proc.stderr.strip()[:500]))
    return proc.stdout.strip()


def gh_api(path, desc, method=None, input_path=None, fields=None, ok_codes=(0,)):
    """gh api を実行してJSONを返す。失敗時はエラー本文全文をstderrに出して終了する。"""
    cmd = ["gh", "api", path]
    if method:
        cmd += ["--method", method]
    if input_path:
        cmd += ["--input", input_path]
    for k, v in (fields or {}).items():
        cmd += ["-f", "{}={}".format(k, v)]
    proc = run_capture(cmd)
    if proc.returncode not in ok_codes:
        # 422等の内容はAPIの実挙動を知る手がかりなので、切り詰めずに出す
        print(proc.stdout, file=sys.stderr)
        die("{} に失敗しました（gh api {}）: {}".format(
            desc, path, proc.stderr.strip()[:1000]))
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        die("{} の出力をJSONとして解析できません。出力先頭: {}".format(desc, proc.stdout[:300]))


def gh_api_paginated(path_base, desc, max_pages=30):
    """一覧APIを per_page/page で全件取得する（ghのバージョン依存を避けるため手動で回す）。"""
    items = []
    for page in range(1, max_pages + 1):
        sep = "&" if "?" in path_base else "?"
        chunk = gh_api("{}{}per_page=100&page={}".format(path_base, sep, page), desc)
        if not isinstance(chunk, list):
            die("{} の応答が配列ではありません: {}".format(desc, str(chunk)[:200]))
        items.extend(chunk)
        if len(chunk) < 100:
            break
    return items


def load_json(path, default):
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def state_file(run_dir):
    return os.path.join(run_dir, "state.json")


def load_state(run_dir):
    if not os.path.isfile(state_file(run_dir)):
        die("実行ディレクトリではありません（state.jsonがありません）: {}".format(run_dir))
    return load_json(state_file(run_dir), None)


def save_state(run_dir, state):
    save_json(state_file(run_dir), state)


def load_config(explicit_path):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if explicit_path:
        try:
            with open(explicit_path, encoding="utf-8") as f:
                overrides = json.load(f)
        except (OSError, ValueError) as e:
            die("設定ファイル {} を読み込めません: {}".format(explicit_path, e))
        unknown = sorted(set(overrides) - set(DEFAULT_CONFIG))
        if unknown:
            die("設定ファイル {} に未知のキーがあります: {}".format(
                explicit_path, ", ".join(unknown)))
        config.update(overrides)
    return config


# ---------------------------------------------------------------------------
# 必読リンクの収集
# 注意: このブロックは codex-pr-review-loop/scripts/pr_review.py と意図的に
# 重複させている。判定規則を変更する場合は両方を同期すること。
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
    proc = run_capture(["gh", "api", "repos/{}/issues/{}".format(repo, number)])
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        return data.get("html_url") or "", data.get("title") or ""
    except ValueError:
        return None


def extract_links_from_text(text, self_repo, self_number, source, warnings):
    links = []
    for m in GITHUB_LINK_RE.finditer(text):
        owner, repo, _kind, number = m.group(1), m.group(2), m.group(3), int(m.group(4))
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
    urls = re.findall(r"https?://[^\s)>\"'\]]+", text)
    return [u for u in urls if not re.match(r"https?://(?:www\.)?github\.com/", u)]


def link_key(link):
    return "{}#{}".format(link["repo"].lower(), link["number"])

# --- 必読リンクの収集ここまで -----------------------------------------------


# ---------------------------------------------------------------------------
# diffの解析（行アンカー検証と抜粋表示に使う）
# ---------------------------------------------------------------------------

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_diff(diff_text):
    """unified diffを解析して、コメント可能な行の集合とハンク一覧を返す。

    戻り値: {path: {"RIGHT": {行番号}, "LEFT": {行番号}, "hunks": [hunk]}}
    hunk = {"header": str, "lines": [(side, old_ln, new_ln, text)]}
      side は "+" / "-" / " "（コンテキスト行は両側の行番号を持つ）
    """
    files = {}
    old_path = new_path = None
    path = None
    old_ln = new_ln = 0
    hunk = None

    def ensure(p):
        if p not in files:
            files[p] = {"RIGHT": set(), "LEFT": set(), "hunks": []}
        return files[p]

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            path = old_path = new_path = None
            hunk = None
            continue
        # 「--- 」「+++ 」のファイルヘッダ判定はハンク外に限定する。
        # ハンク内の同じ書き出し（例: SQLコメント「-- x」の削除は「--- x」になる）を
        # ヘッダと誤認すると行番号がずれるため
        if hunk is None and line.startswith("--- "):
            old_path = line[4:].strip()
            if old_path.startswith("a/"):
                old_path = old_path[2:]
            continue
        if hunk is None and line.startswith("+++ "):
            new_path = line[4:].strip()
            if new_path.startswith("b/"):
                new_path = new_path[2:]
            # 削除ファイルは旧パス、それ以外は新パスをコメント先パスとして使う
            path = old_path if new_path == "/dev/null" else new_path
            continue
        m = HUNK_RE.match(line)
        if m and path:
            old_ln = int(m.group(1))
            new_ln = int(m.group(3))
            hunk = {"header": line, "lines": []}
            ensure(path)["hunks"].append(hunk)
            continue
        if path is None or hunk is None:
            continue
        if line.startswith("+"):
            ensure(path)["RIGHT"].add(new_ln)
            hunk["lines"].append(("+", None, new_ln, line[1:]))
            new_ln += 1
        elif line.startswith("-"):
            ensure(path)["LEFT"].add(old_ln)
            hunk["lines"].append(("-", old_ln, None, line[1:]))
            old_ln += 1
        elif line.startswith(" "):
            ensure(path)["RIGHT"].add(new_ln)
            ensure(path)["LEFT"].add(old_ln)
            hunk["lines"].append((" ", old_ln, new_ln, line[1:]))
            old_ln += 1
            new_ln += 1
        elif line.startswith("\\"):
            continue
        else:
            # ハンク外の行（indexヘッダ等）
            continue
    return files


def anchor_excerpt(diff_files, path, side, line, context=3):
    """(path, side, line) を含むハンクから前後数行の抜粋を作る。"""
    info = diff_files.get(path)
    if not info:
        return None
    for hunk in info["hunks"]:
        rows = hunk["lines"]
        idx = None
        for i, (mark, o, n, _text) in enumerate(rows):
            ln = n if side == "RIGHT" else o
            if ln == line and (mark != "-" if side == "RIGHT" else mark != "+"):
                idx = i
                break
        if idx is None:
            continue
        lo = max(0, idx - context)
        hi = min(len(rows), idx + context + 1)
        out = []
        for i in range(lo, hi):
            mark, o, n, text = rows[i]
            ln = n if side == "RIGHT" else o
            marker = ">>" if i == idx else "  "
            out.append("{} {}{:>5} | {}".format(marker, mark, ln if ln else "", text))
        return "\n".join(out)
    return None


def in_same_hunk(diff_files, path, side, line_a, line_b):
    """2つの行番号が同一ハンク内にあるか（複数行コメントの範囲検証に使う）。"""
    info = diff_files.get(path)
    if not info:
        return False
    for hunk in info["hunks"]:
        lines = set()
        for _mark, o, n, _text in hunk["lines"]:
            ln = n if side == "RIGHT" else o
            if ln is not None:
                lines.add(ln)
        if line_a in lines and line_b in lines:
            return True
    return False


# ---------------------------------------------------------------------------
# 本文の検査（正規化・リンター・用語検査・言語判定）
# ---------------------------------------------------------------------------

def normalize_text(text):
    """比較・ハッシュ・送信のすべてで共用する正規化。"""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [l.rstrip() for l in text.split("\n")]
    return "\n".join(lines).strip("\n")


def strip_code(text):
    """リンター・用語抽出の前処理としてコードブロックとコードスパンを除去する。"""
    no_fence = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    no_span = re.sub(r"`[^`\n]*`", " ", no_fence)
    return no_span


def code_span_tokens(text):
    """バッククォート内の識別子らしき語を抽出する（用語検査の対象）。"""
    tokens = set()
    for m in re.finditer(r"`([^`\n]+)`", text):
        for t in re.findall(r"[A-Za-z_][A-Za-z0-9_.]{2,}", m.group(1)):
            tokens.add(t)
    return tokens


def lint_body(body, repo, config):
    """本文リンター。エラーの一覧を返す（空なら合格）。"""
    errors = []
    if "```suggestion" in body:
        errors.append("suggestionブロックはv1では使用できません")
    text = strip_code(body)
    # Markdownの自動リンク <https://...> はHTMLタグ判定より先に山括弧を外す
    # （URL自体の許可判定は後段のURL検査で行われる）
    text = re.sub(r"<(https?://[^>\s]+)>", r" \1 ", text)
    if re.search(r"!\[", text):
        errors.append("画像記法はバッジ（スクリプトが生成）以外使用できません")
    if "<!--" in text:
        errors.append("HTMLコメントは使用できません")
    if re.search(r"</?[A-Za-z][^>\n]*>", text):
        errors.append("生のHTMLタグは使用できません")
    for m in re.finditer(r"(?<![\w`])@([A-Za-z0-9][A-Za-z0-9-]*)", text):
        if m.group(1) not in (config.get("allowedMentions") or []):
            errors.append("@メンションは使用できません: @{}".format(m.group(1)))
    for m in re.finditer(r"(?<![\w/&#])#(\d+)\b", text):
        errors.append(
            "裸の #{} 参照は使用できません（意図する場合は完全なURLで書いてください）"
            .format(m.group(1)))
    for line in text.split("\n"):
        if re.match(r"^#{1,2}\s", line):
            errors.append("レベル1〜2の見出しは使用できません: {}".format(line[:40]))
    for url in re.findall(r"https?://[^\s)>\"'\]]+", text):
        allowed = re.match(
            r"https://github\.com/[\w.-]+/[\w.-]+/(issues|pull|commit)/", url)
        if not allowed:
            errors.append(
                "このURLは使用できません（同一ホストのIssue/PR/コミットのみ許可）: {}"
                .format(url))
    return errors


def extract_terms(text):
    """用語検査の候補語を抽出する（カタカナ語・英字識別子・コード内識別子）。

    漢字の連続は抽出しない。辞書なしでは一般的な複合語（例: 事前検査）と
    造語を区別できず誤検知ばかりになるため、日本語の造語の抑止は
    SKILL.mdの行動規範とpreviewの人間確認が担う。
    """
    plain = strip_code(text)
    terms = set()
    for m in re.finditer(r"[ァ-ヶー]{4,}", plain):
        terms.add(m.group(0))
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_.-]{2,}", plain):
        t = m.group(0).strip(".-")
        if t.lower() not in GENERAL_TERMS and not re.match(r"^https?$", t.lower()):
            terms.add(t)
    terms |= code_span_tokens(text)
    return terms


def term_grounded(term, corpus_lower, worktree):
    """語が共有文脈（収集済みテキストまたはworktree内のファイル）に存在するか。"""
    if term.lower() in corpus_lower:
        return True
    if worktree and os.path.isdir(worktree):
        proc = run_capture(
            ["git", "-C", worktree, "grep", "-I", "-l", "-F", "--", term],
            timeout=120)
        if proc.returncode == 0:
            return True
        proc = run_capture(
            ["git", "-C", worktree, "grep", "-I", "-l", "-i", "-F", "--", term],
            timeout=120)
        if proc.returncode == 0:
            return True
    # 「識別子.識別子」（例: テーブル名.カラム名）は、ドット結合の全体が
    # 文脈に現れなくても、各部分が個別に根拠付けできれば根拠ありとする
    if "." in term:
        parts = [p for p in term.split(".") if p]
        if len(parts) >= 2 and all(
                term_grounded(p, corpus_lower, worktree) for p in parts):
            return True
    return False


def detect_language(texts):
    """テキスト群の主要言語を判定する（日本語/英語のみの粗い判定）。"""
    joined = "\n".join(texts)
    cjk = len(CJK_RE.findall(joined))
    letters = len(re.findall(r"[A-Za-z]", joined))
    if cjk + letters < 40:
        return "unknown"
    return "ja" if cjk >= (cjk + letters) * 0.05 else "en"


def body_language(text):
    return "ja" if CJK_RE.search(text) else "en"


# ---------------------------------------------------------------------------
# 承認対象の正規化とハッシュ
# ---------------------------------------------------------------------------

def finalize_body(kind, body):
    """種別からバッジ行を生成して本文の先頭に付ける（バッジはここでだけ作る）。"""
    return "![badge]({})\n\n{}".format(BADGES[kind], normalize_text(body))


def approved_payload(state, summary, comments, event="COMMENT"):
    """承認対象の正規形。PR・head・投稿名義・送信種別を含めてハッシュに焼き込む。"""
    return {
        "pr": state["pr"]["url"],
        "headOid": state["pr"]["headRefOid"],
        "login": state["login"],
        "event": event,
        "summary": normalize_text(summary or ""),
        "comments": sorted(
            [
                {
                    "id": c["id"],
                    "kind": c["kind"],
                    "path": c["path"],
                    "side": c["side"],
                    "line": c["line"],
                    "startLine": c.get("start_line"),
                    "startSide": c.get("start_side"),
                    "body": c["bodyFinal"],
                }
                for c in comments
            ],
            key=lambda c: c["id"],
        ),
    }


def payload_hash(payload):
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# GitHub上の状態の取得（鮮度ゲート・照合に使う）
# ---------------------------------------------------------------------------

def fetch_pr_live(repo, number):
    return gh_api(
        "repos/{}/pulls/{}".format(repo, number), "PRの現在状態の取得")


def fetch_reviews(repo, number):
    return gh_api_paginated(
        "repos/{}/pulls/{}/reviews".format(repo, number), "レビュー一覧の取得")


def fetch_review_comments(repo, number):
    return gh_api_paginated(
        "repos/{}/pulls/{}/comments".format(repo, number), "行コメント一覧の取得")


def fetch_issue_comments(repo, number):
    return gh_api_paginated(
        "repos/{}/issues/{}/comments".format(repo, number), "Issueコメント一覧の取得")


def fetch_pending_review(state):
    """自分の保留レビュー（PENDING）を返す。無ければNone。"""
    reviews = fetch_reviews(state["pr"]["repo"], state["pr"]["number"])
    mine = [
        r for r in reviews
        if r.get("state") == "PENDING"
        and (r.get("user") or {}).get("login") == state["login"]
    ]
    return mine[0] if mine else None


def collect_counts(state):
    """鮮度ゲート用のコメント数スナップショット（自分の保留レビューは除外）。"""
    repo, number = state["pr"]["repo"], state["pr"]["number"]
    reviews = fetch_reviews(repo, number)
    other_reviews = [
        r for r in reviews
        if not (r.get("state") == "PENDING"
                and (r.get("user") or {}).get("login") == state["login"])
    ]
    return {
        "reviews": len(other_reviews),
        "reviewComments": len(fetch_review_comments(repo, number)),
        "issueComments": len(fetch_issue_comments(repo, number)),
    }


def check_identity(state):
    login = gh_api("user", "認証アカウントの確認").get("login")
    if login != state["login"]:
        die("ghの認証アカウント（{}）が開始時（{}）と異なります。"
            "gh auth switch 等でアカウントを確認してください。".format(login, state["login"]))
    return login


# ---------------------------------------------------------------------------
# start: 文脈収集と実行ディレクトリの初期化
# ---------------------------------------------------------------------------

def parse_pr_url(pr_arg):
    m = re.match(r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", pr_arg.strip())
    if not m:
        die("PRの指定を解析できません（PRの完全なURLを渡してください）: {}".format(pr_arg))
    return m.group(1), m.group(2), int(m.group(3))


def repo_from_remote(repo_dir, remote):
    proc = run_capture(["git", "-C", repo_dir, "remote", "get-url", remote])
    if proc.returncode != 0:
        return None
    m = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$",
                  proc.stdout.strip())
    return "{}/{}".format(m.group(1), m.group(2)) if m else None


def resolve_clone(config, self_repo, warnings, repo_dir_arg):
    """レビュー対象リポジトリのローカルcloneを決める。無ければcloneする。"""
    remote = config["remoteName"]
    if repo_dir_arg:
        d = os.path.realpath(os.path.expanduser(repo_dir_arg))
        found = repo_from_remote(d, remote)
        if not found or found.lower() != self_repo.lower():
            die("--repo-dir {} のremote（{}）がPRのリポジトリ（{}）と一致しません。"
                .format(d, found, self_repo))
        return d
    proc = run_capture(["git", "rev-parse", "--show-toplevel"])
    if proc.returncode == 0:
        d = os.path.realpath(proc.stdout.strip())
        found = repo_from_remote(d, remote)
        if found and found.lower() == self_repo.lower():
            return d
    clone_root = os.path.expanduser(config["cloneDir"])
    os.makedirs(clone_root, exist_ok=True)
    d = os.path.join(clone_root, self_repo.replace("/", "__"))
    if os.path.isdir(os.path.join(d, ".git")):
        return d
    warnings.append("ローカルcloneが見つからないため {} にcloneしました".format(d))
    proc = run_capture(
        ["gh", "repo", "clone", self_repo, d, "--", "--filter=blob:none"],
        timeout=1800)
    if proc.returncode != 0:
        die("リポジトリのcloneに失敗しました: {}".format(proc.stderr.strip()[:500]))
    return d


def remove_stale_worktree(clone_dir, worktree):
    """既存worktreeを、それを登録しているリポジトリ経由で削除する。

    worktreeの置き場所はPRごとに固定だが、cloneの解決は実行時のカレント
    ディレクトリに依存するため、前回と別のclone（例: ローカルcloneと
    キャッシュclone）に登録されたworktreeが残っていることがある。
    登録先以外のリポジトリからの worktree remove は失敗するので、
    worktree自身から登録先を特定して削除し、失敗はエラーとして報告する。
    """
    owner_repo = clone_dir
    proc = run_capture(
        ["git", "-C", worktree, "rev-parse",
         "--path-format=absolute", "--git-common-dir"])
    if proc.returncode == 0 and proc.stdout.strip():
        common = proc.stdout.strip()
        owner_repo = os.path.dirname(common) \
            if os.path.basename(common) == ".git" else common
    proc = run_capture(
        ["git", "-C", owner_repo, "worktree", "remove", "--force", worktree])
    if proc.returncode != 0:
        die("既存worktreeの削除に失敗しました: {}\n"
            "手動で削除してから start をやり直してください:\n"
            "  git -C \"{}\" worktree remove --force \"{}\"".format(
                proc.stderr.strip()[:300], owner_repo, worktree))


def setup_worktree(clone_dir, run_dir, number, head_oid, remote):
    """PRのheadを専用worktreeに展開する（現在のチェックアウトには触れない）。"""
    worktree = os.path.join(run_dir, "worktree")
    proc = run_capture(
        ["git", "-C", clone_dir, "fetch", remote, "pull/{}/head".format(number)],
        timeout=1800)
    if proc.returncode != 0:
        die("PRのheadの取得（git fetch {} pull/{}/head）に失敗しました: {}".format(
            remote, number, proc.stderr.strip()[:500]))
    if os.path.isdir(worktree):
        cur = run_capture(["git", "-C", worktree, "rev-parse", "HEAD"])
        if cur.returncode == 0 and cur.stdout.strip() == head_oid:
            return worktree
        remove_stale_worktree(clone_dir, worktree)
    proc = run_capture(
        ["git", "-C", clone_dir, "worktree", "add", "--detach", worktree, head_oid])
    if proc.returncode != 0:
        # headOidが直接解決できない場合はFETCH_HEADで再試行する
        proc = run_capture(
            ["git", "-C", clone_dir, "worktree", "add", "--detach", worktree, "FETCH_HEAD"])
        if proc.returncode != 0:
            die("worktreeの作成に失敗しました: {}".format(proc.stderr.strip()[:500]))
        cur = git_out(worktree, ["rev-parse", "HEAD"], "worktreeのHEAD確認")
        if cur != head_oid:
            die("worktreeのHEAD（{}）がPRのhead（{}）と一致しません。"
                .format(cur[:12], head_oid[:12]))
    return worktree


def cmd_start(args):
    config = load_config(args.config)
    warnings = []
    owner, repo_name, number = parse_pr_url(args.pr)
    self_repo = "{}/{}".format(owner, repo_name)

    login = gh_api("user", "認証アカウントの確認").get("login")

    pr = fetch_pr_live(self_repo, number)
    if pr.get("state") != "open":
        die("PRがopenではありません（state: {}）。openなPRを指定してください。"
            .format(pr.get("state")))
    if pr.get("draft"):
        warnings.append("このPRはドラフト状態です")
    head_oid = pr["head"]["sha"]

    state_root = os.path.expanduser(config["stateDir"])
    run_dir = os.path.join(
        state_root, "{}__pr{}".format(self_repo.replace("/", "__"), number))
    os.makedirs(os.path.join(run_dir, "collected"), exist_ok=True)
    os.makedirs(os.path.join(run_dir, "render"), exist_ok=True)

    prev_state = load_json(state_file(run_dir), None)
    if prev_state and prev_state.get("staged") and \
            prev_state["pr"]["headRefOid"] != head_oid:
        die("前回のstage後にPRのheadが変わっています（{} → {}）。"
            "先に abandon で保留レビューを削除してから start をやり直してください。"
            .format(prev_state["pr"]["headRefOid"][:12], head_oid[:12]))
    if prev_state and prev_state["pr"]["headRefOid"] != head_oid:
        # 旧headのdiffに対して検証・承認した成果物は無効。残すと旧検証結果の
        # 行アンカーが新headに紐付け直される事故になるため破棄する
        for name in ("draft_normalized.json", "approved.json"):
            p = os.path.join(run_dir, name)
            if os.path.isfile(p):
                os.remove(p)
                warnings.append(
                    "PRのheadが変わったため {} を破棄しました"
                    "（draft・approveのやり直しが必要です）".format(name))

    clone_dir = resolve_clone(config, self_repo, warnings, args.repo_dir)
    worktree = setup_worktree(
        clone_dir, run_dir, number, head_oid, config["remoteName"])

    # 収集: diff・コメント全系統・CI実結果
    proc = run_capture(["gh", "pr", "diff", pr["html_url"]], timeout=600)
    if proc.returncode != 0:
        die("diffの取得（gh pr diff）に失敗しました: {}".format(proc.stderr.strip()[:500]))
    write_text(os.path.join(run_dir, "collected", "diff.patch"), proc.stdout)

    review_comments = fetch_review_comments(self_repo, number)
    issue_comments = fetch_issue_comments(self_repo, number)
    reviews = fetch_reviews(self_repo, number)
    save_json(os.path.join(run_dir, "collected", "review_comments.json"), review_comments)
    save_json(os.path.join(run_dir, "collected", "issue_comments.json"), issue_comments)
    save_json(os.path.join(run_dir, "collected", "reviews.json"), reviews)
    save_json(os.path.join(run_dir, "collected", "pr.json"), pr)

    # check-runs はオブジェクト応答のため paginated ヘルパーは使わない
    checks_obj = gh_api(
        "repos/{}/commits/{}/check-runs?per_page=100".format(self_repo, head_oid),
        "CIチェック実行結果の取得")
    check_runs = (checks_obj or {}).get("check_runs") or []
    status_obj = gh_api(
        "repos/{}/commits/{}/status".format(self_repo, head_oid),
        "コミットステータスの取得")
    save_json(os.path.join(run_dir, "collected", "checks.json"), {
        "checkRuns": [
            {"name": c.get("name"), "status": c.get("status"),
             "conclusion": c.get("conclusion")} for c in check_runs
        ],
        "commitStatus": {
            "state": (status_obj or {}).get("state"),
            "contexts": [
                {"context": s.get("context"), "state": s.get("state")}
                for s in (status_obj or {}).get("statuses") or []
            ],
        },
    })
    proc = run_capture(["gh", "pr", "checks", pr["html_url"]])
    write_text(os.path.join(run_dir, "collected", "checks.txt"),
               proc.stdout + ("\n" + proc.stderr if proc.stderr else ""))

    # 必読対象の列挙
    body = pr.get("body") or ""
    required_links = dedup_links(
        fetch_closing_issues(owner, repo_name, number, warnings)
        + extract_links_from_text(body, self_repo, number, "body", warnings)
    )
    comment_text = "\n".join(
        (c.get("body") or "") for c in issue_comments + review_comments)
    comment_links = [
        l for l in dedup_links(
            extract_links_from_text(comment_text, self_repo, number, "comments", warnings))
        if link_key(l) not in {link_key(r) for r in required_links}
    ]
    reference_links = [l["url"] for l in comment_links] + \
        extract_web_urls(body + "\n" + comment_text)

    # 必読リンク先Issueの本文も取得しておく（用語検査の共有文脈に使う）
    linked_issues = []
    for l in required_links:
        proc = run_capture(["gh", "api", "repos/{}/issues/{}".format(l["repo"], l["number"])])
        if proc.returncode == 0:
            try:
                d = json.loads(proc.stdout)
                linked_issues.append({
                    "url": l["url"],
                    "title": d.get("title") or "",
                    "body": d.get("body") or "",
                })
            except ValueError:
                pass
    save_json(os.path.join(run_dir, "collected", "linked_issues.json"), linked_issues)

    required_reading = [{
        "id": "R1", "type": "pr_body", "url": pr["html_url"],
        "title": "PR説明文", "source": "pr",
    }]
    for i, l in enumerate(required_links, start=2):
        required_reading.append({
            "id": "R{}".format(i), "type": "issue", "url": l["url"],
            "title": l["title"], "source": l["source"],
        })
    n = len(required_reading)
    required_reading.append({
        "id": "R{}".format(n + 1), "type": "ci",
        "url": os.path.join(run_dir, "collected", "checks.txt"),
        "title": "CIの実行結果", "source": "checks",
    })
    required_reading.append({
        "id": "R{}".format(n + 2), "type": "existing_comments",
        "url": os.path.join(run_dir, "collected"),
        "title": "既存のレビュー・コメント全系統", "source": "comments",
    })

    # 対象リポジトリの主要言語の判定
    samples = [body] + [
        (c.get("body") or "") for c in
        (review_comments + issue_comments)[-30:]
    ] + [(r.get("body") or "") for r in reviews[-10:]]
    language = detect_language(samples)

    waivers = load_json(os.path.join(run_dir, "waivers.json"), [])

    state = {
        "createdAt": now_iso(),
        "config": config,
        "login": login,
        "pr": {
            "url": pr["html_url"],
            "repo": self_repo,
            "number": number,
            "title": pr.get("title") or "",
            "body": body,
            "baseRefName": pr["base"]["ref"],
            "headRefOid": head_oid,
            "authorLogin": (pr.get("user") or {}).get("login") or "",
            "isDraft": bool(pr.get("draft")),
        },
        "cloneDir": clone_dir,
        "worktree": worktree,
        "requiredReading": required_reading,
        "referenceLinks": reference_links,
        "language": language,
        "staged": (prev_state or {}).get("staged"),
        "submitted": (prev_state or {}).get("submitted") or [],
    }
    save_state(run_dir, state)
    if not os.path.isfile(os.path.join(run_dir, "reading.json")):
        save_json(os.path.join(run_dir, "reading.json"), {})

    files = pr.get("changed_files")
    print(json.dumps({
        "runDir": run_dir,
        "worktree": worktree,
        "cloneDir": clone_dir,
        "login": login,
        "pr": {"url": pr["html_url"], "title": state["pr"]["title"],
               "author": state["pr"]["authorLogin"],
               "baseRef": state["pr"]["baseRefName"],
               "headOid": head_oid,
               "changedFiles": files,
               "additions": pr.get("additions"),
               "deletions": pr.get("deletions")},
        "requiredReading": [
            {"id": r["id"], "type": r["type"], "url": r["url"], "title": r["title"]}
            for r in required_reading
        ],
        "referenceLinks": reference_links[:20],
        "language": language,
        "existingReviewComments": len(review_comments),
        "existingIssueComments": len(issue_comments),
        "waivers": [
            {"path": w.get("path"), "line": w.get("line"),
             "kind": w.get("kind"), "summary": (w.get("body") or "")[:80]}
            for w in waivers
        ],
        "warnings": warnings,
    }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# reading / check: 必読ゲート
# ---------------------------------------------------------------------------

def reading_target(state, item):
    """必読記録が指す対象の識別子。CIはheadに紐づくため、head更新で記録が無効になる。"""
    if item["type"] == "ci":
        return "{}@{}".format(item["url"], state["pr"]["headRefOid"])
    return item["url"]


def cmd_reading(args):
    state = load_state(args.run)
    reading_path = os.path.join(args.run, "reading.json")
    reading = load_json(reading_path, {})
    items = {r["id"]: r for r in state["requiredReading"]}
    if args.item not in items:
        die("必読対象のIDではありません: {}（有効: {}）".format(
            args.item, ", ".join(sorted(items))))
    if args.read and args.unreadable:
        die("--read と --unreadable は同時に指定できません")
    target = reading_target(state, items[args.item])
    if args.read:
        if not (args.summary or "").strip():
            die("--read には --summary（読んだ内容の一行要約）が必要です")
        reading[args.item] = {
            "status": "read", "summary": args.summary.strip(),
            "target": target, "at": now_iso()}
    elif args.unreadable:
        if not (args.reason or "").strip():
            die("--unreadable には --reason（読めなかった理由）が必要です")
        reading[args.item] = {
            "status": "unreadable", "reason": args.reason.strip(),
            "target": target, "at": now_iso()}
    else:
        die("--read か --unreadable のどちらかを指定してください")
    save_json(reading_path, reading)
    print(json.dumps(reading_gate(state, reading), ensure_ascii=False))


def reading_gate(state, reading):
    """必読ゲート。IDだけでなく対象の一致まで確認する。

    startの再実行で必読対象の並びや内容（リンクIssueの増減・headの更新）が
    変わった場合、旧記録が別対象の読了として流用されないようにする。
    """
    missing = []
    for r in state["requiredReading"]:
        rec = reading.get(r["id"])
        if not rec or rec.get("target") != reading_target(state, r):
            missing.append(r["id"])
    valid_ids = {r["id"] for r in state["requiredReading"]} - set(missing)
    unreadable = [
        {"id": rid, "reason": v.get("reason")}
        for rid, v in reading.items()
        if v.get("status") == "unreadable" and rid in valid_ids
    ]
    return {"complete": not missing, "missing": missing, "unreadable": unreadable}


def cmd_check(args):
    state = load_state(args.run)
    reading = load_json(os.path.join(args.run, "reading.json"), {})
    print(json.dumps(reading_gate(state, reading), ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# draft: コメント案の検証と投稿物ビューの生成
# ---------------------------------------------------------------------------

def build_corpus(run_dir, state):
    """用語検査の共有文脈（収集済みテキストの連結・小文字化）を作る。"""
    parts = [state["pr"]["title"], state["pr"]["body"]]
    for name in ("review_comments.json", "issue_comments.json", "reviews.json"):
        for c in load_json(os.path.join(run_dir, "collected", name), []):
            parts.append(c.get("body") or "")
    for i in load_json(os.path.join(run_dir, "collected", "linked_issues.json"), []):
        parts.append(i.get("title") or "")
        parts.append(i.get("body") or "")
    diff_path = os.path.join(run_dir, "collected", "diff.patch")
    if os.path.isfile(diff_path):
        parts.append(read_text(diff_path))
    # 読めた外部資料（PR本文等からリンクされたGitHub外の資料）のテキストを
    # collected/extra/ に保存しておくと、共有文脈に含める
    extra_dir = os.path.join(run_dir, "collected", "extra")
    if os.path.isdir(extra_dir):
        for name in sorted(os.listdir(extra_dir)):
            fpath = os.path.join(extra_dir, name)
            if not os.path.isfile(fpath):
                continue
            try:
                parts.append(read_text(fpath))
            except (OSError, UnicodeDecodeError):
                continue
    return "\n".join(p for p in parts if p).lower()


def validate_quote(worktree, quote):
    """引用の実在検証: worktree（PRのhead時点）の実内容と照合する。"""
    path = quote.get("path") or ""
    start = quote.get("start_line")
    end = quote.get("end_line", start)
    text = quote.get("text") or ""
    if not path or not isinstance(start, int) or not isinstance(end, int):
        return "quotes には path / start_line / end_line / text が必要です"
    fpath = os.path.join(worktree, path)
    if not os.path.isfile(fpath):
        return "引用先のファイルがPRのhead時点に存在しません: {}".format(path)
    try:
        lines = read_text(fpath).split("\n")
    except UnicodeDecodeError:
        return "引用先のファイルをテキストとして読めません: {}".format(path)
    if start < 1 or end > len(lines) or start > end:
        return "引用の行範囲が不正です: {}:{}-{}（ファイルは{}行）".format(
            path, start, end, len(lines))
    actual = normalize_text("\n".join(lines[start - 1:end]))
    if normalize_text(text) != actual:
        return ("引用がファイルの実内容と一致しません: {}:{}-{}\n実内容:\n{}"
                .format(path, start, end, actual[:500]))
    return None


def validate_ci_claim(run_dir, ci):
    checks = load_json(os.path.join(run_dir, "collected", "checks.json"), {})
    names = {}
    for c in checks.get("checkRuns") or []:
        names[(c.get("name") or "").lower()] = (c.get("conclusion") or c.get("status") or "")
    for s in (checks.get("commitStatus") or {}).get("contexts") or []:
        names[(s.get("context") or "").lower()] = s.get("state") or ""
    check = (ci.get("check") or "").lower()
    conclusion = (ci.get("conclusion") or "").lower()
    if check not in names:
        return ("CI根拠のチェック名が実結果に存在しません: {}（実在: {}）"
                .format(ci.get("check"), ", ".join(sorted(names)) or "なし"))
    if conclusion != (names[check] or "").lower():
        return ("CI根拠の結果が実結果と一致しません: {} は {} です（指定: {}）"
                .format(ci.get("check"), names[check], ci.get("conclusion")))
    return None


# 「CIが」「CIの」のような日本語の続き書きでも一致するよう、境界はASCII英字のみで判定する
CI_MENTION_RE = re.compile(
    r"(?<![A-Za-z])CI(?![A-Za-z])|GitHub Actions|ワークフロー|チェックが|テストが(通|落|失敗|成功)")


def cmd_draft(args):
    state = load_state(args.run)
    run_dir = args.run
    config = state["config"]
    reading = load_json(os.path.join(run_dir, "reading.json"), {})
    gate = reading_gate(state, reading)
    if not gate["complete"]:
        die("必読ゲートが未充足です（未記録: {}）。reading で全件記録してから"
            "draft を実行してください。".format(", ".join(gate["missing"])))

    if not os.path.isfile(args.file):
        die("コメント案ファイルがありません: {}".format(args.file))
    try:
        draft = json.loads(read_text(args.file))
    except ValueError as e:
        die("コメント案JSONを解析できません: {}".format(e))

    comments = draft.get("comments")
    if not isinstance(comments, list):
        die('コメント案JSONに "comments"（配列）がありません')
    summary = draft.get("summary") or ""

    diff_files = parse_diff(
        read_text(os.path.join(run_dir, "collected", "diff.patch")))
    corpus_lower = build_corpus(run_dir, state)
    approved_terms = set(load_json(os.path.join(run_dir, "approved_terms.json"), []))
    waivers = load_json(os.path.join(run_dir, "waivers.json"), [])
    existing = load_json(
        os.path.join(run_dir, "collected", "review_comments.json"), [])
    unreadable_ids = {u["id"] for u in gate["unreadable"]}

    errors = []
    warnings = []
    seen_ids = set()
    normalized = []

    if summary:
        for e in lint_body(summary, state["pr"]["repo"], config):
            errors.append("summary: {}".format(e))
        if body_language(summary) == "ja" and \
                normalize_text(summary).endswith("。"):
            warnings.append(
                "summary: まとめ文の末尾が「。」で終わっています。最後の文は"
                "「！」・絵文字・「）」などで締めてください（行動規範の文末の規則）")

    for i, c in enumerate(comments):
        cid = str(c.get("id") or "")
        label = cid or "comments[{}]".format(i)
        if not re.fullmatch(r"C\d+", cid):
            errors.append("{}: id は C1, C2, ... の形式で必須です".format(label))
            continue
        if cid in seen_ids:
            errors.append("{}: id が重複しています".format(cid))
            continue
        seen_ids.add(cid)

        kind = c.get("kind")
        if kind not in KIND_LIST:
            errors.append("{}: kind は {} のいずれかが必須です".format(
                cid, "/".join(KIND_LIST)))
            continue
        body = c.get("body") or ""
        if not body.strip():
            errors.append("{}: body が空です".format(cid))
            continue
        if "img.shields.io" in body:
            errors.append("{}: バッジはスクリプトが生成します。本文に書かないでください".format(cid))
            continue
        reason = (c.get("reason") or "").strip()
        if not reason:
            errors.append(
                "{}: reason（放置すると何が起きるか / askの場合は何を確認したいか）が"
                "必須です".format(cid))

        path = c.get("path") or ""
        side = c.get("side") or "RIGHT"
        line = c.get("line")
        if side not in ("RIGHT", "LEFT"):
            errors.append("{}: side は RIGHT か LEFT です".format(cid))
            continue
        if not path or not isinstance(line, int):
            errors.append("{}: path と line（整数）が必須です".format(cid))
            continue
        info = diff_files.get(path)
        if not info:
            errors.append(
                "{}: {} はこのPRのdiffに含まれないファイルです。"
                "diff外への指摘は「投稿しない指摘」として報告書に回してください".format(cid, path))
            continue
        if line not in info[side]:
            near = sorted(info[side])[:0] or []
            errors.append(
                "{}: {}:{}（{}）はdiff上にコメントできる行ではありません。"
                "diffの抜粋を確認して行番号を直してください".format(cid, path, line, side))
            continue
        start_line = c.get("start_line")
        start_side = c.get("start_side")
        if (start_line is None) != (start_side is None):
            errors.append("{}: 複数行コメントは start_line と start_side を両方指定します".format(cid))
            continue
        if start_line is not None:
            if start_side != side:
                errors.append("{}: v1では start_side と side は同じ側のみ対応します".format(cid))
                continue
            if not isinstance(start_line, int) or start_line >= line:
                errors.append("{}: start_line は line より小さい整数が必要です".format(cid))
                continue
            if start_line not in info[side]:
                errors.append("{}: start_line {} がdiff上にありません".format(cid, start_line))
                continue
            if not in_same_hunk(diff_files, path, side, start_line, line):
                errors.append(
                    "{}: start_line {} と line {} が同一ハンク内にありません"
                    "（間にdiff外の行が挟まっています）".format(cid, start_line, line))
                continue

        for e in lint_body(body, state["pr"]["repo"], config):
            errors.append("{}: {}".format(cid, e))

        for premise in c.get("unread_premises") or []:
            if premise not in unreadable_ids:
                errors.append(
                    "{}: unread_premises の {} は「読めなかった」必読対象ではありません"
                    .format(cid, premise))

        evidence = c.get("evidence") or {}
        for q in evidence.get("quotes") or []:
            err = validate_quote(state["worktree"], q)
            if err:
                errors.append("{}: {}".format(cid, err))
        for ci in evidence.get("ci") or []:
            err = validate_ci_claim(run_dir, ci)
            if err:
                errors.append("{}: {}".format(cid, err))
        if CI_MENTION_RE.search(body) and not (evidence.get("ci")):
            errors.append(
                "{}: 本文がCIに言及していますが evidence.ci がありません。"
                "CIに言及する場合は evidence.ci で実結果と照合するか、"
                "CIへの言及を本文から外してください".format(cid))
        if re.search(r"```", body) and not (evidence.get("quotes")):
            warnings.append(
                "{}: 本文にコードブロックがありますが evidence.quotes がありません。"
                "実在するコードの引用であれば evidence.quotes で照合を宣言してください"
                .format(cid))

        lang = body_language(body)
        if state["language"] != "unknown" and lang != state["language"]:
            warnings.append(
                "{}: コメントの言語（{}）が対象リポジトリの主要言語（{}）と異なります"
                .format(cid, lang, state["language"]))
        if lang == "ja" and normalize_text(body).endswith("。"):
            warnings.append(
                "{}: 本文の末尾が「。」で終わっています。最後の文は"
                "「！」・絵文字・「）」などで締めてください（行動規範の文末の規則）"
                .format(cid))

        for w in waivers:
            if w.get("path") == path and w.get("kind") == kind and \
                    isinstance(w.get("line"), int) and abs(w["line"] - line) <= 3:
                warnings.append(
                    "{}: 前回見送った指摘と類似しています（{}:{} {}）。"
                    "再提案の理由がなければ取り下げてください".format(
                        cid, w.get("path"), w.get("line"), (w.get("body") or "")[:60]))
        for ex in existing:
            if ex.get("path") == path and isinstance(ex.get("line"), int) and \
                    abs(ex["line"] - line) <= 3:
                warnings.append(
                    "{}: 既存コメント（{} による {}:{}）と位置が近接しています。"
                    "重複していないか確認してください: {}".format(
                        cid, (ex.get("user") or {}).get("login"),
                        ex.get("path"), ex.get("line"), (ex.get("body") or "")[:60]))

        normalized.append({
            "id": cid,
            "kind": kind,
            "path": path,
            "side": side,
            "line": line,
            "start_line": start_line,
            "start_side": start_side,
            "body": normalize_text(body),
            "bodyFinal": finalize_body(kind, body),
            "reason": reason,
            "unread_premises": c.get("unread_premises") or [],
        })

    # 用語検査（警告のみ）
    term_targets = [(c["id"], c["body"]) for c in normalized]
    if summary:
        term_targets.append(("summary", summary))
    unknown_terms = {}
    for cid, text in term_targets:
        for term in sorted(extract_terms(text)):
            if term in approved_terms:
                continue
            if term_grounded(term, corpus_lower, state["worktree"]):
                continue
            unknown_terms.setdefault(term, []).append(cid)
    if unknown_terms:
        listed = sorted(unknown_terms.items())[:config["maxTermWarnings"]]
        for term, ids in listed:
            warnings.append(
                "出典不明の用語（共有文脈に出現しません）: 「{}」（{}）。"
                "造語・内輪用語でないか確認してください".format(term, ", ".join(ids)))
        if len(unknown_terms) > len(listed):
            warnings.append("出典不明の用語が他に{}語あります".format(
                len(unknown_terms) - len(listed)))

    if errors:
        print(json.dumps({
            "ok": False, "errors": errors, "warnings": warnings,
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

    payload = approved_payload(state, summary, normalized)
    draft_hash = payload_hash(payload)
    save_json(os.path.join(run_dir, "draft_normalized.json"), {
        "summary": normalize_text(summary or ""),
        "comments": normalized,
        "hash": draft_hash,
        "headOid": state["pr"]["headRefOid"],
        "validatedAt": now_iso(),
    })

    # 投稿物ビュー: 公開される文字列そのものをスクリプトが描画する
    preview = ["# 投稿物ビュー（この内容がそのままGitHubに投稿されます）", ""]
    preview.append("- PR: {}".format(state["pr"]["url"]))
    preview.append("- 投稿名義: {}".format(state["login"]))
    preview.append("- head: {}".format(state["pr"]["headRefOid"][:12]))
    preview.append("")
    if summary:
        preview += ["## レビュー全体のまとめ文", "", normalize_text(summary), "", "---", ""]
    for c in normalized:
        loc = "{}:{}".format(c["path"], c["line"])
        if c["start_line"]:
            loc = "{}:{}-{}".format(c["path"], c["start_line"], c["line"])
        preview.append("## {} [{}] {} ({})".format(c["id"], c["kind"], loc, c["side"]))
        preview.append("")
        excerpt = anchor_excerpt(diff_files, c["path"], c["side"], c["line"])
        if excerpt:
            preview += ["対象箇所のdiff抜粋:", "", "~~~", excerpt, "~~~", ""]
        preview += ["投稿される本文（バッジ行込み）:", "", "~~~markdown",
                    c["bodyFinal"], "~~~", ""]
        preview += ["理由（投稿されない・トリアージ用）: {}".format(c["reason"]), ""]
    write_text(os.path.join(run_dir, "render", "preview.md"), "\n".join(preview))

    listing = ["| ID | 種別 | 場所 | 一行目 |", "| --- | --- | --- | --- |"]
    for c in normalized:
        first = c["body"].split("\n")[0][:60]
        listing.append("| {} | {} | {}:{} | {} |".format(
            c["id"], c["kind"], c["path"], c["line"], first.replace("|", "\\|")))
    write_text(os.path.join(run_dir, "render", "list.md"), "\n".join(listing))

    print(json.dumps({
        "ok": True,
        "draftHash": draft_hash,
        "comments": len(normalized),
        "preview": os.path.join(run_dir, "render", "preview.md"),
        "list": os.path.join(run_dir, "render", "list.md"),
        "warnings": warnings,
    }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# lgtm: Approve送信に添えるLGTM画像の取得と選択
# ---------------------------------------------------------------------------

def cmd_lgtm(args):
    load_state(args.run)  # 実行ディレクトリの検証のみ
    req = urllib.request.Request(
        LGTM_API_URL,
        headers={"Accept": "application/json",
                 "User-Agent": "github-pr-review-draft"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        die("LGTM画像一覧の取得（{}）に失敗しました: {}".format(LGTM_API_URL, e))
    if not isinstance(data, list):
        die("LGTM画像一覧の応答が配列ではありません: {}".format(str(data)[:200]))
    candidates = [
        d for d in data
        if isinstance(d, dict)
        and isinstance(d.get("imageUrl"), str)
        and d["imageUrl"].startswith(LGTM_IMAGE_URL_PREFIX)
    ]
    if not candidates:
        die("応答に {} で始まる imageUrl を持つ画像がありません".format(
            LGTM_IMAGE_URL_PREFIX))
    if args.id is not None:
        picked = next((d for d in candidates if d.get("id") == args.id), None)
        if not picked:
            die("id {} は今回取得した一覧にありません（返る一覧は取得のたびに変わります）。"
                "今回の候補: {}".format(
                    args.id, ", ".join(str(d.get("id")) for d in candidates)))
    else:
        picked = random.choice(candidates)
    record = {
        "id": picked.get("id"),
        "imageUrl": picked["imageUrl"],
        "markdown": lgtm_markdown(picked["imageUrl"]),
        "pickedAt": now_iso(),
    }
    save_json(os.path.join(args.run, "lgtm.json"), record)
    out = dict(record)
    out["candidates"] = len(candidates)
    out["note"] = "別の画像にする場合は lgtm を再実行してください（省略時はランダムに選びます）"
    print(json.dumps(out, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# approve: トリアージ結果の確定（承認をディスク上の成果物に固定する）
# ---------------------------------------------------------------------------

def cmd_approve(args):
    state = load_state(args.run)
    run_dir = args.run

    if args.adopt_staged:
        # GitHub上の保留レビュー実体を正として承認し直す（画面上で編集された場合）
        staged = state.get("staged")
        if not staged:
            die("stage済みの保留レビューがありません")
        check_identity(state)
        pending = fetch_pending_review(state)
        if not pending or pending.get("id") != staged["reviewId"]:
            die("記録済みの保留レビューが見つかりません（削除された可能性があります）")
        gh_comments = gh_api_paginated(
            "repos/{}/pulls/{}/reviews/{}/comments".format(
                state["pr"]["repo"], state["pr"]["number"], staged["reviewId"]),
            "保留レビューのコメント取得")
        review_obj = gh_api(
            "repos/{}/pulls/{}/reviews/{}".format(
                state["pr"]["repo"], state["pr"]["number"], staged["reviewId"]),
            "保留レビューの取得")
        # reviews/{review_id}/comments の応答は line / start_line がnullのため、
        # ここで作る承認記録は行アンカーを持たない。submitの照合は行番号なしで
        # 成立し（pending_match_sets）、stageへの再利用はcmd_stageのガードが拒否する
        comments = []
        for i, c in enumerate(
                sorted(gh_comments, key=lambda x: x.get("id") or 0), start=1):
            comments.append({
                "id": "C{}".format(i),
                "kind": kind_from_body(c.get("body") or ""),
                "path": c.get("path"),
                "side": c.get("side") or "RIGHT",
                "line": c.get("line"),
                "start_line": c.get("start_line"),
                "start_side": c.get("start_side"),
                "bodyFinal": normalize_text(c.get("body") or ""),
            })
        # 本文はGitHub上の実体をそのまま正とする（LGTM画像はstage時に含まれていれば
        # そのまま残る。ここでは追加しない）
        payload = approved_payload(
            state, review_obj.get("body") or "", comments, args.event.upper())
        h = payload_hash(payload)
        save_json(os.path.join(run_dir, "approved.json"), {
            "payload": payload, "hash": h, "at": now_iso(), "source": "staged"})
        # stage記録側のハッシュも更新しないと、submitの照合で行き止まりになる
        state["staged"]["approvedHash"] = h
        save_state(run_dir, state)
        print(json.dumps({"approvedHash": h, "login": state["login"],
                          "event": payload["event"],
                          "comments": len(comments), "source": "staged"},
                         ensure_ascii=False))
        return

    draft = load_json(os.path.join(run_dir, "draft_normalized.json"), None)
    if not draft:
        die("検証済みのコメント案がありません。先に draft を実行してください。")
    if draft.get("headOid") != state["pr"]["headRefOid"]:
        die("コメント案の検証時からPRのheadが変わっています。"
            "draft からやり直してください。")
    by_id = {c["id"]: c for c in draft["comments"]}
    post_ids = [s.strip() for s in (args.post or "").split(",") if s.strip()]
    skip_ids = [s.strip() for s in (args.skip or "").split(",") if s.strip()]
    if len(post_ids) != len(set(post_ids)) or len(skip_ids) != len(set(skip_ids)):
        die("--post / --skip に同じIDが重複して指定されています")
    unknown = [i for i in post_ids + skip_ids if i not in by_id]
    if unknown:
        die("コメント案に存在しないIDです: {}".format(", ".join(unknown)))
    unassigned = sorted(set(by_id) - set(post_ids) - set(skip_ids))
    if unassigned:
        die("全コメント案の扱いを明示してください。未指定: {}"
            .format(", ".join(unassigned)))
    dup = sorted(set(post_ids) & set(skip_ids))
    if dup:
        die("--post と --skip の両方に指定されています: {}".format(", ".join(dup)))

    approved = [by_id[i] for i in sorted(post_ids, key=lambda x: int(x[1:]))]
    event = args.event.upper()
    summary = draft.get("summary") or ""
    approve_warnings = []
    if event == "APPROVE":
        # LGTM画像はスクリプトだけが本文に追加する。選択済みの画像を承認内容に
        # 焼き込むことで、承認した文字列と投稿される文字列の一致を保つ
        lgtm = load_json(os.path.join(run_dir, "lgtm.json"), None)
        image_url = (lgtm or {}).get("imageUrl") or ""
        if not image_url.startswith(LGTM_IMAGE_URL_PREFIX):
            die("Approveの承認にはLGTM画像の選択が必要です。"
                "先に lgtm --run \"{}\" を実行してください。".format(run_dir))
        md = lgtm_markdown(image_url)
        summary = "{}\n\n{}".format(normalize_text(summary), md) \
            if summary.strip() else md
        if any(c["kind"] == "must" for c in approved):
            approve_warnings.append(
                "must の指摘を投稿対象に含めたまま Approve しようとしています。"
                "意図した組み合わせか確認してください")
    payload = approved_payload(state, summary, approved, event)
    h = payload_hash(payload)
    save_json(os.path.join(run_dir, "approved.json"), {
        "payload": payload, "hash": h, "at": now_iso(), "source": "draft"})

    # 見送った指摘の記録（次回の再提案を抑止する）
    waivers = load_json(os.path.join(run_dir, "waivers.json"), [])
    for i in skip_ids:
        c = by_id[i]
        waivers.append({
            "path": c["path"], "line": c["line"], "kind": c["kind"],
            "body": c["body"][:200], "waivedAt": now_iso(),
        })
    save_json(os.path.join(run_dir, "waivers.json"), waivers)

    # 承認された文面中の語は既知の用語として記録する
    approved_terms = set(load_json(os.path.join(run_dir, "approved_terms.json"), []))
    for c in approved:
        approved_terms |= extract_terms(c["body"])
    if draft.get("summary"):
        approved_terms |= extract_terms(draft["summary"])
    save_json(os.path.join(run_dir, "approved_terms.json"), sorted(approved_terms))

    print(json.dumps({
        "approvedHash": h,
        "login": state["login"],
        "event": event,
        "post": len(approved),
        "skipped": len(skip_ids),
        "summaryIncluded": bool(payload["summary"]),
        # 公開されるまとめ文そのもの（Approve時はLGTM画像の行が末尾に付く）
        "summaryFinal": payload["summary"],
        "warnings": approve_warnings,
    }, ensure_ascii=False, indent=2))


def kind_from_body(body):
    m = re.search(r"img\.shields\.io/badge/review-(\w+)-", body)
    return m.group(1) if m and m.group(1) in KIND_LIST else "imo"


# ---------------------------------------------------------------------------
# stage / submit / abandon: 保留レビューの操作
# ---------------------------------------------------------------------------

def load_approved(run_dir, arg_hash):
    approved = load_json(os.path.join(run_dir, "approved.json"), None)
    if not approved:
        die("承認記録がありません。先に approve を実行してください。")
    recomputed = payload_hash(approved["payload"])
    if recomputed != approved["hash"]:
        die("承認記録が壊れています（ハッシュ不一致）。approve からやり直してください。")
    if arg_hash != approved["hash"]:
        die("指定された承認ハッシュが記録と一致しません。approve の出力した"
            "approvedHash をそのまま渡してください。")
    return approved


def cmd_stage(args):
    state = load_state(args.run)
    run_dir = args.run
    approved = load_approved(run_dir, args.approved_hash)
    payload = approved["payload"]
    if not payload["comments"] and not payload["summary"]:
        die("承認済みの投稿内容が空です（コメント0件・まとめ文なし）")
    if any(not isinstance(c.get("line"), int) for c in payload["comments"]):
        die("承認記録に行アンカー（line）の無いコメントが含まれています"
            "（approve --adopt-staged で作られた記録は行番号を持たないため、"
            "stage には使えません）。draft と approve からやり直してください。")

    check_identity(state)

    live = fetch_pr_live(state["pr"]["repo"], state["pr"]["number"])
    if live.get("state") != "open":
        die("PRがopenではありません（state: {}）。stageを中止します。".format(live.get("state")))
    if live["head"]["sha"] != payload["headOid"]:
        die("PRのheadが承認時（{}）から変わっています（{}）。"
            "startからやり直して再レビューしてください。".format(
                payload["headOid"][:12], live["head"]["sha"][:12]))

    # 既存の保留レビューがあれば何もせず中止する（手書きの下書きを壊さない）
    pending = fetch_pending_review(state)
    if pending:
        die("このPRには既にあなたの保留中レビュー（id: {}）が存在します。"
            "GitHub上で送信または破棄してから stage してください。"
            "（このスクリプトは既存の保留レビューを削除しません）".format(pending.get("id")))

    # 送信済み内容の二重投稿を拒否する
    for s in state.get("submitted") or []:
        if s.get("hash") == approved["hash"]:
            die("この承認内容（hash: {}...）は送信済みです（review id: {}）。"
                .format(approved["hash"][:12], s.get("reviewId")))

    api_comments = []
    for c in payload["comments"]:
        item = {"path": c["path"], "side": c["side"],
                "line": c["line"], "body": c["body"]}
        if c.get("startLine"):
            item["start_line"] = c["startLine"]
            item["start_side"] = c["startSide"]
        api_comments.append(item)
    # comments は任意パラメータのため、0件（まとめ文のみのApprove等）では送らない
    req = {"commit_id": payload["headOid"]}
    if api_comments:
        req["comments"] = api_comments
    if payload["summary"]:
        req["body"] = payload["summary"]
    # event は指定しない（省略するとPENDINGになる。docs.github.com/en/rest/pulls/reviews）
    input_path = os.path.join(run_dir, "stage_request.json")
    save_json(input_path, req)
    review = gh_api(
        "repos/{}/pulls/{}/reviews".format(state["pr"]["repo"], state["pr"]["number"]),
        "保留レビューの作成", method="POST", input_path=input_path)
    if (review or {}).get("state") != "PENDING":
        die("作成されたレビューがPENDINGではありません（state: {}）。"
            "GitHub上の状態を確認してください。".format((review or {}).get("state")))

    gh_comments = gh_api_paginated(
        "repos/{}/pulls/{}/reviews/{}/comments".format(
            state["pr"]["repo"], state["pr"]["number"], review["id"]),
        "作成された保留コメントの取得")
    if len(gh_comments) != len(payload["comments"]):
        die("作成された保留コメント数（{}）が承認数（{}）と一致しません。"
            "abandon で削除して原因を確認してください。".format(
                len(gh_comments), len(payload["comments"])))

    state["staged"] = {
        "reviewId": review["id"],
        "nodeId": review.get("node_id"),
        "approvedHash": approved["hash"],
        "headOid": payload["headOid"],
        "commentIds": sorted(c.get("id") for c in gh_comments),
        "counts": collect_counts(state),
        "stagedAt": now_iso(),
    }
    save_state(run_dir, state)
    print(json.dumps({
        "staged": True,
        "reviewId": review["id"],
        "comments": len(gh_comments),
        "viewUrl": "{}/files".format(state["pr"]["url"]),
        "note": "保留中レビューは送信まで本人にしか見えません。"
                "GitHub上で内容を確認できます。送信は submit で行います。",
    }, ensure_ascii=False, indent=2))


def pending_match_sets(payload_comments, gh_comments):
    """承認済みコメントとGitHub上の保留コメントを、照合用の正規形に変換する。

    reviews/{review_id}/comments の応答は旧形式で、保留中・公開後を問わず
    line / start_line をnullで返す（実測）。行番号が返っていない場合は
    行番号を照合対象から外し、本文・path・side（と件数）で照合する。
    行アンカー自体はstage時のレビュー作成APIが検証済みで、GitHub画面からの
    編集で変わるのは本文だけのため、これで承認内容との一致確認には足りる。
    """
    lines_known = all(c.get("line") is not None for c in gh_comments)

    def key(path, side, line, start_line, body):
        k = {"path": path, "side": side, "body": body}
        if lines_known:
            k["line"] = line
            k["startLine"] = start_line
        return k

    def norm_set(items):
        return sorted(
            json.dumps(x, ensure_ascii=False, sort_keys=True) for x in items)

    approved_set = norm_set([
        key(c["path"], c["side"], c["line"], c.get("startLine"), c["body"])
        for c in payload_comments
    ])
    actual_set = norm_set([
        key(c.get("path"), c.get("side") or "RIGHT",
            c.get("line"), c.get("start_line"),
            normalize_text(c.get("body") or ""))
        for c in gh_comments
    ])
    return approved_set, actual_set


def cmd_submit(args):
    state = load_state(args.run)
    run_dir = args.run
    approved = load_approved(run_dir, args.approved_hash)
    payload = approved["payload"]
    staged = state.get("staged")
    if not staged:
        die("stage済みの保留レビューがありません。先に stage を実行してください。")
    if staged["approvedHash"] != approved["hash"]:
        die("stage時の承認ハッシュと現在の承認記録が一致しません。"
            "stage からやり直してください。")

    check_identity(state)

    # 鮮度ゲート
    live = fetch_pr_live(state["pr"]["repo"], state["pr"]["number"])
    if live.get("state") != "open":
        die("PRがopenではありません（state: {}）。送信を中止します。".format(live.get("state")))
    if live["head"]["sha"] != staged["headOid"]:
        die("stage後にPRのheadが変わっています（{} → {}）。送信を中止します。"
            "abandon で保留レビューを削除し、start からやり直してください。".format(
                staged["headOid"][:12], live["head"]["sha"][:12]))
    counts = collect_counts(state)
    if counts != staged["counts"]:
        die("stage後にPR上のコメント・レビューが増減しています（{} → {}）。"
            "新しい議論を確認し、内容に変わりがなければ stage からやり直してください。"
            .format(staged["counts"], counts))

    # 保留レビュー実体を再取得し、承認済み内容と完全一致することを検証する
    pending = fetch_pending_review(state)
    if not pending or pending.get("id") != staged["reviewId"]:
        die("記録済みの保留レビュー（id: {}）が見つかりません。"
            "GitHub上で削除・送信された可能性があります。status で確認してください。"
            .format(staged["reviewId"]))
    gh_comments = gh_api_paginated(
        "repos/{}/pulls/{}/reviews/{}/comments".format(
            state["pr"]["repo"], state["pr"]["number"], staged["reviewId"]),
        "保留レビューのコメント取得")

    approved_set, actual_set = pending_match_sets(
        payload["comments"], gh_comments)
    actual_summary = normalize_text((pending.get("body") or ""))
    if approved_set != actual_set or actual_summary != payload["summary"]:
        diff = "\n".join(difflib.unified_diff(
            approved_set + ["summary: " + payload["summary"]],
            actual_set + ["summary: " + actual_summary],
            fromfile="承認済み", tofile="GitHub上の保留レビュー", lineterm=""))
        print(diff, file=sys.stderr)
        die("GitHub上の保留レビューが承認内容と一致しません（画面上で編集された"
            "可能性があります）。上の差分を確認し、その内容でよければ "
            "approve --adopt-staged で再承認してから submit してください。")

    # 送信。eventは承認記録に焼き込まれた値のみを送り、本文は送らない
    # （本文はstage時に保留レビューへ設定済み）
    event = payload.get("event") or "COMMENT"
    expected_state = {"COMMENT": "COMMENTED", "APPROVE": "APPROVED"}.get(event)
    if not expected_state:
        die("承認記録のevent（{}）はこのスクリプトでは送信できません".format(event))
    review = gh_api(
        "repos/{}/pulls/{}/reviews/{}/events".format(
            state["pr"]["repo"], state["pr"]["number"], staged["reviewId"]),
        "レビューの送信", method="POST", fields={"event": event})
    if (review or {}).get("state") != expected_state:
        die("送信後のレビュー状態が想定外です（state: {}、想定: {}）。"
            "GitHub上の状態を確認してください。".format(
                (review or {}).get("state"), expected_state))

    state.setdefault("submitted", []).append({
        "reviewId": staged["reviewId"],
        "hash": approved["hash"],
        "headOid": staged["headOid"],
        "at": now_iso(),
    })
    state["staged"] = None
    save_state(run_dir, state)
    print(json.dumps({
        "submitted": True,
        "event": event,
        "reviewId": (review or {}).get("id"),
        "url": (review or {}).get("html_url") or state["pr"]["url"],
        "note": "レビューを公開しました。worktreeが不要になったら次のコマンドで"
                "削除できます: git -C \"{}\" worktree remove \"{}\"".format(
                    state["cloneDir"], state["worktree"]),
    }, ensure_ascii=False, indent=2))


def cmd_abandon(args):
    state = load_state(args.run)
    staged = state.get("staged")
    if not staged:
        die("stage済みの保留レビューの記録がありません")
    check_identity(state)
    pending = fetch_pending_review(state)
    found = bool(pending and pending.get("id") == staged["reviewId"])
    if not found and not args.clear_state:
        die("記録済みの保留レビュー（id: {}）が見つかりません。"
            "既にGitHub上で削除・送信済みの場合は、ローカル記録だけを整理するため "
            "--clear-state を付けて再実行してください。".format(staged["reviewId"]))
    if found:
        # 削除は自分が作成した記録済みのreview idのみを対象とする
        gh_api(
            "repos/{}/pulls/{}/reviews/{}".format(
                state["pr"]["repo"], state["pr"]["number"], staged["reviewId"]),
            "保留レビューの削除", method="DELETE")
    state["staged"] = None
    save_state(args.run, state)
    print(json.dumps({"abandoned": True, "deletedOnGithub": found},
                     ensure_ascii=False))


def cmd_status(args):
    state = load_state(args.run)
    reading = load_json(os.path.join(args.run, "reading.json"), {})
    approved = load_json(os.path.join(args.run, "approved.json"), None)
    print(json.dumps({
        "pr": state["pr"]["url"],
        "login": state["login"],
        "headOid": state["pr"]["headRefOid"],
        "worktree": state["worktree"],
        "cloneDir": state["cloneDir"],
        "readingGate": reading_gate(state, reading),
        "approvedHash": (approved or {}).get("hash"),
        "approvedEvent": ((approved or {}).get("payload") or {}).get("event"),
        "lgtm": load_json(os.path.join(args.run, "lgtm.json"), None),
        "staged": state.get("staged"),
        "submitted": state.get("submitted"),
        "waivers": len(load_json(os.path.join(args.run, "waivers.json"), [])),
    }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="PRを解析して実行ディレクトリとworktreeを作る")
    p.add_argument("--pr", required=True, help="レビュー対象PRのURL")
    p.add_argument("--repo-dir", help="対象リポジトリのローカルcloneのパス（省略時は自動解決）")
    p.add_argument("--config", help="設定ファイル（JSON）のパス")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("reading", help="必読対象の状態を記録する")
    p.add_argument("--run", required=True)
    p.add_argument("--item", required=True, help="必読対象のID（R1等）")
    p.add_argument("--read", action="store_true", help="読了として記録する")
    p.add_argument("--summary", help="読んだ内容の一行要約（--read時に必須）")
    p.add_argument("--unreadable", action="store_true", help="読めなかったとして記録する")
    p.add_argument("--reason", help="読めなかった理由（--unreadable時に必須）")
    p.set_defaults(func=cmd_reading)

    p = sub.add_parser("check", help="必読ゲートの充足状況を表示する")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("draft", help="コメント案JSONを検証し投稿物ビューを生成する")
    p.add_argument("--run", required=True)
    p.add_argument("--file", required=True, help="コメント案JSONのパス")
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("lgtm", help="Approve送信に添えるLGTM画像を取得して選ぶ")
    p.add_argument("--run", required=True)
    p.add_argument("--id", type=int, help="画像IDを指定して選ぶ（省略時はランダム）")
    p.set_defaults(func=cmd_lgtm)

    p = sub.add_parser("approve", help="トリアージ結果を確定し承認ハッシュを発行する")
    p.add_argument("--run", required=True)
    p.add_argument("--post", help="投稿するコメントID（カンマ区切り）")
    p.add_argument("--skip", help="見送るコメントID（カンマ区切り）")
    p.add_argument("--event", choices=["comment", "approve"], default="comment",
                   help="送信種別。approve はPRの承認（まとめ文末尾にLGTM画像が付く）")
    p.add_argument("--adopt-staged", action="store_true",
                   help="GitHub上の保留レビュー実体を正として再承認する")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("stage", help="承認済みコメント案を保留レビューとして作成する")
    p.add_argument("--run", required=True)
    p.add_argument("--approved-hash", required=True)
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("submit", help="保留レビューを送信して公開する（唯一の公開行為）")
    p.add_argument("--run", required=True)
    p.add_argument("--approved-hash", required=True)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("abandon", help="自分が作成した保留レビューを削除する")
    p.add_argument("--run", required=True)
    p.add_argument("--clear-state", action="store_true",
                   help="GitHub上に保留レビューが無い場合にローカル記録だけ消す")
    p.set_defaults(func=cmd_abandon)

    p = sub.add_parser("status", help="現在の状態を表示する")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
