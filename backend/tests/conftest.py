"""pytest 配置：把 backend/ 加入 import 路径。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
