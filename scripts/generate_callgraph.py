import subprocess
import os
import re
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

# ── 找所有 .py 檔 ─────────────────────────────────────────────────
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

# ── pyan3 產生原始 dot ────────────────────────────────────────────
raw_dot_path = os.path.join(OUTPUT_DIR, "callgraph_raw.dot")
cmd = ["pyan3", *py_files, "--uses", "--no-defines", "--dot"]
with open(raw_dot_path, "w") as f:
    result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)

if result.returncode != 0:
    print("pyan3 error:", result.stderr.decode())
    exit(1)

with open(raw_dot_path) as f:
    raw = f.read()

# ── 後處理：合併同 label 的重複節點 ──────────────────────────────
# pyan3 產生的節點格式：  node_id [label="funcname", ...]
# 同名函數會有不同 node_id 但相同 label

# 1. 收集所有節點的 id → label 對應
id_to_label: dict[str, str] = {}
for m in re.finditer(r'(\w+)\s*\[.*?label\s*=\s*"([^"]+)"', raw):
    node_id, label = m.group(1), m.group(2)
    id_to_label[node_id] = label

# 2. 對每個 label，選第一個出現的 node_id 作為「代表」
label_to_canonical: dict[str, str] = {}
for node_id, label in id_to_label.items():
    if label not in label_to_canonical:
        label_to_canonical[label] = node_id

# id → canonical id
id_to_canonical: dict[str, str] = {
    nid: label_to_canonical[lbl]
    for nid, lbl in id_to_label.items()
}

# 3. 收集邊（用 canonical id）
edges: set[tuple[str, str]] = set()
for m in re.finditer(r'(\w+)\s*->\s*(\w+)', raw):
    src = id_to_canonical.get(m.group(1), m.group(1))
    dst = id_to_canonical.get(m.group(2), m.group(2))
    if src != dst:
        edges.add((src, dst))

# 4. 只保留 canonical 節點
canonical_ids = set(label_to_canonical.values())

# 5. 重建 dot
dot_lines = [
    "digraph callgraph {",
    '  graph [rankdir=LR, bgcolor="#0d1117", pad=0.6, splines=curved, fontname="Courier New"];',
    '  node  [shape=box, style="filled,rounded", fillcolor="#161b22", fontcolor="#c9d1d9",',
    '         fontname="Courier New", fontsize=12, color="#30363d"];',
    '  edge  [color="#58a6ff", arrowsize=0.7, penwidth=1.3];',
]

for nid in sorted(canonical_ids):
    label = id_to_label.get(nid, nid)
    dot_lines.append(f'  {nid} [label="{label}"];')

for src, dst in sorted(edges):
    dot_lines.append(f'  {src} -> {dst};')

dot_lines.append("}")
dot_content = "\n".join(dot_lines)

with open(GRAPH_DOT, "w") as f:
    f.write(dot_content)

# ── PNG ───────────────────────────────────────────────────────────
r = subprocess.run(["dot", "-Tpng", GRAPH_DOT, "-o", GRAPH_PNG], capture_output=True)
if r.returncode != 0:
    print("dot error:", r.stderr.decode())
else:
    print(f"PNG  → {GRAPH_PNG}")

# ── 互動式 HTML ───────────────────────────────────────────────────
vis_nodes = json.dumps([
    {"id": nid, "label": id_to_label.get(nid, nid)}
    for nid in sorted(canonical_ids)
])
vis_edges = json.dumps([
    {"from": s, "to": d, "arrows": "to"}
    for s, d in sorted(edges)
])

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
    shape:"box", borderRadius:6,
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
