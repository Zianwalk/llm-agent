import subprocess
import os
import json
import shutil
from datetime import datetime

# ── 取得 branch 名稱 ──────────────────────────────────────────────
try:
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    ).decode().strip()
except Exception:
    branch = "main"

date = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
OUTPUT_DIR = f"callgraphs/{date}_{branch}"
os.makedirs(OUTPUT_DIR, exist_ok=True)

GRAPH_DOT  = os.path.join(OUTPUT_DIR, "callgraph.dot")
GRAPH_PNG  = os.path.join(OUTPUT_DIR, "callgraph.png")
GRAPH_HTML = os.path.join(OUTPUT_DIR, "callgraph.html")
LATEST_DIR = "callgraphs/latest"
os.makedirs(LATEST_DIR, exist_ok=True)

EXCLUDE_DIRS = {".git", "callgraphs", "__pycache__", ".venv", "venv", "scripts"}

# ── 找所有 .py 檔（排除 scripts/ 自身）───────────────────────────
py_files = []
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
    for f in files:
        if f.endswith(".py"):
            py_files.append(os.path.join(root, f))

if not py_files:
    print("No Python files found.")
    exit(0)

print(f"Analysing {len(py_files)} file(s): {py_files}")

# ── pyan3：--uses 只畫呼叫關係，--no-defines 去掉重複定義節點 ────
cmd = ["pyan3", *py_files, "--uses", "--no-defines", "--dot"]
with open(GRAPH_DOT, "w") as f:
    result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)

if result.returncode != 0:
    print("pyan3 error:", result.stderr.decode())
    exit(1)

# ── 讀 dot，注入深色樣式 ──────────────────────────────────────────
with open(GRAPH_DOT) as f:
    dot_raw = f.read()

style_header = (
    '  graph [rankdir=LR, bgcolor="#0d1117", pad=0.6, splines=curved, fontname="Courier New"];\n'
    '  node  [shape=box, style="filled,rounded", fillcolor="#161b22", fontcolor="#c9d1d9",\n'
    '         fontname="Courier New", fontsize=11, color="#30363d"];\n'
    '  edge  [color="#58a6ff", arrowsize=0.7, penwidth=1.2];\n'
)

dot_styled = dot_raw.replace("{", "{\n" + style_header, 1)

with open(GRAPH_DOT, "w") as f:
    f.write(dot_styled)

# ── PNG ───────────────────────────────────────────────────────────
r = subprocess.run(["dot", "-Tpng", GRAPH_DOT, "-o", GRAPH_PNG], capture_output=True)
if r.returncode != 0:
    print("dot error:", r.stderr.decode())
else:
    print(f"PNG  → {GRAPH_PNG}")

# ── 解析 dot 取節點/邊，產生互動 HTML ────────────────────────────
import re

node_ids: set[str] = set()
edge_list: list[tuple[str, str]] = []

for line in dot_raw.splitlines():
    m = re.match(r'\s*"?([^">\s]+)"?\s*->\s*"?([^";\s]+)"?', line)
    if m:
        src, dst = m.group(1).strip('"'), m.group(2).strip('"')
        edge_list.append((src, dst))
        node_ids.update([src, dst])
    elif re.match(r'\s*"?(\w+)"?\s*\[', line):
        n = re.match(r'\s*"?(\w+)"?', line).group(1)
        node_ids.add(n)

vis_nodes = json.dumps([{"id": n, "label": n} for n in sorted(node_ids)])
vis_edges = json.dumps([{"from": s, "to": d, "arrows": "to"} for s, d in edge_list])

html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>Call Graph – {date}_{branch}</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{background:#0d1117;font-family:'Courier New',monospace;color:#c9d1d9}}
  #header{{padding:14px 22px;border-bottom:1px solid #30363d;display:flex;align-items:center;gap:10px}}
  #header h1{{font-size:14px;color:#58a6ff;letter-spacing:.06em}}
  #header span{{font-size:12px;color:#8b949e}}
  #graph{{width:100%;height:calc(100vh - 49px)}}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/dist/vis-network.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/dist/dist/vis-network.min.css">
</head>
<body>
<div id="header">
  <h1>&#128336; Call Graph</h1>
  <span>{branch} &nbsp;·&nbsp; {date}</span>
</div>
<div id="graph"></div>
<script>
const nodes = new vis.DataSet({vis_nodes});
const edges = new vis.DataSet({vis_edges});
const options = {{
  physics:{{solver:"forceAtlas2Based",forceAtlas2Based:{{gravitationalConstant:-80,springLength:130}},stabilization:{{iterations:250}}}},
  nodes:{{
    shape:"box",borderRadius:6,
    color:{{background:"#161b22",border:"#30363d",highlight:{{background:"#1f6feb",border:"#58a6ff"}}}},
    font:{{color:"#c9d1d9",face:"Courier New",size:13}},
    shadow:{{enabled:true,color:"rgba(0,0,0,.5)",x:2,y:2,size:8}}
  }},
  edges:{{
    color:{{color:"#58a6ff",highlight:"#79c0ff"}},
    smooth:{{type:"curvedCW",roundness:0.15}},
    arrows:{{to:{{enabled:true,scaleFactor:0.7}}}}
  }},
  interaction:{{hover:true,zoomView:true}}
}};
new vis.Network(document.getElementById("graph"),{{nodes,edges}},options);
</script>
</body>
</html>
"""

with open(GRAPH_HTML, "w", encoding="utf-8") as f:
    f.write(html)
print(f"HTML → {GRAPH_HTML}")

# ── 更新 latest/ ──────────────────────────────────────────────────
for fname in ("callgraph.png", "callgraph.html"):
    src = os.path.join(OUTPUT_DIR, fname)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(LATEST_DIR, fname))

print("Done.")
