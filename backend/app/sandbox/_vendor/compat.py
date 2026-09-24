"""hermes 延迟导入小工具的 OpenDomino 等价实现（vendor 支撑层）。

file_operations.py 里若干延迟导入（tools.environments.local / tools.tool_output_limits /
tools.ansi_strip）在 vendor 场景下的轻量替身。行为对齐 hermes 原实现，剔除配置系统依赖。
"""
import platform
import re

IS_WINDOWS = platform.system() == "Windows"

# hermes tool_output_limits 的默认值（hermes config 未启用时即用这些）
MAX_BYTES = 50_000
MAX_LINES = 2000
MAX_LINE_LENGTH = 2000


def _bash_safe_path(path: str) -> str:
    """Git Bash 安全路径（hermes local.py 同名函数）；非 Windows 原样返回。"""
    if not IS_WINDOWS or not path:
        return path
    # MSYS 形态归一：C:/x 或 C:\x → /c/x
    path = re.sub(r"^([A-Za-z]):[/\\]", lambda m: f"/{m.group(1).lower()}/", path)
    return path.replace("\\", "/")


def _msys_to_windows_path(cwd: str) -> str:
    return cwd  # 沙箱内路径恒为 POSIX 形态，无需转换


def get_max_lines() -> int:
    return MAX_LINES


def get_max_line_length() -> int:
    return MAX_LINE_LENGTH


def get_max_bytes() -> int:
    return MAX_BYTES
