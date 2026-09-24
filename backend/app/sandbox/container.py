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
from app.sandbox.repo_config import RepoExecutionConfig, get_repo_config

log = logging.getLogger(__name__)

_WORKDIR = "/workspace"


class ContainerError(RuntimeError):
    pass


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

    async def exec(self, command: str, *, timeout: float | None = None) -> str:
        """容器内执行 shell 命令。setsid 包装 + 超时后二次 exec 杀进程组。"""
        limit = settings.sandbox_command_timeout_seconds if timeout is None else timeout
        loop = asyncio.get_running_loop()
        marker = f"/tmp/.domino-pgid-{uuid.uuid4().hex[:12]}"
        wrapped = (
            f"setsid sh -c 'echo $$ > {marker}; {command}; rc=$?; rm -f {marker}; exit $rc'"
        )
        holder = self._entry.container

        def _run() -> tuple[bytes, bytes]:
            return holder.exec_run(
                cmd=["sh", "-c", wrapped],
                workdir=_WORKDIR,
                demux=True,
            )

        try:
            result = await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=limit + 10)
        except asyncio.TimeoutError:
            # 宿主侧超时：容器内进程组可能还活着 → 另起 exec 击杀（5s 预算）
            await self._kill_group(holder, marker)
            raise TimeoutError(f"命令超时（{limit}s）且进程组已被击杀: {command[:80]}")

        exit_code = result.exit_code
        out = result.output or (b"", b"")
        stdout = (out[0] or b"").decode("utf-8", errors="replace")
        stderr = (out[1] or b"").decode("utf-8", errors="replace")
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
        code = (
            f"test -f {_q(path)} || exit 3; "
            f"file -b --mime-encoding {_q(path)} | grep -qv binary || exit 2; "
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
    """容器内 git 缺失时现场安装（按包管理器探测；已装则零开销跳过）。"""
    probe = await executor.exec("command -v git", timeout=30)
    if probe.strip():
        return
    log.info("git missing in container, installing...")
    out = await executor.exec(
        "(apt-get update && apt-get install -y git) || "
        "(apk add --no-cache git) || "
        "(yum install -y git)",
        timeout=600,
    )
    check = await executor.exec("command -v git", timeout=30)
    if not check.strip():
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

    async def acquire(self, repo: str) -> DockerExecutor:
        """取该仓库的执行器；容器不存在则创建并 clone。"""
        cfg = get_repo_config(repo)
        if cfg is None or not cfg.url:
            raise ContainerError(f"仓库 {repo} 未配置 REPO_{repo.upper()}_URL，无法执行任务")

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
            container = docker.containers.run(
                image,
                command=["tail", "-f", "/dev/null"],
                detach=True,
                cpus=settings.sandbox_cpus,
                mem_limit=settings.sandbox_mem_limit,
                # 端口由任务内部按需起服务；这里不暴露任何端口到公网
                ports={},
                labels={"opendomino": "sandbox", "repo": cfg.name},
            )
            return container

        container = await loop.run_in_executor(None, _run)
        entry = _PoolEntry(container=container, repo_config=cfg, last_used=time.time())

        # clone（容器内，Linux 原生 I/O）；凭据织入 URL，不落盘、不进镜像。
        # 基础镜像（python:3.12-slim 等）可能不带 git：缺失则现场安装（幂等，容器复用时跳过）
        executor = DockerExecutor(entry, cfg.name)
        await _ensure_git(executor)
        await executor.exec(
            f"git clone {_q(cfg.clone_url)} {_WORKDIR} 2>&1",
            timeout=300,
        )
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
