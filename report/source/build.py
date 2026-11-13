"""Build the report PDF:  .venv/bin/python report/source/build.py

Fills the tool table from the live tool registry, then prints report.html with headless Chrome.
"""

import base64
import html
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "report" / "23i-0537-MLOps-A3-REPORT.pdf"
sys.path.insert(0, str(ROOT))

from agent.tools import build_registry  # noqa: E402


def tools_table() -> str:
    rows = [
        '<table><tr><th style="width:31%">Tool</th><th>What it does</th><th class="c" style="width:12%">Changes state</th></tr>'
    ]
    last = None
    for tool in build_registry().tools():
        if tool.category != last:
            last = tool.category
            rows.append(f'<tr class="grp"><td colspan="3">{html.escape(tool.category)}</td></tr>')
        first = tool.description.split(". ")[0].rstrip(".")
        if len(first) > 140:
            first = first[:137].rsplit(" ", 1)[0] + "..."
        state = "<b>yes, after confirmation</b>" if tool.mutating else "no"
        end = "" if first.endswith("...") else "."
        rows.append(
            f'<tr><td><code>{tool.name}</code></td><td>{html.escape(first)}{end}</td><td class="c">{state}</td></tr>'
        )
    rows.append("</table>")
    return "\n".join(rows)


src = (HERE / "report.html").read_text().replace("<!--TOOLS-->", tools_table())
if "\u2014" in src:
    sys.exit("report contains an em dash")
# The cover is printed separately so that the body's page numbers start at 1.
marker = "<!-- ========================================================== 1 -->"
head, rest = src.split("<body>", 1)
cover, body = rest.split(marker, 1)
parts = {"cover": head + "<body>" + cover + "</body></html>", "body": head + "<body>" + marker + body}

tmp = tempfile.mkdtemp()
chrome = subprocess.Popen(
    [
        "google-chrome",
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--remote-debugging-port=9334",
        f"--user-data-dir={tmp}",
        "about:blank",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    for _ in range(60):
        try:
            pages = json.load(urllib.request.urlopen("http://127.0.0.1:9334/json"))
            break
        except Exception:
            time.sleep(0.5)
    ws = connect(
        next(p for p in pages if p["type"] == "page")["webSocketDebuggerUrl"], max_size=None, legacy=True
    )
    n = 0

    def cmd(method, **params):
        global n
        n += 1
        ws.send(json.dumps({"id": n, "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == n:
                return msg.get("result", msg)

    files = []
    for name, text in parts.items():
        built = HERE / f"_{name}.html"
        built.write_text(text)
        cmd("Page.navigate", url=built.as_uri())
        time.sleep(4)
        cmd(
            "Runtime.evaluate",
            expression="document.fonts.ready.then(()=>document.fonts.size)",
            awaitPromise=True,
        )
        time.sleep(1)
        pdf = cmd("Page.printToPDF", preferCSSPageSize=True, printBackground=True, displayHeaderFooter=False)
        out = Path(tmp) / f"{name}.pdf"
        out.write_bytes(base64.b64decode(pdf["data"]))
        files.append(str(out))
        built.unlink(missing_ok=True)
    subprocess.run(["pdfunite", *files, str(OUT)], check=True)
    print("wrote", OUT, OUT.stat().st_size // 1024, "KB")
finally:
    chrome.terminate()
