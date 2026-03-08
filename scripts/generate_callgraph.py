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

print(f"Found {len(py_files)} file(s): {py_files}")


def make_graph(files: list[str], name: str, out_dir: str):
    """pyan3 分析 files，產生 {name}.png 和 {name}.html 到 out_dir"""
    raw_dot = os.path.join(out_dir, f"{name}_raw.dot")
    out_dot  = os.path.join(out_dir, f"{name}.dot")
    out_png  = os.path.join(out_dir, f"{name}.png")
    out_html = os.path.join(out_dir, f"{name}.html")

    # pyan3
    cmd = ["pyan3", *files, "--uses", "--no-defines", "--dot"]
    with open(raw_dot, "w") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)

    if result.returncode != 0:
        print(f"  pyan3 error ({name}):", result.stderr.decode().strip())
        return None, None

    with open(raw_dot) as f:
        raw = f.read()

    # ── 合併同 label 的重複節點 ───────────────────────────────────
    id_to_label: dict[str, str] = {}
    for m in re.finditer(r'(\w+)\s*\[.*?label\s*=\s*"([^"]+)"', raw):
        id_to_label[m.group(1)] = m.group(2)

    label_to_canonical: dict[str, str] = {}
    for nid, lbl in id_to_label.items():
        if lbl not in label_to_canonical:
            label_to_canonical[lbl] = nid

    id_to_canonical = {nid: label_to_canonical[lbl] for nid, lbl in id_to_label.items()}

    edges: set[tuple[str, str]] = set()
    for m in re.finditer(r'(\w+)\s*->\s*(\w+)', raw):
        src = id_to_canonical.get(m.group(1), m.group(1))
        dst = id_to_canonical.get(m.group(2), m.group(2))
        if src != dst:
            edges.add((src, dst))

    canonical_ids = set(label_to_canonical.values())

    if not edges:
        print(f"  No edges found for {name}, skipping.")
        return None, None

    # ── dot ───────────────────────────────────────────────────────
    dot_lines = [
        f'digraph "{name}" {{',
        '  graph [rankdir=LR, bgcolor="#0d1117", pad=0.6, splines=curved, fontname="Courier New"];',
        '  node  [shape=box, style="filled,rounded", fillcolor="#161b22", fontcolor="#c9d1d9",',
        '         fontname="Courier New", fontsize=12, color="#30363d"];',
        '  edge  [color="#58a6ff", arrowsize=0.7, penwidth=1.3];',
    ]
    for nid in sorted(canonical_ids):
        dot_lines.append(f'  {nid} [label="{id_to_label[nid]}"];')
    for src, dst in sorted(edges):
        dot_lines.append(f'  {src} -> {dst};')
    dot_lines.append("}")

    with open(out_dot, "w") as f:
        f.write("\n".join(dot_lines))

    # ── PNG ───────────────────────────────────────────────────────
    r = subprocess.run(["dot", "-Tpng", out_dot, "-o", out_png], capture_output=True)
    if r.returncode != 0:
        print(f"  dot error ({name}):", r.stderr.decode().strip())
        return None, None
    print(f"  PNG  → {out_png}")

    # ── HTML ──────────────────────────────────────────────────────
    vis_nodes = json.dumps([{"id": nid, "label": id_to_label[nid]} for nid in sorted(canonical_ids)])
    vis_edges = json.dumps([{"from": s, "to": d, "arrows": "to"} for s, d in sorted(edges)])

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>{name} – {date}_{branch}</title>
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
  <h1>&#128336; {name}</h1>
  <span>{branch} &nbsp;·&nbsp; {date}</span>
</div>
<div id="graph"></div>
<script>
const nodes = new vis.DataSet({vis_nodes});
const edges = new vis.DataSet({vis_edges});
const options = {{
  physics:{{solver:"forceAtlas2Based",forceAtlas2Based:{{gravitationalConstant:-80,springLength:130}},stabilization:{{iterations:250}}}},
  nodes:{{shape:"box",borderRadius:6,color:{{background:"#161b22",border:"#30363d",highlight:{{background:"#1f6feb",border:"#58a6ff"}}}},font:{{color:"#c9d1d9",face:"Courier New",size:13}},shadow:{{enabled:true,color:"rgba(0,0,0,.5)",x:2,y:2,size:8}}}},
  edges:{{color:{{color:"#58a6ff",highlight:"#79c0ff"}},smooth:{{type:"curvedCW",roundness:0.15}},arrows:{{to:{{enabled:true,scaleFactor:0.7}}}}}},
  interaction:{{hover:true,zoomView:true}}
}};
new vis.Network(document.getElementById("graph"),{{nodes,edges}},options);
</script>
</body>
</html>"""

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML → {out_html}")

    return out_png, out_html


# ── 每個 .py 各產一張圖 ───────────────────────────────────────────
for fp in py_files:
    name = os.path.splitext(os.path.basename(fp))[0]
    print(f"\n[{name}]")
    png, html = make_graph([fp], name, OUTPUT_DIR)

    # 更新 latest/
    if png:
        shutil.copy2(png,  os.path.join(LATEST_DIR, os.path.basename(png)))
    if html:
        shutil.copy2(html, os.path.join(LATEST_DIR, os.path.basename(html)))

print("\nDone.")
