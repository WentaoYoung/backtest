import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(PROJECT_ROOT, "results")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
PARQUET_DIR = os.path.join(DATA_DIR, "parquet_loaded")
# 与「分区.py」导出目录一致；可用环境变量 FACTORS_HIVE_DIR 覆盖
FACTORS_HIVE_DIR = os.environ.get("FACTORS_HIVE_DIR", os.path.join(PROJECT_ROOT, "factors_hive"))


def get_server_port(default: int = 5000) -> int:
    """读取服务端口，非法值时回退默认值。"""
    try:
        return int(os.environ.get("PORT", default))
    except (TypeError, ValueError):
        return default
