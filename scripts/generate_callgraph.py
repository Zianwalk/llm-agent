import subprocess
import os
import ast
import json
from datetime import datetime
from collections import defaultdict

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

# ── 找所有 .py 檔 ──────────────────────────────────────────────────
py_files = []
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in (".git", "callgraphs", "__pycache__", ".venv", "venv")]
    for f in files:
        if f.endswith(".py"):
            py_files.append(os.path.join(root, f))

# ── 用 AST 自行解析，避免 pyan3 重複節點問題 ───────────────────────
def parse_calls(filepath):
    """回傳 {caller_func: {callee_func, ...}} 的 dict（純函數名，不含模組）"""
    try:
        with open(filepath, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=filepath)
    except SyntaxError:
        return {}

    calls = defaultdict(set)

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.current_func = None

        def visit_FunctionDef(self, node):
            prev = self.current_func
            self.current_func = node.name
            self.generic_visit(node)
            self.current_func = prev

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if self.current_func is None:
                self.generic_visit(node)
                return
            # 取得被呼叫的函數名
            callee = None
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            if callee:
                calls[self.current_func].add(callee)
            self.generic_visit(node)

    Visitor().visit(tree)
    return dict(calls)

# 合併所有檔案的呼叫圖（同名函數視為同一個節點）
merged: dict[str, set] = defaultdict(set)
all_funcs: set[str] = set()

for fp in py_files:
    file_calls = parse_calls(fp)
    for caller, callees in file_calls.items():
        all_funcs.add(caller)
        all_funcs.update(callees)
        merged[caller].update(callees)

# 只保留「被定義過的函數」之間的邊（濾掉 print / len 等內建）
defined_funcs = set(merged.keys())
edges: list[tuple[str, str]] = []
for caller, callees in merged.items():
    for callee in callees:
        if callee in defined_funcs and callee != caller:
            edges.append((caller, callee))

nodes = sorted(defined_funcs)

# ── 產生 DOT ──────────────────────────────────────────────────────
dot_lines = [
    "digraph callgraph {",
    '  graph [rankdir=LR, bgcolor="#0d1117", fontname="Courier New", pad=0.5, splines=curved];',
    '  node  [shape=roundedbox, style="filled,setlinewidth(1.5)", fillcolor="#161b22",',
    '         fontcolor="#c9d1d9", fontname="Courier New", fontsize=11, color="#30363d"];',
    '  edge  [color="#58a6ff", arrowsize=0.7, penwidth=1.2];',
]
for n in nodes:
    dot_lines.append(f'  "{n}";')
for src, dst in edges:
    dot_lines.append(f'  "{src}" -> "{dst}";')
dot_lines.append("}")

with open(GRAPH_DOT, "w") as f:
    f.write("\n".join(dot_lines))

# ── 產生 PNG ──────────────────────────────────────────────────────
result = subprocess.run(["dot", "-Tpng", GRAPH_DOT, "-o", GRAPH_PNG], capture_output=True)
if result.returncode != 0:
    print("dot error:", result.stderr.decode())
else:
    print(f"PNG → {GRAPH_PNG}")

# ── 產生互動式 HTML (vis-network) ────────────────────────────────
vis_nodes = json.dumps([
    {"id": n, "label": n, "title": n} for n in nodes
])
vis_edges = json.dumps([
    {"from": src, "to": dst, "arrows": "to"} for src, dst in edges
])

html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>Call Graph – {date}_{branch}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#0d1117; font-family:'Courier New',monospace; color:#c9d1d9; }}
  #header {{
    padding:16px 24px;
    border-bottom:1px solid #30363d;
    display:flex; align-items:center; gap:12px;
  }}
  #header h1 {{ font-size:15px; color:#58a6ff; letter-spacing:.05em; }}
  #header span {{ font-size:12px; color:#8b949e; }}
  #graph {{ width:100%; height:calc(100vh - 53px); }}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/dist/vis-network.min.js"></script>
<link rel="stylesheet"
      href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/dist/dist/vis-network.min.css">
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
  physics: {{
    solver: "forceAtlas2Based",
    forceAtlas2Based: {{ gravitationalConstant:-60, springLength:120 }},
    stabilization: {{ iterations:200 }}
  }},
  nodes: {{
    shape:"box", borderRadius:6,
    color:{{ background:"#161b22", border:"#30363d", highlight:{{ background:"#1f6feb", border:"#58a6ff" }} }},
    font:{{ color:"#c9d1d9", face:"Courier New", size:13 }},
    shadow:{{ enabled:true, color:"rgba(0,0,0,0.5)", x:2, y:2, size:8 }}
  }},
  edges:{{
    color:{{ color:"#58a6ff", highlight:"#79c0ff" }},
    smooth:{{ type:"curvedCW", roundness:0.15 }},
    arrows:{{ to:{{ enabled:true, scaleFactor:0.7 }} }}
  }},
  interaction:{{ hover:true, tooltipDelay:100, zoomView:true }}
}};

new vis.Network(
  document.getElementById("graph"),
  {{ nodes, edges }},
  options
);
</script>
</body>
</html>
"""

with open(GRAPH_HTML, "w", encoding="utf-8") as f:
    f.write(html)
print(f"HTML → {GRAPH_HTML}")

# ── 更新 latest/ ──────────────────────────────────────────────────
import shutil
for fname in ("callgraph.png", "callgraph.html"):
    src = os.path.join(OUTPUT_DIR, fname)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(LATEST_DIR, fname))

print("Done.")
