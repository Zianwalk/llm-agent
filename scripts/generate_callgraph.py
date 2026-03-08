import subprocess
import os
from datetime import datetime

# 取得 branch 名稱
try:
    branch = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    ).decode().strip()
except:
    branch = "main"

# 建立輸出資料夾，用日期+branch管理
date = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
OUTPUT_DIR = f"callgraphs/{date}_{branch}"
os.makedirs(OUTPUT_DIR, exist_ok=True)

GRAPH_DOT = os.path.join(OUTPUT_DIR, "callgraph.dot")
GRAPH_PNG = os.path.join(OUTPUT_DIR, "callgraph.png")

# 找所有 python 檔案
py_files = []
for root, dirs, files in os.walk("."):
    if ".git" in root or "callgraphs" in root:
        continue
    for f in files:
        if f.endswith(".py"):
            py_files.append(os.path.join(root, f))

# 生成 dot graph
cmd = ["pyan", *py_files, "--dot"]
with open(GRAPH_DOT, "w") as f:
    subprocess.run(cmd, stdout=f)

# dot → png
subprocess.run([
    "dot",
    "-Tpng",
    GRAPH_DOT,
    "-o",
    GRAPH_PNG
])

print(f"Callgraph generated in {GRAPH_PNG}")