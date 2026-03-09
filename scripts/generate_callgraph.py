import subprocess
import os
import re
import ast
import json
import shutil
from collections import defaultdict
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

LATEST_DIR = "callgraphs/latest"
os.makedirs(LATEST_DIR, exist_ok=True)

EXCLUDE_DIRS = {".git", "callgraphs", "__pycache__", ".venv", "venv", "scripts"}


# ── AST 分析：抓直接呼叫 + 全域變數依賴 ──────────────────────────
def analyze(filepath: str):
    """
    回傳:
      funcs        : set of function names defined in this file
      call_edges   : set of (caller, callee) — 直接呼叫
      global_edges : set of (func, producer_func) — 全域變數依賴
    """
    with open(filepath, encoding="utf-8") as f:
        src = f.read()
    try:
        tree = ast.parse(src, filename=filepath)
    except SyntaxError as e:
        print(f"  SyntaxError in {filepath}: {e}")
        return set(), set(), set()

    # 1. 找出所有頂層定義的函數
    funcs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.add(node.name)

    # 2. 找出全域賦值：var = some_func(...) 或 var, var2 = some_func(...)
    #    → 記錄 global_var → produced_by_func
    global_producers: dict[str, str] = {}  # var_name → func_name
    for node in ast.iter_child_nodes(tree):  # 只看頂層
        if isinstance(node, ast.Assign):
            # 找右側呼叫的函數名
            producer = None
            if isinstance(node.value, ast.Call):
                if isinstance(node.value.func, ast.Name):
                    producer = node.value.func.id
                elif isinstance(node.value.func, ast.Attribute):
                    producer = node.value.func.attr
            if producer and producer in funcs:
                # 左側可能是 tuple: a, b = func()
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        global_producers[target.id] = producer
                    elif isinstance(target, ast.Tuple):
                        for elt in target.elts:
                            if isinstance(elt, ast.Name):
                                global_producers[elt.id] = producer

    # 3. 找頂層 dict 賦值：TOOLS = {"KEY": func, ...}
    dict_to_funcs: dict[str, set] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    contained = {v.id for v in node.value.values
                                 if isinstance(v, ast.Name) and v.id in funcs}
                    if contained:
                        dict_to_funcs[target.id] = contained

    # 4. 分析每個函數內部：直接呼叫 + 使用的全域變數 + dict 動態呼叫
    call_edges: set[tuple[str, str]] = set()
    global_edges: set[tuple[str, str]] = set()

    class FuncVisitor(ast.NodeVisitor):
        def __init__(self):
            self.current: str | None = None
            self.local_vars: set[str] = set()

        def visit_FunctionDef(self, node):
            prev, prev_locals = self.current, self.local_vars
            self.current = node.name
            self.local_vars = set()
            # 收集函數參數為 local
            for arg in node.args.args:
                self.local_vars.add(arg.arg)
            self.generic_visit(node)
            self.current, self.local_vars = prev, prev_locals

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node):
            # 收集函數內部的 local 變數名
            if self.current:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.local_vars.add(target.id)
            self.generic_visit(node)

        def visit_Call(self, node):
            if self.current:
                callee = None
                if isinstance(node.func, ast.Name):
                    callee = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    callee = node.func.attr
                if callee and callee in funcs and callee != self.current:
                    call_edges.add((self.current, callee))
            self.generic_visit(node)

        def visit_Name(self, node):
            # 使用了某個名稱（非賦值）
            if self.current and isinstance(node.ctx, ast.Load):
                name = node.id
                # 是全域變數（不是 local，不是函數名本身）且有生產者
                if (name not in self.local_vars
                        and name not in funcs
                        and name in global_producers):
                    producer = global_producers[name]
                    if producer != self.current:
                        global_edges.add((self.current, producer))
                # dict 動態呼叫：用了 TOOLS 就依賴 TOOLS 內的所有函數
                if name in dict_to_funcs:
                    for contained in dict_to_funcs[name]:
                        if contained != self.current:
                            call_edges.add((self.current, contained))
            self.generic_visit(node)

    FuncVisitor().visit(tree)
    return funcs, call_edges, global_edges


def make_graph(filepath: str, out_dir: str):
    name = os.path.splitext(os.path.basename(filepath))[0]
    print(f"\n[{name}]")

    funcs, call_edges, global_edges = analyze(filepath)
    all_edges = call_edges | global_edges

    if not funcs:
        print("  No functions found, skipping.")
        return

    # ── 拓撲排序決定層次 ─────────────────────────────────────────
    # 建立 caller→callees 的圖，計算每個節點的 in-degree
    # 層次 = 最長路徑深度（BFS from roots）
    children: dict[str, set] = defaultdict(set)
    parents:  dict[str, set] = defaultdict(set)
    for src, dst in all_edges:
        if src in funcs and dst in funcs:
            children[src].add(dst)
            parents[dst].add(src)

    # BFS 計算層次
    level: dict[str, int] = {}
    queue = [f for f in funcs if not parents[f]]  # roots = 無父節點
    if not queue:
        queue = list(funcs)  # 有環的話全部放 level 0
    for f in queue:
        level[f] = 0

    changed = True
    while changed:
        changed = False
        for src, dst in all_edges:
            if src in level:
                new_level = level[src] + 1
                if dst not in level or level[dst] < new_level:
                    level[dst] = new_level
                    changed = True

    for f in funcs:
        if f not in level:
            level[f] = 0

    # ── 只保留有邊的節點（孤立節點也顯示） ───────────────────────
    connected = set()
    for s, d in all_edges:
        if s in funcs and d in funcs:
            connected.add(s)
            connected.add(d)
    show_nodes = funcs  # 全部顯示

    # ── 產生 dot ─────────────────────────────────────────────────
    out_dot  = os.path.join(out_dir, f"{name}.dot")
    out_png  = os.path.join(out_dir, f"{name}.png")
    out_html = os.path.join(out_dir, f"{name}.html")

    dot_lines = [
        f'digraph "{name}" {{',
        '  graph [rankdir=TB, bgcolor="#0d1117", pad=0.8, ranksep=1.0, nodesep=0.7, fontname="Courier New"];',
        '  node  [shape=box, style="filled,rounded", fillcolor="#161b22", fontcolor="#c9d1d9",',
        '         fontname="Courier New", fontsize=12, color="#30363d", width=1.6];',
        '  edge  [arrowsize=0.7, penwidth=1.3];',
    ]

    # 同層節點用 rank=same 對齊
    by_level: dict[int, list] = defaultdict(list)
    for f in show_nodes:
        by_level[level[f]].append(f)

    for lvl in sorted(by_level):
        nodes_at_level = sorted(by_level[lvl])
        dot_lines.append(f'  {{ rank=same; {"; ".join(nodes_at_level)} }}')

    for f in sorted(show_nodes):
        dot_lines.append(f'  {f} [label="{f}"];')

    # 直接呼叫：藍色實線
    for src, dst in sorted(call_edges):
        if src in funcs and dst in funcs:
            dot_lines.append(f'  {src} -> {dst} [color="#58a6ff"];')

    # 全域變數依賴：橘色虛線
    for src, dst in sorted(global_edges):
        if src in funcs and dst in funcs:
            dot_lines.append(f'  {src} -> {dst} [color="#f0883e", style=dashed, label="uses globals"];')

    dot_lines.append("}")

    with open(out_dot, "w") as f:
        f.write("\n".join(dot_lines))

    r = subprocess.run(["dot", "-Tpng", out_dot, "-o", out_png], capture_output=True)
    if r.returncode != 0:
        print(f"  dot error: {r.stderr.decode().strip()}")
    else:
        print(f"  PNG  → {out_png}")

    # ── HTML ─────────────────────────────────────────────────────
    vis_nodes = json.dumps([
        {"id": f, "label": f, "level": level[f]}
        for f in sorted(show_nodes)
    ])
    vis_edges = json.dumps(
        [{"from": s, "to": d, "color": {"color": "#58a6ff"}, "arrows": "to"} for s, d in sorted(call_edges) if s in funcs and d in funcs] +
        [{"from": s, "to": d, "color": {"color": "#f0883e"}, "arrows": "to", "dashes": True, "label": "globals"} for s, d in sorted(global_edges) if s in funcs and d in funcs]
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>{name} – {date}</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{background:#0d1117;font-family:'Courier New',monospace;color:#c9d1d9}}
  #header{{padding:14px 22px;border-bottom:1px solid #30363d;display:flex;align-items:center;justify-content:space-between}}
  #header h1{{font-size:14px;color:#58a6ff;letter-spacing:.06em}}
  #header span{{font-size:12px;color:#8b949e}}
  #legend{{display:flex;gap:20px;font-size:11px}}
  .dot{{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:5px}}
  #graph{{width:100%;height:calc(100vh - 49px)}}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/dist/vis-network.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/dist/dist/vis-network.min.css">
</head>
<body>
<div id="header">
  <h1>&#128336; {name}</h1>
  <div id="legend">
    <span><span class="dot" style="background:#58a6ff"></span>直接呼叫</span>
    <span><span class="dot" style="background:#f0883e"></span>全域變數依賴</span>
  </div>
  <span>{branch} · {date}</span>
</div>
<div id="graph"></div>
<script>
const nodes = new vis.DataSet({vis_nodes});
const edges = new vis.DataSet({vis_edges});
const options = {{
  layout:{{hierarchical:{{enabled:true,direction:"UD",sortMethod:"directed",levelSeparation:120,nodeSpacing:160,treeSpacing:200}}}},
  physics:{{enabled:false}},
  nodes:{{shape:"box",borderRadius:6,color:{{background:"#161b22",border:"#30363d",highlight:{{background:"#1f6feb",border:"#58a6ff"}}}},font:{{color:"#c9d1d9",face:"Courier New",size:13}},shadow:{{enabled:true,color:"rgba(0,0,0,.5)",x:2,y:2,size:8}}}},
  edges:{{smooth:{{type:"cubicBezier",forceDirection:"vertical"}},arrows:{{to:{{enabled:true,scaleFactor:0.7}}}},font:{{color:"#8b949e",size:10}}}},
  interaction:{{hover:true,zoomView:true}}
}};
new vis.Network(document.getElementById("graph"),{{nodes,edges}},options);
</script>
</body>
</html>"""

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML → {out_html}")

    # 更新 latest/
    for src_path, fname in [(out_png, f"{name}.png"), (out_html, f"{name}.html")]:
        if os.path.exists(src_path):
            shutil.copy2(src_path, os.path.join(LATEST_DIR, fname))


# ── 找所有 .py 檔，各自分析 ──────────────────────────────────────
py_files = []
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
    for f in files:
        if f.endswith(".py"):
            py_files.append(os.path.join(root, f))

if not py_files:
    print("No Python files found.")
    exit(0)

print(f"Found {len(py_files)} file(s)")
for fp in py_files:
    make_graph(fp, OUTPUT_DIR)


# ── 更新 README.md ────────────────────────────────────────────────
repo_name = os.path.basename(os.path.abspath("."))

# 掃描 latest/ 裡的 png，依檔名排序
png_files = sorted([
    f for f in os.listdir(LATEST_DIR)
    if f.endswith(".png")
])

sections = []
for png in png_files:
    name = os.path.splitext(png)[0]
    sections.append(f"## {name}\n![{name}]({LATEST_DIR}/{png})")

readme = f"""# {repo_name}

---

{"\n\n---\n\n".join(sections)}
"""

with open("README.md", "w", encoding="utf-8") as f:
    f.write(readme)

print("README.md updated.")

print("\nDone.")
