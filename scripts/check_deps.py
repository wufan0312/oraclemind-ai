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

  坑 3 · .gitignore 规则连坐（2026-09-30 二次事故）
    现象：Vercel Runtime Logs 报
          ImportError: cannot import name 'route_trace' from 'src.harness' (unknown location)
    根因：与坑 2 同一行 `_*.py`，但残留在 **.gitignore** 里（坑 2 只修了 .vercelignore）。
          Vercel 走 Git 集成部署，产物 = git 仓库内容；被 .gitignore 排除的文件
          **根本不在产物里**。本地磁盘上有 → import 正常；线上没有 → 全部子包
          退化为 namespace package（报错里的 `(unknown location)` 即此意），
          包内 __init__ 副作用（如 src/harness/__init__.py 导出的 route_trace）静默失效。
          修复：`_*.py` 后追加否定规则 `!**/__init__.py`。
          （同理 .env.development / .env.production 这类非敏感默认值也必须入库。）

  坑 4 · 运行时名字解析错误（2026-09-30 三次事故，同一类）
    现象：服务终于起来了（HTTP 200），但业务全链路降级、日志里刷警告。两次实例：
          ① `name '_orig_chat' is not defined`
             src/harness/observability/patch.py 的 traced_chat 里裸写了 _orig_chat，
             但 install_tracing 只捕获了 _orig_stream，漏了 _orig_chat。
             CPython 把该名字按「全局名」解析 → NameError。
             TRACE_ENABLED 默认 True，故 provider.chat() 的 6 个调用点全挂
             （summary/stream、interpret、poster 一起降级）。
          ② `cannot access local variable 'cross_labels'`
             src/agent/report_agent.py 的 mark_phase(...) 用了 cross_labels，
             而 cross_labels 在 8 行之后才赋值 → UnboundLocalError，报告链路
             在首个事件前就崩。
    根因：这两类错（F821 未定义名 / 局部变量先读后写）**只在被执行到时才炸**，
          单测覆盖不到就没信号；解释器不执行也不报错。本地同样会炸，只是没人跑那条分支。
    修复：把原始方法先捕获进局部变量（闭包），并把 cross_labels 的计算提到使用之前。
    防复发：本脚本新增 [4/4] 静态名字解析（symtable + dis，纯标准库），
          在部署前静态扫出「未定义全局名」与「局部变量赋值前被读取」。

用法：
    python scripts/check_deps.py            # 默认检查当前目录
    python scripts/check_deps.py <项目根>

退出码 0 = 通过；1 = 存在缺失依赖或被误排除的源码。
"""
from __future__ import annotations

import ast
import dis
import fnmatch
import pathlib
import symtable
import sys

# 解释器注入的模块级名字（symtable 里看不到，须白名单）
_BUILTIN_EXTRA = {
    "__name__", "__file__", "__doc__", "__builtins__", "__package__",
    "__spec__", "__loader__", "__annotations__", "__debug__", "__class__",
    "__dict__", "WindowsError",
}

# 读取局部变量但会「先读后写」的合法场景很少，这里只认真正的读取指令。
#
# ⚠️ CPython 3.13 起有「超级指令」：一条 opcode 同时操作两个名字，argval 是元组，
#    例如 `_build(parsed, rid, ...)` → LOAD_FAST_LOAD_FAST ('parsed','rid')、
#    `a, b = f()` → STORE_FAST_STORE_FAST ('a','b')。
#    因此必须用「前缀匹配 + 元组展开」，不能精确比对单一 opcode 名。
def _classify(op: str, argval):
    """把一条指令拆成 (写入的局部名, 读取的局部名, 读取的全局名)。

    LOAD_FAST_AND_CLEAR 是内联推导式用来暂存外层变量的，不算读取。
    """
    names = argval if isinstance(argval, tuple) else (argval,)
    writes: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()
    globals_: tuple[str, ...] = ()

    if op == "LOAD_FAST_AND_CLEAR":
        return writes, reads, globals_
    if op == "STORE_FAST_LOAD_FAST":
        return (names[0],), (names[1],), globals_
    if op == "STORE_FAST_STORE_FAST":
        return names, reads, globals_
    if op.startswith("STORE_FAST") or op.startswith("DELETE_FAST"):
        return names, reads, globals_
    if op.startswith("LOAD_FAST"):
        return writes, names, globals_
    if op.startswith("LOAD_GLOBAL"):
        return writes, reads, names
    return writes, reads, globals_

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


# 必须入库的「非敏感分层默认值」env 文件（缺则线上静默回落代码默认值）
REQUIRED_ENV_FILES = (".env.example", ".env.development", ".env.production")

# 有意忽略、不得入库的 env（含密钥 / 个人本地兜底）
INTENTIONALLY_IGNORED_ENV = {".env", ".env.local", ".env.prod"}


def _git(root: pathlib.Path, *args: str) -> list[str] | None:
    """执行 git 子命令并返回非空行；非 git 仓库或命令失败返回 None。"""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(root), *args],
                             check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return [l for l in out.stdout.splitlines() if l.strip()]


def check_git_tracked(root: pathlib.Path) -> list[str]:
    """坑 3 专项：Vercel 走 Git 集成部署，产物 = git 仓库内容。

    因此「磁盘上有、但被 .gitignore 排除」的文件在生产里根本不存在 ——
    本地 import 正常、线上必崩，且报错不含文件名（只报 unknown location），极难定位。
    此处模拟该场景：列出被 .gitignore 忽略的源码 / 必备文件。
    """
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        return []  # 非 git 仓库，跳过（不影响其它检查）

    ignored = set(_git(root, "ls-files", "--others", "--ignored",
                       "--exclude-standard") or [])
    if not ignored:
        return []

    bad: list[str] = []

    # 1) 包结构完整性：所有 __init__.py 都不允许被忽略（否则子包退化）
    for rel in sorted(ignored):
        parts = rel.split("/")
        if any(part in SKIP_DIRS for part in parts):
            continue
        if parts[-1] == "__init__.py":
            bad.append(f"{rel:<44} 被 .gitignore 排除 → 子包退化为 namespace package")

    # 2) src/ 下被忽略的源码文件（线上会缺文件，ModuleNotFoundError / 静默降级）
    for rel in sorted(ignored):
        parts = rel.split("/")
        if any(part in SKIP_DIRS for part in parts):
            continue
        if rel.startswith("src/") and rel.endswith(".py") and parts[-1] != "__init__.py":
            bad.append(f"{rel:<44} 被 .gitignore 排除 → 线上缺源码文件")

    # 3) 必备的非敏感分层 env 默认值
    for name in REQUIRED_ENV_FILES:
        if (root / name).exists() and name in ignored:
            bad.append(f"{name:<44} 被 .gitignore 排除 → 线上回落代码默认值（如 localhost）")

    # 4) 反例断言：含密钥的 .env 必须保持被忽略，否则有泄密风险
    for name in sorted(INTENTIONALLY_IGNORED_ENV):
        if (root / name).exists() and name not in ignored:
            bad.append(f"{name:<44} 未被忽略 → 密钥有入库泄露风险，请检查 .gitignore")

    return bad


def _arg_names(code) -> set[str]:
    """形参名（含 *args / **kwargs）：它们由调用方传入，天然已绑定。"""
    import inspect

    n = code.co_argcount + code.co_kwonlyargcount
    if code.co_flags & inspect.CO_VARARGS:
        n += 1
    if code.co_flags & inspect.CO_VARKEYWORDS:
        n += 1
    return set(code.co_varnames[:n])


def _scan_code(code, rel: str, module_names: set[str], builtins_ok: set[str],
               issues: list[str], is_module_code: bool) -> None:
    """按字节码指令顺序，扫出「未定义名」与「局部变量先读后写」。"""
    args = _arg_names(code)
    # ⚠️ 3.11+ 移除了 LOAD_CLOSURE：构造闭包时用 LOAD_FAST 加载「cell 对象本身」，
    #    那不是取值读取。cell/free 变量的取值走 LOAD_DEREF，故整类跳过。
    cells = set(code.co_cellvars) | set(code.co_freevars)
    stored: set[str] = set()

    for ins in dis.get_instructions(code):
        # 3.11+ 才有逐指令精确行号；starts_line 在 3.13 已变成布尔语义，不可用
        line = getattr(ins, "positions", None)
        line = line.lineno if line else None
        writes, reads, globs = _classify(ins.opname, ins.argval)
        if cells:
            writes = tuple(n for n in writes if n not in cells)
            reads = tuple(n for n in reads if n not in cells)

        for name in writes:
            stored.add(name)
        for name in reads:
            if name not in stored and name not in args:
                issues.append(
                    f"{rel}:{line}: 局部变量 {name!r} 在赋值前被读取 → 触发时 UnboundLocalError"
                )
        for name in globs:
            if name not in module_names and name not in builtins_ok:
                issues.append(
                    f"{rel}:{line}: 名字 {name!r} 未定义（模块级与内置都没有）→ NameError"
                )
        if is_module_code and ins.opname.startswith("LOAD_NAME"):
            for name in (ins.argval if isinstance(ins.argval, tuple) else (ins.argval,)):
                if name not in module_names and name not in builtins_ok:
                    issues.append(
                        f"{rel}:{line}: 名字 {name!r} 未定义（模块级与内置都没有）→ NameError"
                    )

    # 内嵌函数 / 推导式 / 类体各自是独立 code object，需递归
    for const in code.co_consts:
        if hasattr(const, "co_code"):
            _scan_code(const, rel, module_names, builtins_ok, issues, False)


def _module_bindings(st: symtable.SymbolTable) -> set[str]:
    """模块顶层所有被绑定过的名字（导入 / 赋值 / def / class / 形参）。"""
    names: set[str] = set()
    for sym in st.get_symbols():
        if sym.is_imported() or sym.is_assigned() or sym.is_namespace() or sym.is_parameter():
            names.add(sym.get_name())
    return names


def check_names(root: pathlib.Path) -> list[str]:
    """坑 4 专项：静态扫「未定义名 / 局部变量先读后写」。

    这两类错只在对应分支被执行时才炸（本地同样会炸），解释器不执行不报错，
    单测覆盖不到就没信号 —— 本检查不需要运行代码，源文件级别就能拦下。
    """
    import builtins

    builtins_ok = set(dir(builtins)) | _BUILTIN_EXTRA
    issues: list[str] = []

    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        try:
            src = p.read_text(encoding="utf-8-sig")
            st = symtable.symtable(src, str(p), "exec")
            code = compile(src, str(p), "exec")
        except (SyntaxError, UnicodeDecodeError, ValueError) as e:
            print(f"  [警告] 无法静态解析 {rel}: {e}")
            continue

        # `from x import *` 会注入无法静态枚举的名字，跳过该文件的全局名检查
        has_star_import = any(
            isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)
            for node in ast.walk(ast.parse(src))
        )
        module_names = _module_bindings(st)

        file_issues: list[str] = []
        _scan_code(code, rel, module_names, builtins_ok, file_issues, True)
        if has_star_import:
            # 星号导入会注入无法枚举的名字，只保留局部变量检查，避免误报
            file_issues = [i for i in file_issues if "UnboundLocalError" in i]
        issues.extend(file_issues)

    return issues


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    print(f"自查目录: {root}\n")

    ok = True

    print("[1/4] 依赖声明完整性（源码 import 是否都在 requirements/pyproject 里）")
    missing = check_deps(root)
    if missing:
        ok = False
        print(f"  ❌ 发现 {len(missing)} 个未声明的第三方依赖（Vercel 上会冷启动崩溃）：")
        for m in missing:
            print(f"     - {m}")
        print("  → 补进 requirements.txt 与 pyproject.toml 的 dependencies")
    else:
        print("  ✅ 通过")

    print("\n[2/4] .vercelignore 排除规则（源码是否被误排除）")
    bad = check_vercelignore(root)
    if bad:
        ok = False
        print(f"  ❌ 发现 {len(bad)} 个源码文件被排除（线上缺文件 / 包结构退化）：")
        for b in bad:
            print(f"     - {b}")
    else:
        print("  ✅ 通过")

    print("\n[3/4] .gitignore 排除规则（Vercel 产物 = git 仓库，被忽略即线上缺失）")
    gitbad = check_git_tracked(root)
    if gitbad:
        ok = False
        print(f"  ❌ 发现 {len(gitbad)} 个必要文件被 .gitignore 排除（本地能跑、线上必崩）：")
        for b in gitbad:
            print(f"     - {b}")
        print("  → 加否定规则（如 `!**/__init__.py`）或从 .gitignore 移除过宽通配")
    else:
        print("  ✅ 通过")

    print("\n[4/4] 静态名字解析（未定义名 / 局部变量先读后写，只在执行到才炸）")
    namebad = check_names(root)
    if namebad:
        ok = False
        print(f"  ❌ 发现 {len(namebad)} 处运行期才会暴露的名字错误：")
        for b in namebad:
            print(f"     - {b}")
        print("  → 未定义名：补上定义，或把外层变量先捕获进局部变量再用")
        print("  → 先读后写：把赋值语句挪到首次使用之前")
    else:
        print("  ✅ 通过")

    print("\n" + ("✅ 全部通过，可部署" if ok else "❌ 存在问题，请先修复再部署"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
