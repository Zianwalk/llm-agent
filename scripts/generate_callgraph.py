import subprocess
import os
import re
import ast
import csv
import json
import shutil
import builtins
from collections import defaultdict
from datetime import datetime

BUILTINS = set(dir(builtins))

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
    with open(filepath, encoding="utf-8") as f:
        src = f.read()
    try:
        tree = ast.parse(src, filename=filepath)
    except SyntaxError as e:
        print(f"  SyntaxError in {filepath}: {e}")
        return set(), set(), set(), {}

    # 1. 所有定義的函數
    funcs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.add(node.name)

    # 2. import 名稱
    imports: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imports.add(alias.asname or alias.name)

    # 3. 頂層 dict：TOOLS = {"KEY": func} → dict名 → {func, ...}
    dict_contents: dict[str, set] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    contained = {v.id for v in node.value.values
                                 if isinstance(v, ast.Name) and v.id in funcs}
                    if contained:
                        dict_contents[t.id] = contained

    # 4. 頂層變數來源：tokenizer, model = load_model() → var → producer
    var_source: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            producer = None
            if isinstance(node.value.func, ast.Name):
                producer = node.value.func.id
            elif isinstance(node.value.func, ast.Attribute):
                producer = node.value.func.attr
            if producer and producer in funcs:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        var_source[t.id] = producer
                    elif isinstance(t, ast.Tuple):
                        for e in t.elts:
                            if isinstance(e, ast.Name):
                                var_source[e.id] = producer

    # 5. 函數參數名稱
    func_params: dict[str, list] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_params[node.name] = [a.arg for a in node.args.args]

    # 6. 找呼叫時傳入 dict 的對應：func(TOOLS) → call_dict_params[func][param] = dict名
    call_dict_params: dict[str, dict] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = None
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            if callee and callee in funcs and callee in func_params:
                params = func_params[callee]
                for i, arg in enumerate(node.args):
                    if isinstance(arg, ast.Name) and arg.id in dict_contents:
                        if i < len(params):
                            if callee not in call_dict_params:
                                call_dict_params[callee] = {}
                            call_dict_params[callee][params[i]] = arg.id

    # 7. 分析每個函數用到的外部變數
    class VarVisitor(ast.NodeVisitor):
        def __init__(self):
            self.current = None
            self.params: set[str] = set()
            self.local_vars: set[str] = set()
            self.comp_vars: set[str] = set()
            self.func_vars: dict[str, list] = {}

        def visit_FunctionDef(self, node):
            prev = self.current
            prev_params = self.params
            prev_locals = self.local_vars
            prev_comp = self.comp_vars
            self.current = node.name
            self.params = {a.arg for a in node.args.args}
            self.local_vars = set()
            self.comp_vars = set()
            self.func_vars[node.name] = []
            self.generic_visit(node)
            self.current = prev
            self.params = prev_params
            self.local_vars = prev_locals
            self.comp_vars = prev_comp

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node):
            self.generic_visit(node.value)
            if self.current:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.local_vars.add(t.id)
                    elif isinstance(t, ast.Tuple):
                        for e in t.elts:
                            if isinstance(e, ast.Name):
                                self.local_vars.add(e.id)

        def visit_comprehension(self, node):
            if isinstance(node.target, ast.Name):
                self.comp_vars.add(node.target.id)
            elif isinstance(node.target, ast.Tuple):
                for e in node.target.elts:
                    if isinstance(e, ast.Name):
                        self.comp_vars.add(e.id)
            self.generic_visit(node)

        def visit_Name(self, node):
            if not self.current or not isinstance(node.ctx, ast.Load):
                self.generic_visit(node)
                return
            name = node.id
            vars_list = self.func_vars[self.current]

            # dict 參數展開優先（在 params 過濾之前）：tools → TOOLS + calculator
            if (self.current in call_dict_params
                    and name in call_dict_params[self.current]):
                dict_name = call_dict_params[self.current][name]
                if dict_name not in vars_list:
                    vars_list.append(dict_name)
                for f in sorted(dict_contents.get(dict_name, set())):
                    if f not in vars_list:
                        vars_list.append(f)
                self.generic_visit(node)
                return

            if (name in self.params
                    or name in self.local_vars
                    or name in self.comp_vars
                    or name in BUILTINS
                    or name in imports
                    or name.startswith("_")
                    or name == self.current):
                self.generic_visit(node)
                return

            # 全域 dict 直接使用
            elif name in dict_contents:
                if name not in vars_list:
                    vars_list.append(name)
                for f in sorted(dict_contents[name]):
                    if f not in vars_list:
                        vars_list.append(f)
            # 全域變數由函數產生
            elif name in var_source:
                producer = var_source[name]
                if producer not in vars_list:
                    vars_list.append(producer)
            else:
                if name not in vars_list:
                    vars_list.append(name)

            self.generic_visit(node)

    vov = VarVisitor()
    vov.visit(tree)
    func_vars = vov.func_vars

    # 8. 頂層賦值鏈：SYSTEM_PROMPT = build_tool_prompt(TOOLS)
    #    → agent(MESSAGES) 間接依賴 build_tool_prompt
    def collect_top_calls(node, result: set):
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name) and child.func.id in funcs:
                    result.add(child.func.id)

    top_var_funcs: dict[str, set] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    deps: set = set()
                    collect_top_calls(node.value, deps)
                    for child in ast.walk(node.value):
                        if isinstance(child, ast.Name) and child.id in top_var_funcs:
                            deps.update(top_var_funcs[child.id])
                    if deps:
                        top_var_funcs[t.id] = deps

    class TopCallInjector(ast.NodeVisitor):
        def __init__(self):
            self.current = None
        def visit_FunctionDef(self, node):
            prev = self.current
            self.current = node.name
            self.generic_visit(node)
            self.current = prev
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_Call(self, node):
            callee = None
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            if callee and callee in funcs:
                target = self.current  # 在函數內 → 注入該函數；在頂層 → 注入 callee 自己
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id in top_var_funcs:
                        inject_to = target if target else callee
                        for dep in top_var_funcs[arg.id]:
                            if dep != inject_to and dep not in func_vars.get(inject_to, []):
                                func_vars.setdefault(inject_to, []).append(dep)
            self.generic_visit(node)

    TopCallInjector().visit(tree)

    # var_edges 保留給外部使用（目前圖已改用 func_vars 推導）
    var_edges: set[tuple[str, str]] = set()
    for func, vars_used in func_vars.items():
        for var in vars_used:
            if var in funcs and var != func:
                var_edges.add((func, var))

    return funcs, set(), var_edges, func_vars


def make_graph(filepath: str, out_dir: str):
    name = os.path.splitext(os.path.basename(filepath))[0]
    print(f"\n[{name}]")

    funcs, call_edges, var_edges, func_vars = analyze(filepath)

    # 從 func_vars 推導邊，方向是「前置條件 → 依賴它的函數」
    # 1. var 名稱直接是函數名：A 用到 B → B 是 A 的前置條件 → 邊 B→A
    # 2. var 是某函數產生的（透過 var_edges）：A 用到 var，var 由 B 產生 → 邊 B→A
    derived_edges: set[tuple[str, str]] = set()

    # var_source: 把 var_edges 反查 dependency→producer
    var_source_map: dict[str, str] = {}
    for dependent, producer in var_edges:
        # var_edges = (func_that_uses_var, func_that_produces_var)
        # 我們需要知道哪個 var 對應哪個 producer
        # 直接用 var_edges 建邊：producer → dependent
        derived_edges.add((producer, dependent))

    for func, vars_used in func_vars.items():
        for var in vars_used:
            if var in funcs and var != func:
                # var 名稱就是函數名，直接建邊 var→func
                derived_edges.add((var, func))

    all_edges = derived_edges

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
        '  graph [rankdir=TB, bgcolor="#0d1117", pad=0.8, ranksep=1.2, nodesep=0.8, fontname="Courier New"];',
        '  node  [shape=box, style="filled,rounded", fillcolor="white", fontcolor="#111111",',
        '         fontname="Courier New", fontsize=12, color="#888888", width=1.6, penwidth=1.5];',
        '  edge  [arrowsize=0.8, penwidth=2.0];',
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

    # 邊從 derived_edges 建立：src 是前置條件，dst 是依賴它的函數
    for src, dst in sorted(derived_edges):
        if src in funcs and dst in funcs:
            dot_lines.append(f'  {src} -> {dst} [color="#00cfff", penwidth=2.0];')

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
        [{"from": s, "to": d, "color": {"color": "#00cfff"}, "arrows": "to"} for s, d in sorted(derived_edges) if s in funcs and d in funcs]
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>{name} – {date}</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{background:#0d1117;font-family:'Courier New',monospace;color:#ccccdd}}
  #header{{padding:14px 22px;border-bottom:1px solid #444466;display:flex;align-items:center;justify-content:space-between}}
  #header h1{{font-size:14px;color:#00cfff;letter-spacing:.06em}}
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
    <span><span class="dot" style="background:#00cfff"></span>直接呼叫</span>
    <span><span class="dot" style="background:#ffaa00"></span>全域變數依賴</span>
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
  nodes:{{shape:"box",borderRadius:6,color:{{background:"#ffffff",border:"#888888",highlight:{{background:"#e8f4ff",border:"#00cfff"}}}},font:{{color:"#111111",face:"Courier New",size:13}},shadow:{{enabled:true,color:"rgba(0,0,0,.4)",x:2,y:2,size:6}}}},
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

# 收集所有依賴關係
all_deps = {}
for fp in py_files:
    name = os.path.splitext(os.path.basename(fp))[0]
    funcs, call_edges, var_edges, func_vars = analyze(fp)
    all_deps[name] = {
        "functions": sorted(funcs),
        "variables_used": {k: v for k, v in func_vars.items()},
        "call_dependencies": sorted([{"from": s, "to": d} for s, d in call_edges], key=lambda x: x["from"]),
        "variable_dependencies": sorted([{"from": s, "to": d} for s, d in var_edges], key=lambda x: x["from"]),
    }
    make_graph(fp, OUTPUT_DIR)

# 輸出 dependencies.json
deps_path = os.path.join(OUTPUT_DIR, "dependencies.json")
with open(deps_path, "w", encoding="utf-8") as f:
    json.dump(all_deps, f, indent=2, ensure_ascii=False)
shutil.copy2(deps_path, os.path.join(LATEST_DIR, "dependencies.json"))
print(f"JSON → {deps_path}")

# ── 產生 CSV（每個檔案各一份）────────────────────────────────────
for file_name, info in all_deps.items():
    rows = []
    for func, vars_used in info.get("variables_used", {}).items():
        rows.append([func] + vars_used)

    max_cols = max((len(r) for r in rows), default=1)
    header = ["function_name"] + [f"var_{i+1}" for i in range(max_cols - 1)]

    csv_path = os.path.join(OUTPUT_DIR, f"{file_name}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row + [""] * (max_cols - len(row)))

    shutil.copy2(csv_path, os.path.join(LATEST_DIR, f"{file_name}.csv"))
    print(f"CSV  → {csv_path}")


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

separator = "\n\n---\n\n"
readme = f"""# {repo_name}

---

{separator.join(sections)}
"""

with open("README.md", "w", encoding="utf-8") as f:
    f.write(readme)

print("README.md updated.")

print("\nDone.")
