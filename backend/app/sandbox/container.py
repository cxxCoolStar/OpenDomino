"""容器池（ADR-004 详细设计 §3）：按仓库池化复用 + 容器内 clone + 真超时击杀。

- 池化键 = 仓库名；同仓库串行（由 executor.py 队列保证），跨仓库并行；
- 仓库 clone 在容器文件系统 /workspace（Linux 原生 I/O，规避 Windows bind-mount）；
- 保活 = `tail -f /dev/null`，一切操作走 exec_run；
- 真超时三层（fastclaw 同款）：命令包 setsid 成新会话 leader、pid 写 marker 文件、
  超时后另起 exec 读 pgid 执行 kill -KILL -pgid（杀整组防 pytest 子进程漏杀）；
- 后台 sweeper 按 idle TTL 驱逐容器。成果已 push 成分支，驱逐不做快照。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from dataclasses import dataclass

from app.config import settings
from app.sandbox.repo_config import RepoExecutionConfig, resolve_repo_config
from app.sandbox.tools import sanitize

log = logging.getLogger(__name__)

_WORKDIR = "/workspace"


class ContainerError(RuntimeError):
    pass


class ExecError(RuntimeError):
    """容器执行层失败（OCI/chdir/命令缺失），区别于命令自身的非零退出。"""


@dataclass
class _PoolEntry:
    container: object          # docker.models.containers.Container
    repo_config: RepoExecutionConfig
    last_used: float


class DockerExecutor:
    """一个已绑定容器的执行门面（tools.Executor 协议的真实实现）。"""

    def __init__(self, entry: "_PoolEntry", repo: str) -> None:
        self._entry = entry
        self.repo = repo

    async def exec(self, command: str, *, timeout: float | None = None, workdir: str | None = None) -> str:
        """容器内执行 shell 命令。setsid 包装 + 超时后二次 exec 杀进程组。

        workdir 默认 /workspace（初始化阶段可用 workdir="/" 覆盖）。
        错误分层（fastclaw 模式）：执行层失败（OCI 错误、exit 125/126/127）抛 ExecError，
        调用方以异常为判据；命令自身的非零退出正常返回输出。
        """
        limit = settings.sandbox_command_timeout_seconds if timeout is None else timeout
        loop = asyncio.get_running_loop()
        marker = f"/tmp/.domino-pgid-{uuid.uuid4().hex[:12]}"
        # 命令里的单引号会击穿外层 sh -c '...' 包裹（hermes 原子写脚本的 trap '...' 正是此形态），
        # 统一转义为 '\''（POSIX sh 标准写法：结束引号+转义引号+重开引号）
        escaped = command.replace("'", "'\\''")
        wrapped = (
            f"setsid sh -c 'echo $$ > {marker}; {escaped}; rc=$?; rm -f {marker}; exit $rc'"
        )
        holder = self._entry.container
        wd = workdir or _WORKDIR

        def _run() -> tuple[int | None, bytes, bytes]:
            r = holder.exec_run(
                cmd=["sh", "-c", wrapped],
                workdir=wd,
                demux=True,
            )
            out = r.output or (b"", b"")
            return r.exit_code, out[0] or b"", out[1] or b""

        try:
            exit_code, stdout_b, stderr_b = await asyncio.wait_for(
                loop.run_in_executor(None, _run), timeout=limit + 10
            )
        except asyncio.TimeoutError:
            # 宿主侧超时：容器内进程组可能还活着 → 另起 exec 击杀（5s 预算）
            await self._kill_group(holder, marker)
            raise TimeoutError(f"命令超时（{limit}s）且进程组已被击杀: {command[:80]}")

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        # 执行层失败：exit 125/126/127 是 docker 运行时/命令缺失的信号（125=OCI 错误、
        # 126=不可执行、127=命令不存在），不作为模型可见的"命令失败"回填，抛 ExecError
        combined_err = (stdout + stderr).strip()
        if exit_code in (125, 126, 127):
            raise ExecError(f"容器执行层失败 (exit {exit_code}): {combined_err[:300]}")

        text = stdout if not stderr else f"{stdout}\n[stderr]\n{stderr}"
        if exit_code not in (0, None):
            text = f"{text}\n[exit {exit_code}]" if text else f"[exit {exit_code}]"
        return text

    @staticmethod
    async def _kill_group(holder, marker: str) -> None:
        def _kill() -> None:
            holder.exec_run(
                cmd=["sh", "-c",
                     f"pgid=$(cat {marker} 2>/dev/null); "
                     f"[ -n \"$pgid\" ] && kill -KILL -$pgid; rm -f {marker}"],
            )

        try:
            await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, _kill), timeout=5)
        except Exception:  # noqa: BLE001 —— 击杀失败只记日志，不影响主流程超时语义
            log.warning("kill group failed for marker %s", marker)

    async def read_file(self, path: str) -> str:
        if path.endswith((".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz", ".whl", ".pyc")):
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary file refused")
        # 二进制嗅探用 grep -qI（GNU grep 内置，镜像里无需 file 命令）：
        # -I 把二进制文件当无匹配处理 → 检出二进制时 exit 1
        code = (
            f"test -f {_q(path)} || exit 3; "
            f"grep -qI . {_q(path)} || exit 2; "
            f"cat {_q(path)}"
        )
        out = await self.exec(code, timeout=30)
        if "[exit 3]" in out:
            raise FileNotFoundError(path)
        if "[exit 2]" in out:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary file refused")
        return out

    async def write_file(self, path: str, content: str) -> None:
        # stdin 管道写入，绕开 argv 的 NUL/长度限制
        import base64

        encoded = base64.b64encode(content.encode()).decode()
        await self.exec(
            f"mkdir -p $(dirname {_q(path)}) && echo {encoded} | base64 -d > {_q(path)}",
            timeout=60,
        )

    async def list_dir(self, path: str = ".") -> list[dict]:
        out = await self.exec(
            f"ls -la --time-style=long-iso {_q(path)} | tail -n +2", timeout=30
        )
        entries = []
        for line in out.splitlines():
            # long-iso 固定 8 列：perm links owner group size date time name
            parts = line.split(None, 7)
            if len(parts) < 8:
                continue
            name = parts[7]
            entries.append({
                "name": name,
                "type": "dir" if parts[0].startswith("d") else "file",
                "size": int(parts[4]) if parts[4].isdigit() else 0,
            })
        return entries


def _q(path: str) -> str:
    """shell 单引号转义。"""
    return "'" + path.replace("'", "'\\''") + "'"


async def _ensure_git(executor: "DockerExecutor") -> None:
    """容器内 git 缺失时现场安装（按包管理器探测；已装则零开销跳过）。

    探测命令用 `|| echo MISSING` 收尾保证 exit 0——否则 git 缺失时
    `command -v git` 自身 exit 127，会触发 exec 的执行层错误分层。
    """
    probe = await executor.exec("command -v git || echo MISSING", timeout=30)
    if "MISSING" not in probe:
        return
    log.info("git missing in container, installing...")
    # DEBIAN_FRONTEND=noninteractive：apt 安装 debconf 需要 tty，缺它会在非交互 exec 下失败；
    # 三个包管理器分支各自用 `&& ... || true` 链收束为 0/1（apt 失败时 fallback 到 apk/yum），
    # 且不再让"apk/yum not found"的 127 掩盖真实失败码
    out = await executor.exec(
        "export DEBIAN_FRONTEND=noninteractive; "
        "if command -v apt-get >/dev/null 2>&1; then "
        "  apt-get update -qq && apt-get install -y -qq git; "
        "elif command -v apk >/dev/null 2>&1; then "
        "  apk add --no-cache git; "
        "elif command -v yum >/dev/null 2>&1; then "
        "  yum install -y git; "
        "else echo 'no known package manager'; exit 1; fi",
        timeout=600,
    )
    check = await executor.exec("command -v git || echo MISSING", timeout=30)
    if "MISSING" in check:
        raise ContainerError(f"容器内 git 安装失败: {out[-500:]}")


class ContainerPool:
    """按仓库池化容器。entry lifecycle：懒创建 → 复用 → idle 驱逐。"""

    def __init__(self) -> None:
        self._pool: dict[str, _PoolEntry] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task | None = None
        self._docker = None      # 惰性导入 docker SDK

    def _client(self):
        if self._docker is None:
            import docker

            self._docker = docker.from_env()
        return self._docker

    # ---- 获取/归还 ----

    async def acquire(self, repo: str, *, chat_id: str = "", memory=None) -> DockerExecutor:
        """取该仓库的执行器；容器不存在则创建并 clone。

        池化键 = repo 名（同仓库跨群共享容器池——M:N 绑定下 clone 只此一份）。
        配置解析：L2 repo_bindings 优先，.env REPO_* 兜底。
        """
        cfg = await resolve_repo_config(repo, chat_id=chat_id, memory=memory)
        if cfg is None or not cfg.url:
            raise ContainerError(
                f"仓库 {repo} 未注册：请在群里发「绑定仓库 {repo}: <仓库URL>」完成绑定"
            )

        async with self._lock:
            entry = self._pool.get(repo)
            if entry is None:
                entry = await self._create(cfg)
                self._pool[repo] = entry
            entry.last_used = time.time()
        return DockerExecutor(entry, repo)

    async def release(self, repo: str, executor: DockerExecutor) -> None:
        """任务结束归还容器（工作区在下次 acquire 时重置）。"""
        async with self._lock:
            entry = self._pool.get(repo)
            if entry is not None:
                entry.last_used = time.time()

    # ---- 创建 ----

    async def _create(self, cfg: RepoExecutionConfig) -> _PoolEntry:
        image = cfg.image or settings.sandbox_docker_image
        log.info("creating container for repo=%s image=%s", cfg.name, image)
        loop = asyncio.get_running_loop()

        def _run() -> object:
            docker = self._client()
            # 镜像 ENTRYPOINT=[sh]：Cmd 第一个元素被当脚本文件名（"cannot open tail"），
            # 容器创建即退出 → 后续 exec 全 409。必须传 sh 的参数形式 [-c, "命令"]。
            container = docker.containers.run(
                image,
                command=["-c", "tail -f /dev/null"],
                detach=True,
                # docker SDK 的 CPU 限额走 nano_cpus（1 核 = 1e9），非 --cpus
                nano_cpus=int(settings.sandbox_cpus * 1e9),
                mem_limit=settings.sandbox_mem_limit,
                # 端口由任务内部按需起服务；这里不暴露任何端口到公网
                ports={},
                labels={"opendomino": "sandbox", "repo": cfg.name},
            )
            return container

        container = await loop.run_in_executor(None, _run)
        entry = _PoolEntry(container=container, repo_config=cfg, last_used=time.time())

        # 初始化序列（顺序敏感）：所有 exec 默认 workdir=/workspace，而 clone 的目标正是
        # /workspace——容器刚创建时它不存在，先在 root 下 mkdir 兜底（fastclaw 用 bind-mount
        # 物理保证同一点；容器内 clone 方案下靠这条初始化命令 + 后续就绪校验）。
        executor = DockerExecutor(entry, cfg.name)
        await executor.exec(f"mkdir -p {_WORKDIR}", timeout=60, workdir="/")

        # 基础镜像（python:3.12-slim 等）可能不带 git：缺失则现场安装（幂等，容器复用时跳过）
        await _ensure_git(executor)
        # clone 失败不能静默：exec 对命令非零退出只回不抛，这里用哨兵捕获真实
        # 失败原因（认证失败/仓库不存在/网络不通），脱敏后抛出——否则群里只看
        # 得到后续 probe 的 NOT_A_REPO，掩盖根因
        clone_out = await executor.exec(
            f"git clone {_q(cfg.clone_url)} {_WORKDIR} 2>&1 || echo CLONE_FAILED",
            timeout=300,
        )
        if "CLONE_FAILED" in clone_out:
            raise ContainerError(
                "clone 失败: " + sanitize(clone_out.replace("CLONE_FAILED", "").strip()[-300:])
            )
        # 就绪校验：clone 失败绝不静默——工作区必须真的就绪（fastclaw StatusCrashed 模式）。
        # git 此刻必已安装（_ensure_git 保证），rev-parse 失败会以 exit 非0 正常返回
        probe = await executor.exec("git rev-parse --is-inside-work-tree || echo NOT_A_REPO", timeout=60)
        if "true" not in probe:
            raise ContainerError(f"clone 后工作区未就绪（probe={probe.strip()[:120]}）")
        if cfg.setup_cmd:
            await executor.exec(cfg.setup_cmd, timeout=900)
        return entry

    # ---- 工作区重置（每次任务领取后调用）----

    async def reset_workspace(self, repo: str, executor: DockerExecutor, base_ref: str) -> str:
        """重置到基线并清未跟踪文件；返回当前 HEAD sha 作为 base_ref。"""
        if not base_ref:
            base_ref = (await executor.exec("git rev-parse HEAD")).strip()
        await executor.exec(
            f"git fetch origin && git reset --hard {_q(base_ref)} && git clean -fdx",
            timeout=300,
        )
        return base_ref

    # ---- sweeper ----

    def start_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop(), name="sandbox-sweeper")

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.sandbox_sweep_interval_seconds)
            try:
                await self._evict_idle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("sweeper round failed")

    async def _evict_idle(self) -> None:
        cutoff = time.time() - settings.sandbox_container_idle_ttl_min * 60
        async with self._lock:
            expired = [r for r, e in self._pool.items() if e.last_used < cutoff]
            for repo in expired:
                entry = self._pool.pop(repo)
                log.info("evicting idle container repo=%s", repo)
                await self._destroy(entry)

    async def _destroy(self, entry: _PoolEntry) -> None:
        def _rm() -> None:
            entry.container.remove(force=True)

        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _rm), timeout=30
            )
        except Exception:  # noqa: BLE001
            log.exception("container destroy failed")

    async def close(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
        async with self._lock:
            entries = list(self._pool.values())
            self._pool.clear()
        for entry in entries:
            await self._destroy(entry)
