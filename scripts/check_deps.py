#!/usr/bin/env python
"""部署前自查：依赖声明完整性 + .vercelignore 排除规则安全性。

背景（2026-09-14 Vercel 部署事故，两类坑都是"本地能跑、线上必崩"）：

  坑 1 · 依赖未声明
    现象：Vercel 构建成功，但访问报 500 / This Serverless Function has crashed
          （Code: FUNCTION_INVOCATION_FAILED）——函数进程级崩溃，不是 FastAPI 返回的 500。
    根因：`src/services/astrology.py` 模块级 `from pymeeus.Epoch import Epoch`、
          `src/agent/report_agent.py` 模块级 `import aiohttp`，而这两个包从未写进
          requirements.txt / pyproject.toml。冷启动链
          `api/index.py → src.server → src.routes.astrology` 直接 ModuleNotFoundError。
          本地因环境里恰好手装过，故一直不暴露；pip 只装声明过的包，线上必崩。

  坑 2 · .vercelignore 规则连坐
    现象：包结构静默退化（子包__init__副作用失效），或某个源码文件根本没被上传。
    根因：曾写 `_*.py` 想排临时脚本，但 gitignore 语义下 `_*` 会连 `__init__.py`
          一起匹配 —— 一行规则把 src/ 下 7 个 __init__.py 全排除了。
          临时脚本已统一放 scripts/，不需要这条规则。

用法：
    python scripts/check_deps.py            # 默认检查当前目录
    python scripts/check_deps.py <项目根>

退出码 0 = 通过；1 = 存在缺失依赖或被误排除的源码。
"""
from __future__ import annotations

import ast
import fnmatch
import pathlib
import sys

# 导入名 ≠ 包名 的常见映射（导入名 -> PyPI 包名）
ALIAS = {
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "pil": "pillow",
    "yaml": "pyyaml",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
}

# 由已声明依赖自动带入的传递依赖（自身不必写进 requirements.txt）
TRANSITIVE = {
    "starlette",      # fastapi 的运行时依赖
    "pydantic_core",  # pydantic 的运行时依赖
}

# 不参与检查的目录（测试/本地脚本/虚拟环境等）
SKIP_DIRS = {".venv", "venv", ".git", "tests", "scripts", "__pycache__",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", "alembic"}


def norm(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def collect_imports(root: pathlib.Path) -> dict[str, set[str]]:
    """AST 提取第三方顶层导入 -> 出现文件集合。"""
    found: dict[str, set[str]] = {}
    for p in root.rglob("*.py"):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            # utf-8-sig：容忍部分文件带 BOM（backend 若干净，此处不加会误报解析失败）
            tree = ast.parse(p.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError) as e:
            print(f"  [警告] 解析失败 {rel}: {e}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    found.setdefault(a.name.split(".")[0], set()).add(str(rel))
            elif isinstance(node, ast.ImportFrom):
                # level>0 是相对导入（本包内），不参与第三方判定
                if node.level == 0 and node.module:
                    found.setdefault(node.module.split(".")[0], set()).add(str(rel))
    return found


def local_roots(root: pathlib.Path) -> set[str]:
    """项目内可导入的顶层名（目录或同名 .py）。"""
    names = {p.stem for p in root.glob("*.py")}
    names |= {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}
    return names


def declared_packages(root: pathlib.Path) -> set[str]:
    """解析 requirements.txt + pyproject.toml 里声明的包名。"""
    declared: set[str] = set()
    req = root / "requirements.txt"
    if req.exists():
        for line in req.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            name = line.split("[")[0].split("=")[0].split(">")[0].split("<")[0].split("~")[0]
            name = name.strip().rstrip(";").strip()
            if name:
                declared.add(norm(name))
    pyproj = root / "pyproject.toml"
    if pyproj.exists():
        try:
            import tomllib
            data = tomllib.loads(pyproj.read_text(encoding="utf-8"))
            for spec in (data.get("project", {}) or {}).get("dependencies", []) or []:
                name = spec.split("[")[0].split("=")[0].split(">")[0].split("<")[0].split("~")[0]
                declared.add(norm(name))
        except Exception as e:  # tomllib 缺失或 TOML 非法
            print(f"  [警告] 读取 pyproject.toml 失败: {e}")
    return declared


def check_deps(root: pathlib.Path) -> list[str]:
    third = collect_imports(root)
    # 标准库 / 项目内模块 / 传递依赖都不需要声明
    skip = local_roots(root) | set(sys.stdlib_module_names) | TRANSITIVE | {"__future__"}
    declared = declared_packages(root)

    missing = []
    for mod in sorted(third):
        if mod in skip:
            continue
        if norm(ALIAS.get(mod, mod)) in declared:
            continue
        missing.append(f"{mod:<22} (首次出现于 {sorted(third[mod])[0]})")
    return missing


def check_vercelignore(root: pathlib.Path) -> list[str]:
    """模拟 .vercelignore 匹配，找出被误排除的 .py（tests/scripts 除外）。"""
    vi = root / ".vercelignore"
    if not vi.exists():
        return []
    pats = [l.strip() for l in vi.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]

    def ignored(rel: str) -> str | None:
        parts = rel.split("/")
        for p in pats:
            if p.endswith("/"):
                d = p.rstrip("/")
                if any(fnmatch.fnmatch(seg, d) for seg in parts):
                    return p
            elif "/" in p:
                if fnmatch.fnmatch(rel, p):
                    return p
            elif any(fnmatch.fnmatch(seg, p) for seg in parts):
                return p
        return None

    bad = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        if rel.startswith((".venv/", ".git/")):
            continue
        if rel.startswith(("tests/", "scripts/")):
            continue  # 这两类是有意排除的
        rule = ignored(rel)
        if rule:
            bad.append(f"{rel:<40} 被规则 {rule!r} 排除")
    return bad


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    print(f"自查目录: {root}\n")

    ok = True

    print("[1/2] 依赖声明完整性（源码 import 是否都在 requirements/pyproject 里）")
    missing = check_deps(root)
    if missing:
        ok = False
        print(f"  ❌ 发现 {len(missing)} 个未声明的第三方依赖（Vercel 上会冷启动崩溃）：")
        for m in missing:
            print(f"     - {m}")
        print("  → 补进 requirements.txt 与 pyproject.toml 的 dependencies")
    else:
        print("  ✅ 通过")

    print("\n[2/2] .vercelignore 排除规则（源码是否被误排除）")
    bad = check_vercelignore(root)
    if bad:
        ok = False
        print(f"  ❌ 发现 {len(bad)} 个源码文件被排除（线上缺文件 / 包结构退化）：")
        for b in bad:
            print(f"     - {b}")
    else:
        print("  ✅ 通过")

    print("\n" + ("✅ 全部通过，可部署" if ok else "❌ 存在问题，请先修复再部署"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
