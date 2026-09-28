#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""玄镜 OracleMind · AI 服务启停脚本（纯标准库，Windows / Linux 通用，无 psutil 依赖）

为什么需要它：
    之前 AI 服务偶尔「端口被占、进程杀不死」，根因是 uvicorn 开了 reload——
    reloader 会再 fork 出 worker 进程持有监听套接字；一旦父进程异常退出，
    worker 变成脱离进程树的「孤儿监听」，只能被迫换端口。
    现在 server.py 已默认关闭 reload（单进程），本脚本负责用「可控、可追踪」的
    方式启动/停止，杜绝端口漂移：

    - 单进程运行（`uvicorn --no-reload`），从根上消除双进程孤儿。
    - 脱离控制台启动（DETACHED_PROCESS / start_new_session），父进程退出不影响服务。
    - 启动后写 pidfile，停止时按「pidfile + 端口扫描」双重清理，确保端口释放干净。
    - 启动后轮询 /health 做健康检查，确认真正可用再返回。

用法（在 oraclemind-ai 目录下执行）：
    python scripts/ai_server.py start        # 启动（默认端口 8021）
    python scripts/ai_server.py stop         # 停止
    python scripts/ai_server.py restart      # 重启
    python scripts/ai_server.py status       # 查看状态
可选参数：
    --port 8099       覆盖端口（默认读 AI_SERVER_PORT 环境变量，再 fallback 到 8021）
    --host 0.0.0.0    绑定地址（默认 0.0.0.0）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

# ---------- 路径与常量 ----------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)          # oraclemind-ai/
MONOREPO_ROOT = os.path.dirname(PROJECT_ROOT)         # F:/project/oraclemind（monorepo 根）
DEFAULT_PORT = 8021                                  # 与前端 .env.local 的 NEXT_PUBLIC_AI_API_BASE 对齐
HEALTH_PATH = "/health"
STARTUP_TIMEOUT = 30                                 # 秒：等待 /health 就绪
STOP_TIMEOUT = 15                                    # 秒：等待端口释放


def log(msg: str) -> None:
    print(f"[ai_server] {msg}")


def resolve_python() -> str:
    """优先用项目自带的 .venv，否则退回当前解释器。"""
    venv_py = os.path.join(
        PROJECT_ROOT, ".venv", "Scripts", "python.exe"
    ) if os.name == "nt" else os.path.join(PROJECT_ROOT, ".venv", "bin", "python")
    if os.path.isfile(venv_py):
        return venv_py
    return sys.executable


def pidfile_path(port: int) -> str:
    p = os.path.join(MONOREPO_ROOT, "outputs", "pids", f"oj_ai_server_{port}.pid")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def logfile_path(port: int) -> str:
    p = os.path.join(MONOREPO_ROOT, "outputs", "logs", f"oj_ai_server_{port}.log")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


# ---------- 端口占用检测 ----------
def pids_on_port(port: int) -> list[int]:
    """返回当前监听该端口的所有 PID（跨平台）。"""
    pids: set[int] = set()
    if os.name == "nt":
        # 注意：netstat 输出编码随系统代码页变化，可能含非 UTF-8 字节，
        # 用 text=True 会触发解码线程异常并让 .stdout 变成 None。改为读字节后安全解码。
        try:
            raw = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
            ).stdout or b""
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        for line in text.splitlines():
            # 形如:  TCP    0.0.0.0:8021   0.0.0.0:0   LISTENING   20032
            if ":{} ".format(port) not in line and ":{}:".format(port) not in line:
                continue
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if parts:
                try:
                    pids.add(int(parts[-1]))
                except ValueError:
                    pass
    else:
        out = subprocess.run(
            ["bash", "-c", f"lsof -ti tcp:{port} || true"],
            capture_output=True, text=True,
        ).stdout
        for tok in out.split():
            try:
                pids.add(int(tok))
            except ValueError:
                pass
    return sorted(pids)


def port_is_free(port: int) -> bool:
    return len(pids_on_port(port)) == 0


# ---------- 健康检查 ----------
def health_ok(port: int, host: str = "127.0.0.1") -> bool:
    url = f"http://{host}:{port}{HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


# ---------- pidfile ----------
def read_pid(port: int) -> int | None:
    path = pidfile_path(port)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (ValueError, OSError):
        return None


def write_pid(port: int, pid: int) -> None:
    with open(pidfile_path(port), "w", encoding="utf-8") as f:
        f.write(str(pid))


def clear_pid(port: int) -> None:
    path = pidfile_path(port)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def is_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            raw = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
            ).stdout or b""
            out = raw.decode("utf-8", errors="replace")
        except Exception:
            out = ""
        return str(pid) in out
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


# ---------- 启停动作 ----------
def kill_pid(pid: int) -> bool:
    if os.name == "nt":
        r = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    else:
        try:
            os.kill(pid, 15)
            return True
        except OSError:
            return False


def start(port: int, host: str) -> int:
    if not port_is_free(port):
        holders = pids_on_port(port)
        log(f"端口 {port} 已被占用（PID: {holders}）。")
        log("请先执行 `python scripts/ai_server.py stop` 或 `restart` 释放端口。")
        return 1

    py = resolve_python()
    log(f"使用解释器: {py}")

    # 脱离进程树启动：Windows 用 DETACHED_PROCESS + 新进程组；POSIX 用新会话
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

    log_path = logfile_path(port)
    log_file = open(log_path, "a", encoding="utf-8")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    log_file.write(f"\n===== start @ {ts} (port={port}) =====\n")
    log_file.flush()

    popen_kwargs: dict = dict(
        cwd=PROJECT_ROOT,
        stdout=log_file,
        stderr=log_file,
        close_fds=True,
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True

    cmd = [
        py, "-m", "uvicorn", "src.server:app",
        "--host", host,
        "--port", str(port),
        # 不传 --reload（uvicorn 默认即单进程、不开热重载），从根上杜绝孤儿监听。
        "--log-level", "info",
    ]
    log(f"启动命令: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, **popen_kwargs)
    pid = proc.pid
    log(f"已派生服务进程 PID={pid}（脱离控制台，已写入 pidfile）。")
    write_pid(port, pid)

    # 健康检查
    log(f"等待 /health 就绪（最多 {STARTUP_TIMEOUT}s）…")
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if health_ok(port):
            log(f"✅ 服务已就绪: http://{host}:{port}  (健康检查通过)")
            log(f"日志: {log_path}")
            return 0
        time.sleep(1.0)

    # 超时：服务可能仍在启动（模型/依赖加载慢），给出诊断信息但不强杀
    if is_alive(pid):
        log(f"⏳ 健康检查超时，但进程 PID={pid} 仍在运行，可能仍在加载依赖。")
        log(f"   请稍后执行 `python scripts/ai_server.py status` 复核；日志: {log_path}")
        return 0
    log(f"❌ 进程 PID={pid} 已退出，启动失败。请查看日志: {log_path}")
    clear_pid(port)
    return 1


def stop(port: int) -> int:
    killed_any = False

    # 1) 先按 pidfile 清理
    pid = read_pid(port)
    if pid is not None and is_alive(pid):
        log(f"停止 pidfile 记录的主进程 PID={pid} …")
        if kill_pid(pid):
            killed_any = True
            log(f"✅ 已终止 PID={pid}")
        else:
            log(f"⚠️ 无法终止 PID={pid}（可能属其他会话），转由端口扫描兜底。")
    clear_pid(port)

    # 2) 端口扫描兜底：清掉一切仍监听该端口的进程（含历史孤儿）
    holders = pids_on_port(port)
    if holders:
        log(f"端口 {port} 仍有监听进程 PID={holders}，尝试强制清理…")
        for h in holders:
            if kill_pid(h):
                killed_any = True
                log(f"✅ 已终止监听进程 PID={h}")
            else:
                log(f"⚠️ 无法终止 PID={h}（可能属其他会话/无权限）。")

    if not killed_any and not holders:
        log("未发现运行中的服务进程。")
        return 0

    # 3) 等待端口真正释放
    deadline = time.time() + STOP_TIMEOUT
    while time.time() < deadline:
        if port_is_free(port):
            log(f"✅ 端口 {port} 已释放。")
            return 0
        time.sleep(0.5)
    remaining = pids_on_port(port)
    if remaining:
        log(f"⚠️ 端口 {port} 仍未释放（残留 PID={remaining}）。")
        log("   这些进程可能不在当前会话中，请在宿主机用管理员权限执行：")
        log(f"   taskkill /PID {remaining[0]} /F /T")
        return 1
    log(f"✅ 端口 {port} 已释放。")
    return 0


def status(port: int, host: str = "127.0.0.1") -> int:
    pid = read_pid(port)
    holders = pids_on_port(port)
    healthy = health_ok(port, host)

    log(f"端口 {port} | pidfile={pid} | 监听PID={holders} | 健康={healthy}")
    if healthy:
        log("状态: ✅ 运行中且健康")
        return 0
    if holders:
        log("状态: ⚠️ 端口被占用但健康检查未通过（服务可能正在启动或已卡死）")
        return 2
    log("状态: 未运行")
    return 3


def restart(port: int, host: str) -> int:
    log(">>> restart: 先停止，再启动")
    stop(port)
    # 给操作系统一点时间回收端口
    time.sleep(1.0)
    return start(port, host)


# ---------- CLI ----------
def main() -> int:
    parser = argparse.ArgumentParser(description="玄镜 OracleMind AI 服务启停脚本")
    parser.add_argument("action", choices=["start", "stop", "restart", "status"])
    parser.add_argument("--port", type=int, default=int(os.environ.get("AI_SERVER_PORT", DEFAULT_PORT)))
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()

    if args.action == "start":
        return start(args.port, args.host)
    if args.action == "stop":
        return stop(args.port)
    if args.action == "restart":
        return restart(args.port, args.host)
    if args.action == "status":
        return status(args.port)
    return 1


if __name__ == "__main__":
    sys.exit(main())
