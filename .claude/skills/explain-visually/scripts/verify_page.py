#!/usr/bin/env python3
"""生成した解説HTMLを実際にブラウザで描画し、結果を検証するスクリプト。

ヘッドレスChromeでHTMLを開き、次の2つを行う。

1. 描画後のDOMを取得し、Mermaidの図が全て描画されたかを機械的に確認する
2. ページ全体のスクリーンショットを1枚撮る（エージェントがReadして目視するため）

python3 標準ライブラリのみで動作する。Google Chrome を外部コマンドとして使う。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# ページ高さをDOMから取得できなかった場合に使う高さ。
# Chrome の --screenshot はウィンドウ高さを超える部分を写さないため、実際の高さを使うのが基本
FALLBACK_WINDOW_HEIGHT = 12000


def run_chrome(url: str, profile: Path, extra: list[str], timeout: int) -> str:
    """Chromeを起動し、標準出力を返す。

    --headless=new は --dump-dom / --screenshot の処理を終えてもプロセスが残ることがある。
    そのため打ち切りを前提にし、それまでに得た出力を使う。子プロセスごと確実に止めるため、
    独立したプロセスグループで起動してグループ単位で終了させる。
    """
    cmd = [
        CHROME,
        "--headless=new",
        "--disable-gpu",
        "--disable-crash-reporter",
        "--no-first-run",
        f"--user-data-dir={profile}",
        "--hide-scrollbars",
        *extra,
        url,
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        out, _ = proc.communicate()
    return out or ""


def dump_dom(url: str, profile: Path, width: int, budget_ms: int, timeout: int) -> str:
    # ページ高さは幅に依存するため、スクリーンショットと同じ幅で計測する
    return run_chrome(
        url, profile, [f"--window-size={width},1200", f"--virtual-time-budget={budget_ms}", "--dump-dom"], timeout
    )


def screenshot(url: str, profile: Path, width: int, height: int, out: Path, budget_ms: int, timeout: int) -> None:
    run_chrome(
        url,
        profile,
        [f"--virtual-time-budget={budget_ms}", f"--window-size={width},{height}", f"--screenshot={out}"],
        timeout,
    )


def page_title(dom: str) -> str:
    match = re.search(r"<title>(.*?)</title>", dom, re.S)
    return match.group(1).strip() if match else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", help="検証するHTMLファイルのパス")
    parser.add_argument("--width", type=int, default=1250, help="ビューポート幅（既定: 1250）")
    parser.add_argument("--wait", type=int, default=25000, help="描画を待つ仮想時間のミリ秒（既定: 25000）")
    parser.add_argument("--timeout", type=int, default=40, help="Chrome1回あたりの打ち切り秒数（既定: 40）")
    args = parser.parse_args()

    html = Path(args.html).resolve()
    if not html.is_file():
        print(json.dumps({"ok": False, "error": f"ファイルが見つかりません: {html}"}, ensure_ascii=False))
        return 1
    if not Path(CHROME).exists():
        print(json.dumps({"ok": False, "error": f"Google Chrome が見つかりません: {CHROME}"}, ensure_ascii=False))
        return 1

    url = html.as_uri()
    warnings: list[str] = []

    with tempfile.TemporaryDirectory(prefix="explain-visually-") as tmp:
        profile = Path(tmp) / "profile"

        dom = dump_dom(url, profile, args.width, args.wait, args.timeout)
        # 描画後は pre の中身が svg に置き換わるため、元の記法の数は svg と pre の合計で数える
        rendered = len(re.findall(r'<svg id="fig-\d+"', dom))
        unrendered = len(re.findall(r'<pre class="mermaid">\s*\w', dom))
        sources = rendered + unrendered
        ready = 'data-mermaid-ready="1"' in dom

        if sources and not ready:
            warnings.append(
                "Mermaid の描画完了フラグが立っていない。CDN に到達できていないか、記法にエラーがある。"
                "Bash のサンドボックス内では外部ホストに到達できないため、サンドボックス無しで再実行する"
            )
        if sources and rendered != sources:
            warnings.append(
                f"Mermaid の図が {sources} 個あるのに描画されたのは {rendered} 個。"
                "記法エラーか id の衝突が疑われる"
            )

        match = re.search(r'data-page-height="(\d+)"', dom)
        page_height = int(match.group(1)) if match else 0
        if not page_height:
            page_height = FALLBACK_WINDOW_HEIGHT
            warnings.append(
                "ページ高さを取得できなかった。テンプレートの高さ出力scriptが消えている可能性がある。"
                f"既定の {FALLBACK_WINDOW_HEIGHT}px で撮影したため、末尾が切れていないかスクリーンショットで目視する"
            )

        shot = html.parent / f"{html.stem}-shot.png"
        screenshot(url, profile, args.width, page_height + 40, shot, args.wait, args.timeout)
        if not shot.exists():
            print(json.dumps({"ok": False, "error": "スクリーンショットを生成できなかった"}, ensure_ascii=False))
            return 1

    print(
        json.dumps(
            {
                "ok": not warnings,
                "html": str(html),
                "title": page_title(dom),
                "pageHeight": page_height,
                "mermaidSources": sources,
                "mermaidRendered": rendered,
                "mermaidReady": ready,
                "screenshot": str(shot),
                "warnings": warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
