# -*- coding: utf-8 -*-
"""
量化回测结果展示 Web 应用
使用 Flask + 现代化前端展示回测结果
支持动态参数输入和回测执行
"""

from flask import Flask, render_template, jsonify, send_from_directory, request, Response
import pandas as pd
import numpy as np
import os
import sys
import json
import time  # 新增：用于计算回测耗时
import threading
from jinja2 import TemplateNotFound

# 设置控制台编码为UTF-8（解决Windows下Unicode字符显示问题）
if sys.platform == 'win32':
    try:
        import io

        if hasattr(sys.stdout, 'buffer'):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    except:
        pass

import builtins

_original_print = builtins.print

from flask import jsonify



def print(*args, **kwargs):
    """自动刷新输出的 print 函数确保 PyCharm 控制台实时显示"""
    kwargs['flush'] = True
    _original_print(*args, **kwargs)

# 添加项目根目录到路径
_PROJECT_ROOT_BOOTSTRAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT_BOOTSTRAP not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT_BOOTSTRAP)

from web.config import PROJECT_ROOT, RESULTS_DIR, DATA_DIR, PARQUET_DIR, FACTORS_HIVE_DIR, get_server_port


from backtest.factor_analyzer import FactorAnalyzer
from backtest.backtest_engine import BacktestEngine
from backtest.profiler import SectionTimer, get_profiler, reset_profiler
from backtest.stream_logger import stream_logger, ThreadAwareStdout
from web.services.factor_repository import FactorRepository
from web.services.factor_data_gateway import FactorDataGateway
from web.services.backtest_service import execute_database_backtest, execute_csv_backtest
from web.services.correlation_service import execute_factor_correlation
try:
    from web.correlation_api import correlation_bp, set_library_cache, set_price_df
except ImportError:
    try:
        from multi.correlation_api import correlation_bp, set_library_cache, set_price_df
    except ImportError:
        correlation_bp = None

        def set_library_cache(_cache):
            return None

        def set_price_df(_price_df):
            return None

try:
    from web.multi_factor_api import multi_factor_bp, init_shared_data
except ImportError:
    try:
        from multi.multi_factor_api import multi_factor_bp, init_shared_data
    except ImportError:
        multi_factor_bp = None

        def init_shared_data(_price_df, _mkt_val_df):
            return None

# 与 app.py 一致：拦截 stdout，使「已注册线程」内的 print 经 StreamLogger 写到真实终端。
# 否则数据库加载等在子线程里执行时，Windows/Cursor 下常出现控制台无输出。
_utf8_stdout = sys.stdout
sys.stdout = ThreadAwareStdout(terminal=_utf8_stdout)

# 因子库数据库连接
from data.date_params import coerce_yyyy_mm_dd
from data.db_connector import (
    get_factor_database,
    test_database_connection,
    detect_new_factor_tables as detect_new_db_tables
)

# 便捷函数 - 检测新增因子表
def detect_new_factor_tables(force_rescan=False):
    """检测新增的因子表（包装函数）"""
    from data.db_connector import detect_new_factor_tables as _detect
    return _detect(force_rescan=force_rescan)

app = Flask(__name__,
            template_folder='templates',
            static_folder='static')

if correlation_bp is not None:
    app.register_blueprint(correlation_bp)

if multi_factor_bp is not None:
    app.register_blueprint(multi_factor_bp)

_factor_repository = FactorRepository(PARQUET_DIR, FACTORS_HIVE_DIR)
_factor_data_gateway = FactorDataGateway(_factor_repository, get_factor_database)

# 缓存数据和分析器
_cached_data = None
_cached_analyzer = None
_cached_db_data = {}  # 缓存从数据库加载的因子数据
_last_backtest_data = None  # 缓存最近一次回测的数据（用于相关性分析）
_last_backtest_params = None  # 缓存最近一次回测的参数（日期范围等）
_price_cache = None  # 缓存价格数据

# 回测进度缓存（key: request_id）
_progress_lock = threading.Lock()
_progress_store = {}
_stage_history = {}  # 各阶段历史耗时（秒）
_stage_data_history = {}  # 新增：各阶段历史处理的数据量（条数）

def _calc_eta_by_data(current: int, total: int, elapsed: float) -> int:
    """
    根据实际处理的数据量计算ETA（更准确）
    current: 已处理数量
    total: 总数量
    elapsed: 已用时间（秒）
    """
    if total <= 0 or current <= 0:
        return None

    # 计算速度：条/秒
    speed = current / elapsed if elapsed > 0 else 0

    # 剩余数量
    remaining = total - current

    # 如果速度太慢（<1条/秒），不信任速度，用历史平均值
    if speed < 1 and elapsed > 5:
        # 用历史数据估算
        return None

    # 预估剩余时间
    if speed > 0:
        eta = remaining / speed
        return int(max(eta, 1))

    return None

def report_stage_progress(request_id: str, stage: str, current: int, total: int, detail: str = ""):
    """
    报告实际进度（基于数据量的更准确进度）
    current: 已处理数量
    total: 总数量
    """
    if not request_id:
        return

    now = time.time()
    with _progress_lock:
        existing = _progress_store.get(request_id, {})
        stage_start_ts = existing.get("stage_start_ts", now)
        elapsed = max(0.1, now - stage_start_ts)

        # 计算进度百分比
        if total > 0:
            progress = int((current / total) * 100)
            progress = max(1, min(99, progress))  # 保留1-99之间，避免100%
        else:
            progress = None

        # 用数据量计算ETA
        eta_seconds = _calc_eta_by_data(current, total, elapsed)

        # 构建详情文本
        if detail:
            detail_text = f"{detail} ({current}/{total})"
        else:
            detail_text = f"已处理 {current}/{total} 条"

        # 更新存储
        _progress_store[request_id] = {
            "request_id": request_id,
            "status": "running",
            "progress": progress if progress else existing.get("progress", 0),
            "stage": stage,
            "detail": detail_text,
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": eta_seconds,
            "updated_at": now,
            "start_ts": existing.get("start_ts", now),
            "stage_start_ts": stage_start_ts,
            # 新增：记录实际数据量进度
            "data_progress": {
                "current": current,
                "total": total,
                "percent": progress
            }
        }

# 阶段默认耗时（首次运行时用于估算）
_stage_default_seconds = {
    "初始化": 1.5,
    "加载因子数据": 18.0,  # 数据库模式用
    "解析 CSV": 30.0,  # CSV模式用 (补上)
    "数据预处理": 5.0,  # (补上)
    "初始化回测引擎": 2.0,
    "回测计算": 20.0,  # 统一改成这个名字
    "因子分析": 14.0,
    "基准对齐": 4.0,  # 统一改成这个名字
    "生成结果": 2.0  # (补上)
}

_stage_order = [
    "初始化",
    "加载因子数据",
    "解析 CSV",
    "数据预处理",
    "初始化回测引擎",
    "回测计算",
    "因子分析",
    "基准对齐",
    "生成结果"
]

# 数据源配置
DATA_SOURCE = "local"  # "local" 或 "database"

def get_cached_data():
    """获取缓存的数据"""
    global _cached_data
    if _cached_data is None:
        _cached_data = load_base_data()
    return _cached_data

def get_last_backtest_data():
    """获取最近一次回测的数据（用于相关性分析）"""
    global _last_backtest_data
    return _last_backtest_data

def get_last_backtest_params():
    """获取最近一次回测的参数"""
    global _last_backtest_params
    return _last_backtest_params

def set_last_backtest(data, params):
    """设置最近一次回测的数据和参数"""
    global _last_backtest_data, _last_backtest_params
    _last_backtest_data = data
    _last_backtest_params = params
    print(f"[OK] Already cached: {len(data)} records, date: {params.get('start_date')} ~ {params.get('end_date')}")

# 在文件顶部定义
# 在文件顶部定义，根据你的实际回测情况调整
_stage_default_seconds = {
    "加载因子数据": 120,  # 加载因子数据（实际约2分钟）
    "加载价格数据": 8,  # 加载价格数据（实际约8秒）
    "数据预处理": 5,  # 数据预处理（实际约5秒）
    "回测计算": 15,  # 回测计算（实际约15秒）
    "因子分析": 25,  # 因子分析（实际约25秒）
    "基准对齐": 3,  # 加载基准数据（实际约3秒）
    "生成图表": 2  # 生成图表（实际约2秒）
}

# 添加根据数据量调整的函数
def update_stage_times_by_data(stage_name: str, data_count: int):
    """根据实际数据量动态调整各阶段预估时间"""
    global _stage_default_seconds

    # 基础时间（100万条数据）
    base_times = {
        "加载因子数据": 60,
        "回测计算": 10,
        "因子分析": 15,
    }

    multiplier = data_count / 1000000

    if stage_name == "加载因子数据":
        _stage_default_seconds["加载因子数据"] = base_times["加载因子数据"] * multiplier
    elif stage_name == "回测计算":
        _stage_default_seconds["回测计算"] = base_times["回测计算"] * multiplier

    print(f"[进度] 数据量 {data_count} 条，更新 {stage_name} 预估时间")

def _avg_stage_seconds(stage_name: str) -> float:
    """获取某个阶段的平均耗时（秒）"""
    hist = _stage_history.get(stage_name, [])
    if hist:
        # 取最近3次平均值，避免极端值影响
        recent = hist[-3:] if len(hist) > 3 else hist
        return sum(recent) / len(recent)
    else:
        return _stage_default_seconds.get(stage_name, 30)

def _estimate_eta_seconds(stage_name: str, stage_elapsed: float) -> int:
    """简化版：基于整体60秒预估剩余时间"""
    # 假设整体还需要60秒，根据阶段名称返回保守估计
    if "加载因子数据" in stage_name:
        return 40
    elif "数据预处理" in stage_name:
        return 20
    elif "回测计算" in stage_name:
        return 15
    elif "因子分析" in stage_name:
        return 10
    else:
        return 20

def _set_progress(request_id: str, progress: int, stage: str, status: str = "running", detail: str = "",
                  data_current: int = None, data_total: int = None):
    """更新任务进度。progress 范围 0-100。"""
    if not request_id:
        return
    now = time.time()
    with _progress_lock:
        existing = _progress_store.get(request_id, {})
        # 🛑 【防御覆盖】如果已经结束了，拒绝任何试图把它改回 running 的请求
        if existing.get("status") in ("completed", "error") and status == "running":
            return
    start_ts = existing.get("start_ts", now)
    prev_stage = existing.get("stage")
    stage_start_ts = existing.get("stage_start_ts", now)
    progress = max(0, min(100, int(progress)))
    elapsed = max(0.0, now - start_ts)

    # 1. 阶段切换时，记录上一阶段真实耗时到历史
    data_progress = None  # 默认置空
    if prev_stage and prev_stage != stage and status == "running":
        prev_elapsed = max(0.0, now - stage_start_ts)
        if prev_stage in _stage_default_seconds:
            hist = _stage_history.setdefault(prev_stage, [])
            hist.append(prev_elapsed)
            if len(hist) > 10:
                del hist[0]
        stage_start_ts = now
        # 【关键修复】阶段切换时必须清空数据进度，防止上一阶段的数据量干扰下一阶段的ETA计算
        data_progress = None
    elif existing.get("data_progress"):
        # 同一阶段内，保留之前的数据进度
        data_progress = existing["data_progress"]

    # 2. 任务结束/报错时，记录当前阶段真实耗时
    if status in ("completed", "error") and stage in _stage_default_seconds:
        final_stage_elapsed = max(0.0, now - stage_start_ts)
        hist = _stage_history.setdefault(stage, [])
        hist.append(final_stage_elapsed)
        if len(hist) > 10:
            del hist[0]

    # 3. 强制修正：只要状态是完成，进度必须死磕到 100%
    if status == "completed":
        progress = 100

    # 4. 【核心修复】计算 ETA (剩余时间)
    eta_seconds = None
    if status == "completed":
        eta_seconds = 0
    elif status == "running" and progress > 0:
        # 优先级A：基于真实数据量计算（最准，但只有传了 data_current 时才生效）
        if data_current is not None and data_total is not None and data_total > 0:
            stage_elapsed = max(0.1, now - stage_start_ts)
            eta_seconds = _calc_eta_by_data(data_current, data_total, stage_elapsed)

        # 优先级B：基于全局进度比例外推
        if eta_seconds is None:
            completed_ratio = progress / 100.0
            if completed_ratio > 0.05:
                total_estimate = elapsed / completed_ratio
                raw_eta = total_estimate - elapsed

                # 【解决卡1秒的核心】如果算出的剩余时间极小，说明实际速度远超预期，外推失效！
                if raw_eta < 2:
                    # 放弃外推，改用“后续阶段的预期时间之和”来兜底
                    remaining_expected = 0
                    try:
                        idx = _stage_order.index(stage)
                        # 累加后续所有阶段的默认时间
                        for s in _stage_order[idx + 1:]:
                            remaining_expected += _stage_default_seconds.get(s, 0)
                        # 加上当前阶段如果还没跑完的预估剩余
                        stage_expected = _stage_default_seconds.get(stage, 0)
                        stage_elapsed = max(0.1, now - stage_start_ts)
                        remaining_expected += max(0, stage_expected - stage_elapsed)
                    except ValueError:
                        remaining_expected = 30  # 找不到阶段名称时的极端兜底

                    eta_seconds = max(1, int(remaining_expected))
                else:
                    # 正常情况：使用外推时间
                    eta_seconds = max(1, int(raw_eta))
            else:
                # 刚启动(<5%)：用所有阶段预设时间总和兜底
                total_expected = sum(_stage_default_seconds.values())
                eta_seconds = max(1, int(total_expected - elapsed))

    # 5. 构造最终的数据进度 (覆盖前面的默认值 None)
    if data_current is not None and data_total is not None and data_total > 0:
        data_progress = {
            "current": data_current,
            "total": data_total,
            "percent": int((data_current / data_total) * 100)
        }

    # 6. 写入存储
    _progress_store[request_id] = {
        "request_id": request_id,
        "status": status,
        "progress": progress,
        "stage": stage,
        "detail": detail,
        "elapsed_seconds": round(elapsed, 1),
        "eta_seconds": eta_seconds,
        "updated_at": now,
        "start_ts": start_ts,
        "stage_start_ts": stage_start_ts,
        "data_progress": data_progress
    }

def get_cached_analyzer():
    """获取缓存的因子分析器"""
    global _cached_analyzer
    if _cached_analyzer is None:
        data = get_cached_data()
        if data is not None:
            _cached_analyzer = FactorAnalyzer(
                data,
                factor_col="factor",
                price_col="adj_open",
                date_col="trade_dt",
                ticker_col="ticker"
            )
    return _cached_analyzer


def load_factor_from_database(factor_name: str, start_date: str = None, end_date: str = None, table_name: str = None):
    global _cached_db_data, _price_cache

    cache_key = f"{factor_name}_{start_date}_{end_date}"
    if cache_key in _cached_db_data:
        print(f"使用缓存的因子数据: {factor_name}")
        return _cached_db_data[cache_key].copy()

    try:
        timings = {}

        # 1. 统一网关读取因子（DuckDB/parquet 优先，失败回退数据库）
        t0 = time.time()
        factor_df, source = _factor_data_gateway.load_single_factor(
            factor_name=factor_name,
            start_date=start_date,
            end_date=end_date,
            table_name=table_name,
        )
        if source == "database":
            print("    [因子连接方式] MySQL数据库直连")
        else:
            print("    [因子连接方式] DuckDB读取本地Parquet")
        timings['查询因子'] = time.time() - t0
        print(f"    [TIME]️ 查询因子({source}): {timings['查询因子']:.3f}s")

        if factor_df.empty:
            return None

        # 2. 获取因子数据的日期范围
        factor_min_date = factor_df['trade_dt'].min()
        factor_max_date = factor_df['trade_dt'].max()
        unique_tickers = factor_df['ticker'].unique()

        print(f"    因子数据日期范围: {factor_min_date.date()} ~ {factor_max_date.date()}")
        print(f"    涉及股票数量: {len(unique_tickers)}")

        # ========== 加载开盘价数据（修改点） ==========
        t0 = time.time()

        if _price_cache is None:
            print("    [FILE] 首次加载开盘价数据...")
            # 修改 1：使用 adjopen_wide.csv 文件
            from data.data_io import resolve_data_csv_path, safe_read_csv

            price_path = resolve_data_csv_path(DATA_DIR, "adjopen_wide.csv")
            if price_path:
                price_wide = safe_read_csv(price_path)
                first_col = price_wide.columns[0]
                if first_col in ['', 'Unnamed: 0', 'trade_dt', 'date']:
                    price_wide = price_wide.rename(columns={first_col: 'trade_dt'})
                price_wide['trade_dt'] = pd.to_datetime(price_wide['trade_dt'])

                # 只保留需要的日期范围
                price_wide = price_wide[
                    (price_wide['trade_dt'] >= factor_min_date) &
                    (price_wide['trade_dt'] <= factor_max_date)
                    ]

                # 转换为长表，价格列名改为 adj_open
                if 'adj_open' in price_wide.columns:
                    price_wide = price_wide.rename(columns={'adj_open': '_original_adj_open'})
                price_df = price_wide.melt(
                    id_vars=['trade_dt'],
                    var_name='ticker',
                    value_name='adj_open'          # 修改 2：列名改为 adj_open
                )
                price_df['ticker'] = price_df['ticker'].str.upper()

                # 只保留需要的股票
                price_df = price_df[price_df['ticker'].isin(unique_tickers)]

                _price_cache = price_df
                print(f"    ✅ 开盘价数据加载完成: {len(_price_cache)} 条")
            else:
                print("本地开盘价数据不存在，请先运行 `python main.py --action cache` 生成 adjopen_wide.csv")
                return None
        else:
            # 从缓存中过滤，注意列名是 adj_open
            price_df = _price_cache[
                (_price_cache['trade_dt'] >= factor_min_date) &
                (_price_cache['trade_dt'] <= factor_max_date) &
                (_price_cache['ticker'].isin(unique_tickers))
                ]

        timings['加载价格'] = time.time() - t0
        print(f"    [TIME]️ 加载开盘价: {timings['加载价格']:.3f}s (过滤后 {len(price_df)} 条)")

        # 3. 合并数据
        t0 = time.time()
        factor_df = factor_df.rename(columns={'factor_value': 'factor'})

        # 使用索引加速合并
        factor_df_idx = factor_df.set_index(['ticker', 'trade_dt'])
        price_df_idx = price_df.set_index(['ticker', 'trade_dt'])

        merged = factor_df_idx.join(price_df_idx, how='inner').reset_index()
        timings['合并数据'] = time.time() - t0
        print(f"    [TIME]️ 合并数据: {timings['合并数据']:.3f}s")

        # 4. 清理（注意列名改为 adj_open）
        t0 = time.time()
        merged = merged.dropna(subset=['factor', 'adj_open'])   # 修改 3：检查 adj_open 列
        merged['market_value'] = merged['factor']
        timings['清理'] = time.time() - t0

        total_time = sum(timings.values())

        print(f"✅ 从数据库加载因子 {factor_name}: {len(merged)} 条记录, {merged['ticker'].nunique()} 只股票 (使用开盘价)")
        print(f"  [TIME]️ 加载耗时明细:")
        for name, elapsed in timings.items():
            print(f"     {name}: {elapsed:.3f}s")
        print(f"     总计: {total_time:.3f}s")

        _cached_db_data[cache_key] = merged.copy()
        return merged

    except Exception as e:
        import traceback
        print(f"从数据库加载因子失败: {e}")
        traceback.print_exc()
        return None

def load_benchmark_from_wind(code: str, start_date: str, end_date: str) -> pd.Series:
    """
    从 MySQL index_data 表获取基准净值序列（沪深300等指数）
    连接信息已在代码中固定
    """
    sd = coerce_yyyy_mm_dd(start_date)
    ed = coerce_yyyy_mm_dd(end_date)
    if not sd or not ed:
        return pd.Series(dtype=float)

    try:
        from sqlalchemy import create_engine
        import pandas as pd

        # 使用你提供的 MySQL 连接信息
        engine = create_engine(
            "mysql+pymysql://intern:intern@10.129.67.103:3306/intern?charset=utf8mb4",
            connect_args={"connect_timeout": 10}
        )

        s = sd.replace("-", "")
        e = ed.replace("-", "")

        # 直接查询 index_data 表
        query = f"""
            SELECT trade_dt, close
            FROM index_data
            WHERE s_info_windcode = '{code}'
              AND trade_dt >= '{s}'
              AND trade_dt <= '{e}'
            ORDER BY trade_dt
        """

        df = pd.read_sql(query, engine)
        engine.dispose()

        if df.empty:
            print(f"[WARN]️ MySQL 中未找到基准数据: {code}")
            return pd.Series(dtype=float)

        # 转换日期格式
        df["trade_dt"] = pd.to_datetime(df["trade_dt"], format="%Y%m%d")
        df = df.sort_values("trade_dt")
        df["pct_change"] = df["close"].pct_change().fillna(0)
        nav = (1 + df["pct_change"]).cumprod()
        nav.index = df["trade_dt"]

        print(f"✅ 从 MySQL 加载基准数据成功: {code}, {len(nav)} 条")
        return nav

    except Exception as e:
        print(f"[WARN]️ 基准数据加载失败: {e}")
        import traceback
        traceback.print_exc()
        return pd.Series(dtype=float)

def load_benchmark_data(benchmark_code: str, start_date: str, end_date: str) -> pd.Series:
    """
    从数据库或本地数据加载基准指数数据
    
    参数:
        benchmark_code: 基准指数代码 (如 '000300.SH')
        start_date: 开始日期
        end_date: 结束日期
    
    返回:
        Series: 日期索引的净值序列（从1.0开始）
    """
    print(f"[基准数据] 开始加载: {benchmark_code}, {start_date} ~ {end_date}")

    # 0. 优先尝试 Wind/Oracle
    nav_from_wind = load_benchmark_from_wind(benchmark_code, start_date, end_date)
    if not nav_from_wind.empty:
        return nav_from_wind

    # 1. 尝试从本地CSV加载
    try:
        # 尝试加载 data/{code}.csv 或 data/benchmark_{code}.csv
        possible_paths = [
            os.path.join(DATA_DIR, f"{benchmark_code}.csv"),
            os.path.join(DATA_DIR, f"benchmark_{benchmark_code}.csv")
        ]

        benchmark_path = None
        for path in possible_paths:
            if os.path.exists(path):
                benchmark_path = path
                break

        if benchmark_path:
            print(f"从本地文件加载基准数据: {benchmark_path}")
            bm_df = pd.read_csv(benchmark_path)

            # 处理日期列
            first_col = bm_df.columns[0]
            if first_col in ['', 'Unnamed: 0', 'trade_dt', 'date']:
                bm_df = bm_df.rename(columns={first_col: 'trade_dt'})
            bm_df['trade_dt'] = pd.to_datetime(bm_df['trade_dt'])

            # 查找价格列 (close, adj_close, etc.)
            price_col = None
            for col in ['adj_close', 'close', 'S_DQ_CLOSE', 'S_DQ_ADJCLOSE']:
                if col in bm_df.columns:
                    price_col = col
                    break

            if price_col:
                # 筛选日期
                if start_date:
                    bm_df = bm_df[bm_df['trade_dt'] >= pd.to_datetime(start_date)]
                if end_date:
                    bm_df = bm_df[bm_df['trade_dt'] <= pd.to_datetime(end_date)]

                # 计算净值
                bm_df = bm_df.sort_values('trade_dt')
                bm_df['pct_change'] = bm_df[price_col].pct_change().fillna(0)
                nav_series = (1 + bm_df['pct_change']).cumprod()
                nav_series.index = bm_df['trade_dt']

                print(f"[OK] 加载基准指数数据: {len(nav_series)} 个交易日")
                return nav_series
    except Exception as e:
        print(f"[WARN]️ 加载本地基准数据失败: {e}")

    # 2. 占位实现：返回等权组合作为基准
    try:
        print("未找到特定基准数据，使用市场等权组合作为基准...")
        price_path = os.path.join(DATA_DIR, 'adjclose_wide.csv')
        if os.path.exists(price_path):
            price_wide = pd.read_csv(price_path)
            first_col = price_wide.columns[0]
            if first_col in ['', 'Unnamed: 0', 'trade_dt', 'date']:
                price_wide = price_wide.rename(columns={first_col: 'trade_dt'})

            price_wide['trade_dt'] = pd.to_datetime(price_wide['trade_dt'])

            # 筛选日期范围
            if start_date:
                price_wide = price_wide[price_wide['trade_dt'] >= pd.to_datetime(start_date)]
            if end_date:
                price_wide = price_wide[price_wide['trade_dt'] <= pd.to_datetime(end_date)]

            # 计算等权组合收益（所有股票的平均收益）
            price_cols = [c for c in price_wide.columns if c != 'trade_dt']
            # 使用 fill_method=None 避免未来版本弃用警告，并保持缺失为 NaN
            price_wide[price_cols] = price_wide[price_cols].pct_change(fill_method=None)

            # 每日等权收益
            daily_returns = price_wide[price_cols].mean(axis=1)
            daily_returns.index = price_wide['trade_dt']

            # 计算累计净值
            nav_series = (1 + daily_returns).cumprod()
            nav_series = nav_series.fillna(1.0)  # 第一行填充为1.0

            print(f"[OK] 加载基准指数数据（等权组合）: {len(nav_series)} 个交易日")
            return nav_series
    except Exception as e:
        print(f"[WARN]️ 加载基准指数数据失败: {e}")

    # 如果失败，返回空Series
    return pd.Series(dtype=float)

def load_base_data():
    """加载基础数据"""
    try:
        # 加载因子数据 (宽表格式: trade_dt, stock1, stock2, ...)
        from data.data_io import resolve_data_csv_path, safe_read_csv

        factor_path = resolve_data_csv_path(DATA_DIR, "market_value.csv")
        if not factor_path:
            print("因子数据文件不存在: market_value.csv 或 market_value.csv.gz")
            return None

        factor_wide = safe_read_csv(factor_path)

        # 检测并处理日期列（可能是第一列，名称可能为空或 'trade_dt' 或 'Unnamed: 0'）
        first_col = factor_wide.columns[0]
        if first_col in ['', 'Unnamed: 0', 'trade_dt', 'date']:
            factor_wide = factor_wide.rename(columns={first_col: 'trade_dt'})

        factor_wide['trade_dt'] = pd.to_datetime(factor_wide['trade_dt'])

        # 转换为长表格式
        # 如果存在名为'factor'的列，重命名以避免melt冲突
        if 'factor' in factor_wide.columns:
            factor_wide = factor_wide.rename(columns={'factor': '_original_factor'})
        factor_long = factor_wide.melt(
            id_vars=['trade_dt'],
            var_name='ticker',
            value_name='factor'
        )

        # 统一股票代码为大写
        factor_long['ticker'] = factor_long['ticker'].str.upper()

        # 加载价格数据 (宽表格式)
        price_path = resolve_data_csv_path(DATA_DIR, "adjclose_wide.csv")
        if not price_path:
            print("价格数据文件不存在: adjclose_wide.csv")
            return None

        price_wide = safe_read_csv(price_path)

        # 处理日期列
        first_col = price_wide.columns[0]
        if first_col in ['', 'Unnamed: 0', 'trade_dt', 'date']:
            price_wide = price_wide.rename(columns={first_col: 'trade_dt'})

        price_wide['trade_dt'] = pd.to_datetime(price_wide['trade_dt'])

        # 转换为长表格式
        # 如果存在名为'adj_close'的列，重命名以避免melt冲突
        if 'adj_close' in price_wide.columns:
            price_wide = price_wide.rename(columns={'adj_close': '_original_adj_close'})
        price_long = price_wide.melt(
            id_vars=['trade_dt'],
            var_name='ticker',
            value_name='adj_close'
        )

        # 统一股票代码为大写
        price_long['ticker'] = price_long['ticker'].str.upper()

        # 合并数据
        data = pd.merge(factor_long, price_long, on=['trade_dt', 'ticker'], how='inner')

        # 删除缺失值
        data = data.dropna(subset=['factor', 'adj_close'])

        # 添加市值列（用于市值加权）
        data['market_value'] = data['factor']

        print(f"数据加载成功: {len(data)} 条记录, {data['ticker'].nunique()} 只股票")

        # 注入相关性分析所需缓存（若 correlation_api 可用，价格口径使用开盘价宽表）。
        try:
            try:
                from backtest.factor_correlation import LibraryCache
            except ImportError:
                from multi.factor_correlation import LibraryCache

            cache = LibraryCache()
            cache.load(get_factor_database())
            set_library_cache(cache)
            open_path = resolve_data_csv_path(DATA_DIR, "adjopen_wide.csv")
            if open_path:
                open_wide = safe_read_csv(open_path)
                first_col = open_wide.columns[0]
                if first_col in ['', 'Unnamed: 0', 'trade_dt', 'date']:
                    open_wide = open_wide.rename(columns={first_col: 'trade_dt'})
                open_wide['trade_dt'] = pd.to_datetime(open_wide['trade_dt'])
                open_wide = open_wide.set_index('trade_dt').sort_index()
                open_wide.columns = open_wide.columns.map(lambda x: str(x).upper())
                set_price_df(open_wide)
                print("✅ 已注入相关性分析缓存（LibraryCache + adjopen_wide）")
            else:
                print(f"[WARN] 相关性分析开盘价文件不存在: {open_path}")
        except Exception as corr_e:
            print(f"[WARN] 相关性缓存注入失败: {corr_e}")

        # 多因子批量回测：价格宽表必须与单因子库表回测一致，使用 adjopen_wide（BacktestEngine 口径为开盘价收益）
        try:
            if multi_factor_bp is not None and init_shared_data is not None:
                open_path_mf = resolve_data_csv_path(DATA_DIR, "adjopen_wide.csv")
                if open_path_mf:
                    open_w = safe_read_csv(open_path_mf)
                    fc0 = open_w.columns[0]
                    if fc0 in ["", "Unnamed: 0", "trade_dt", "date"]:
                        open_w = open_w.rename(columns={fc0: "trade_dt"})
                    open_w["trade_dt"] = pd.to_datetime(open_w["trade_dt"])
                    pw = open_w.set_index("trade_dt").sort_index()
                    pw.columns = pw.columns.map(lambda x: str(x).upper())
                    price_src = "adjopen_wide.csv（与单因子回测一致）"
                else:
                    pw = price_wide.set_index("trade_dt").sort_index()
                    pw.columns = pw.columns.map(lambda x: str(x).upper())
                    price_src = "adjclose_wide.csv（未找到 adjopen_wide.csv，与单因子口径可能不一致）"
                mw = factor_wide.set_index("trade_dt").sort_index()
                mw.columns = mw.columns.map(lambda x: str(x).upper())
                init_shared_data(pw, mw)
                print(f"✅ 已注入多因子模块共享数据（{price_src} + market_value 宽表）")
        except Exception as mf_e:
            print(f"[WARN] 多因子共享数据注入失败: {mf_e}")

        return data
    except Exception as e:
        import traceback
        print(f"加载数据失败: {e}")
        traceback.print_exc()
        return None

def load_results():
    """加载所有回测结果"""
    results = {}

    # 1. 加载三种方法的对比数据
    comparison_path = os.path.join(RESULTS_DIR, 'all_methods_comparison.csv')
    if os.path.exists(comparison_path):
        results['comparison'] = pd.read_csv(comparison_path).to_dict('records')

    # 2. 加载年度IC数据
    yearly_ic_path = os.path.join(RESULTS_DIR, 'yearly_ic.csv')
    if os.path.exists(yearly_ic_path):
        yearly_df = pd.read_csv(yearly_ic_path)
        results['yearly_ic'] = yearly_df.to_dict('records')

    # 3. 加载各方法的详细数据
    for method in ['equal', 'mkt_val', 'factor_score']:
        method_dir = os.path.join(RESULTS_DIR, method)
        if os.path.exists(method_dir):
            results[method] = {}

            # 分组统计
            stats_path = os.path.join(method_dir, 'group_stats.csv')
            if os.path.exists(stats_path):
                results[method]['stats'] = pd.read_csv(stats_path).to_dict('records')

            # 分组净值
            nav_path = os.path.join(method_dir, 'group_nav.csv')
            if os.path.exists(nav_path):
                nav_df = pd.read_csv(nav_path)
                nav_df['trade_dt'] = pd.to_datetime(nav_df['trade_dt']).dt.strftime('%Y-%m-%d')
                results[method]['nav'] = nav_df.to_dict('records')

            # Long-Short
            ls_path = os.path.join(method_dir, 'long_short.csv')
            if os.path.exists(ls_path):
                ls_df = pd.read_csv(ls_path)
                ls_df['trade_dt'] = pd.to_datetime(ls_df['trade_dt']).dt.strftime('%Y-%m-%d')
                results[method]['long_short'] = ls_df.to_dict('records')

    # 4. 加载每日IC数据
    daily_ic_path = os.path.join(RESULTS_DIR, 'daily_ic.csv')
    if os.path.exists(daily_ic_path):
        ic_df = pd.read_csv(daily_ic_path)
        ic_df.columns = ['trade_dt', 'IC']
        ic_df['trade_dt'] = pd.to_datetime(ic_df['trade_dt']).dt.strftime('%Y-%m-%d')
        results['daily_ic'] = ic_df.to_dict('records')

    return results

@app.route('/')
def index():
    """主页"""
    return render_template('index.html')

@app.route('/correlation')
def correlation_page():
    try:
        return render_template('correlation.html')
    except TemplateNotFound:
        return jsonify({'success': False, 'error': 'templates/correlation.html 不存在，请先添加页面模板'}), 404


@app.route('/multi_factor')
def multi_factor_page():
    try:
        return render_template('multi_factor.html')
    except TemplateNotFound:
        return jsonify({'success': False, 'error': 'templates/multi_factor.html 不存在'}), 404


@app.route('/api/data_range')
def get_data_range():
    """
    返回本地价格/因子数据的日期范围，供前端初始化使用。
    如果缺少文件，返回空值但 success=True，避免前端 404。
    """
    try:
        from data.data_io import resolve_data_csv_path, safe_read_csv

        price_path = resolve_data_csv_path(DATA_DIR, "adjclose_wide.csv")
        if price_path:
            df = safe_read_csv(price_path)
            first_col = df.columns[0]
            df = df.rename(columns={first_col: 'trade_dt'})
            df['trade_dt'] = pd.to_datetime(df['trade_dt'], format='mixed', errors='coerce')
            min_date = df['trade_dt'].min().strftime('%Y-%m-%d')
            max_date = df['trade_dt'].max().strftime('%Y-%m-%d')
            n_tickers = len(df.columns) - 1
        else:
            min_date = ''
            max_date = ''
            n_tickers = 0
        return jsonify({'success': True, 'data': {
            'min_date': min_date,
            'max_date': max_date,
            'n_tickers': n_tickers
        }})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/factor_library')
def factor_library_status():
    """
    因子库状态接口。
    成功连接则返回基础信息和部分因子列表；失败则 connected=False。
    """
    # 直接写到 stderr 确保日志能输出（改用 print 避免 OSError）
    print("[因子库] 开始连接...")

    try:
        print("[因子库] 测试数据库连接...")
        ok, msg = test_database_connection()
        print(f"[因子库] 连接结果: ok={ok}, msg={msg}")

        if not ok:
            return jsonify({'success': True, 'data': {
                'connected': False,
                'message': msg
            }})

        print("[因子库] 获取因子列表...")
        db = get_factor_database()
        factors = db.get_available_factors_dynamic()

        print("[因子库] 获取日期范围...")
        min_date, max_date = db.get_date_range()

        # 跳过 ticker_count 查询（很慢），直接返回 0
        ticker_count = 0

        print(f"[因子库] 成功! 因子数={len(factors)}, 日期={min_date}~{max_date}")

        return jsonify({'success': True, 'data': {
            'connected': True,
            'factors': factors,
            'date_range': {'min': min_date, 'max': max_date},
            'ticker_count': ticker_count
        }})
    except Exception as e:
        import traceback
        print(f"[因子库] 异常: {e}")
        traceback.print_exc()
        return jsonify({'success': True, 'data': {
            'connected': False,
            'message': str(e)
        }})

@app.route('/api/factor_tables')
def factor_tables():
    """返回可用因子表列表"""
    import sys
    sys.stderr.write("[因子表] 开始获取因子表列表...\n")
    sys.stderr.flush()
    try:
        db = get_factor_database()
        tables = db.get_all_factor_tables()
        sys.stderr.write(f"[因子表] 成功! 获取到 {len(tables)} 个因子表\n")
        sys.stderr.flush()
        return jsonify({'success': True, 'data': tables})
    except Exception as e:
        import traceback
        sys.stderr.write(f"[因子表] 失败: {e}\n")
        sys.stderr.write(traceback.format_exc())
        sys.stderr.flush()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/available_factors', methods=['GET'])
def available_factors():
    """返回指定因子表的因子列表"""
    try:
        table_name = request.args.get('table_name')
        db = get_factor_database()
        factors = db.get_available_factors_dynamic(table_name)
        return jsonify({'success': True, 'data': factors})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


def _coerce_csv_trade_dt_column(s: pd.Series) -> pd.Series:
    """
    将因子 CSV 的日期列转为 datetime。

    CSV 中若日期为无引号整数 20190102，read_csv 会得到 int64；若直接 pd.to_datetime(20190102)，pandas 会按「纳秒时间戳」理解，结果落在 1970 年附近。
    对落在 [19000101, 21001231] 的整数/整数值浮点列，按 %Y%m%d 解析。
    """
    if s is None or len(s) == 0:
        return pd.to_datetime(s, errors='coerce')
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    num = pd.to_numeric(s, errors='coerce')
    finite = num.dropna()
    if len(finite) > 0 and (finite == np.floor(finite)).all():
        vi = finite.astype(np.int64)
        if vi.min() >= 19000101 and vi.max() <= 21001231:
            ii = num.round(0).astype('Int64')
            return pd.to_datetime(ii, format='%Y%m%d', errors='coerce')
    if s.dtype == object:
        str_s = s.astype(str).str.strip()
        if str_s.notna().all() and str_s.str.match(r'^\d{8}$', na=False).all():
            return pd.to_datetime(str_s, format='%Y%m%d', errors='coerce')
    return pd.to_datetime(s, errors='coerce')


@app.route('/api/parse_local_csv', methods=['POST'])
def parse_local_csv():
    """
    解析上传的本地CSV文件，返回因子信息摘要
    """
    try:
        from io import StringIO

        params = request.get_json()
        csv_data = params.get('csv_data')

        if not csv_data:
            return jsonify({'success': False, 'error': '未提供CSV数据'})

        df = pd.read_csv(StringIO(csv_data))

        if df.empty:
            return jsonify({'success': False, 'error': 'CSV为空，请检查文件内容'})

        lower_map = {c.lower().strip(): c for c in df.columns}
        date_candidates = ['trade_dt', 'date', 'datetime', 'dt']
        ticker_candidates = ['ticker', 'code', 'ts_code', 'symbol']
        factor_candidates = ['factor', 'value', 'factor_value']

        date_col = next((lower_map[k] for k in date_candidates if k in lower_map), None)
        ticker_col = next((lower_map[k] for k in ticker_candidates if k in lower_map), None)
        factor_col = next((lower_map[k] for k in factor_candidates if k in lower_map), None)

        # 兼容宽表：第一列当日期，其余列为股票
        source_format = 'wide'
        if date_col is None:
            first_col = df.columns[0]
            df = df.rename(columns={first_col: 'trade_dt'})
            date_col = 'trade_dt'
        else:
            df = df.rename(columns={date_col: 'trade_dt'})
            date_col = 'trade_dt'

        if ticker_col and factor_col:
            source_format = 'long'
            df = df.rename(columns={ticker_col: 'ticker', factor_col: 'factor'})
        elif source_format == 'wide':
            # 宽表转长表，统一输出口径
            value_cols = [c for c in df.columns if c != 'trade_dt']
            # 如果存在名为'factor'的列，从value_cols中排除，避免melt冲突
            value_cols = [c for c in value_cols if c != 'factor']
            if not value_cols:
                return jsonify({'success': False, 'error': '未识别到因子列，请检查CSV结构'})
            df = df.melt(id_vars='trade_dt', var_name='ticker', value_name='factor')

        df['trade_dt'] = _coerce_csv_trade_dt_column(df['trade_dt'])
        df = df.dropna(subset=['trade_dt', 'ticker'])

        if 'factor' not in df.columns:
            return jsonify({'success': False, 'error': '未识别到因子值列，请使用宽表或包含 factor 列的长表'})

        df['factor'] = pd.to_numeric(df['factor'], errors='coerce')

        total_rows = len(df)
        valid_factor_rows = int(df['factor'].notna().sum())
        missing_factor_rows = int(total_rows - valid_factor_rows)
        coverage = round((valid_factor_rows / total_rows) * 100, 2) if total_rows > 0 else 0.0
        missing_ratio = round((missing_factor_rows / total_rows) * 100, 2) if total_rows > 0 else 0.0

        if df.empty:
            return jsonify({'success': False, 'error': 'CSV解析后无有效数据，请检查日期和字段'})

        min_date = df['trade_dt'].min().strftime('%Y-%m-%d')
        max_date = df['trade_dt'].max().strftime('%Y-%m-%d')
        n_tickers = int(df['ticker'].nunique())
        n_dates = int(df['trade_dt'].nunique())

        return jsonify({
            'success': True,
            'data': {
                'format': source_format,
                'min_date': min_date,
                'max_date': max_date,
                'n_dates': n_dates,
                'n_tickers': n_tickers,
                'n_records': total_rows,
                'valid_factor_rows': valid_factor_rows,
                'missing_factor_rows': missing_factor_rows,
                'factor_coverage_pct': coverage,
                'factor_missing_pct': missing_ratio,
                'columns': list(df.columns)
            }
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/results')
def get_results():
    """获取回测结果数据"""
    try:
        results = load_results()
        return jsonify({'success': True, 'data': results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/benchmarks')
def get_benchmarks():
    """获取可用的基准指数列表"""
    benchmarks = [
        {"value": "000300.SH", "label": "沪深300"},
        # 其他指数暂无数据
        {"value": "none", "label": "无基准"}
    ]
    return jsonify({'success': True, 'data': benchmarks})

@app.route('/api/stream_logs')
def stream_logs():
    """SSE端点：流式传输日志"""

    def generate():
        for msg in stream_logger.get_messages():
            yield msg

    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/progress/<request_id>')
def get_progress(request_id):
    """查询指定回测任务的进度。"""
    with _progress_lock:
        progress = _progress_store.get(request_id)

    if not progress:
        return jsonify({'success': False, 'error': '未找到任务进度'})

    return jsonify({'success': True, 'data': progress})

@app.route('/api/ic_analysis')
def get_ic_analysis():
    """获取IC分析数据"""
    try:
        analyzer = get_cached_analyzer()
        if analyzer is None:
            return jsonify({'success': False, 'error': '数据加载失败'})

        # 获取完整的IC分析
        ic_analysis = analyzer.get_full_ic_analysis(method="pearson")

        # 格式化日期
        if 'cumulative' in ic_analysis:
            for item in ic_analysis['cumulative']:
                if isinstance(item.get('trade_dt'), pd.Timestamp):
                    item['trade_dt'] = item['trade_dt'].strftime('%Y-%m-%d')

        return jsonify({'success': True, 'data': ic_analysis})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/run_backtest', methods=['POST'])
def run_backtest():
    """
    执行回测
    
    接收参数:
    - start_date: 开始日期
    - end_date: 结束日期
    - rebalance_freq: 调仓频率 (daily/weekly/monthly)
    - transaction_cost: 交易费用 (%)
    - slippage: 滑点 (%)
    - risk_free_rate: 无风险利率 (%)
    - benchmark: 基准指数代码
    - allow_short: 是否允许卖空
    - initial_capital: 初始资金
    - weight_method: 加权方式 (equal/mkt_val/factor_score)
    - n_groups: 分组数量
    """
    # 注册线程以捕获日志
    stream_logger.register_thread()

    try:
        # 旧版统一入口 /api/run_backtest 已停用。当前 Web：因子库 -> POST /api/run_backtest_db；
        # 本地上传 CSV -> POST /api/run_backtest_csv（与「禁用 CSV」无关，CSV 走专用接口）。
        return jsonify({
            'success': False,
            'error': '接口 /api/run_backtest 已停用。请使用因子库回测（/api/run_backtest_db）或 CSV 回测（/api/run_backtest_csv）。'
        })

        # 获取参数
        params = request.get_json()

        # 重置性能计时器
        reset_profiler()
        api_timer = SectionTimer("API回测请求", print_sections=True)

        # 记录回测开始时间（秒）
        start_time = time.time()

        start_date = params.get('start_date')
        end_date = params.get('end_date')

        # 添加调试日志
        print(f"收到的参数: start_date={start_date}, end_date={end_date}")

        rebalance_freq = params.get('rebalance_freq', 'daily')
        transaction_cost = float(params.get('transaction_cost', 0)) / 100
        slippage = float(params.get('slippage', 0)) / 100
        risk_free_rate = float(params.get('risk_free_rate', 0)) / 100
        benchmark = params.get('benchmark', 'none')
        allow_short = params.get('allow_short', True)
        initial_capital = float(params.get('initial_capital', 1000000))
        weight_method = params.get('weight_method', 'equal')
        n_groups = int(params.get('n_groups', 5))

        # 检查是否有自定义因子CSV
        custom_factor_csv = params.get('custom_factor_csv')

        api_timer.start("数据加载")
        if custom_factor_csv:
            # 使用上传的自定义因子数据
            print("使用自定义因子CSV数据...")
            data = load_custom_factor_csv(custom_factor_csv)
            if data is None:
                return jsonify({'success': False, 'error': '解析自定义因子CSV失败'})
        else:
            # 使用默认缓存数据
            data = get_cached_data()
            if data is None:
                return jsonify({'success': False, 'error': '数据加载失败'})
        api_timer.end("数据加载")

        # 按日期筛选
        if start_date:
            data = data[data['trade_dt'] >= pd.to_datetime(start_date)]
        if end_date:
            data = data[data['trade_dt'] <= pd.to_datetime(end_date)]

        # 添加调试日志
        print(f"筛选后数据范围: {data['trade_dt'].min()} 至 {data['trade_dt'].max()}")
        print(f"筛选后数据量: {len(data)} 条")

        if len(data) == 0:
            return jsonify({'success': False, 'error': '所选日期范围内没有数据'})

        # 调仓频率处理
        if rebalance_freq == 'weekly':
            # 只保留每周一的数据
            data = data[data['trade_dt'].dt.dayofweek == 0]
        elif rebalance_freq == 'monthly':
            # 只保留每月第一个交易日
            data = data.groupby([data['trade_dt'].dt.to_period('M'), 'ticker']).first().reset_index(drop=True)

        # 创建回测引擎
        api_timer.start("回测引擎初始化")
        engine = BacktestEngine(
            data,
            factor_col="factor",
            price_col="adj_close",
            date_col="trade_dt",
            ticker_col="ticker",
            mkt_val_col="market_value"
        )
        api_timer.end("回测引擎初始化")

        # 运行回测
        api_timer.start("回测计算")
        backtest_start = time.time()
        results = engine.run_group_backtest(n_groups=n_groups, weight_method=weight_method)
        backtest_time = time.time() - backtest_start
        api_timer.end("回测计算")
        print(f"  回测引擎耗时: {backtest_time:.2f} 秒")

        # 应用交易成本和滑点（简化处理）
        total_cost_rate = transaction_cost + slippage
        if total_cost_rate > 0:
            # 简化：假设每日调仓，成本从收益中扣除
            nav_df = results['group_nav']
            for col in nav_df.columns:
                if col != 'trade_dt':
                    # 调整净值曲线
                    nav_df[col] = nav_df[col] * (1 - total_cost_rate) ** (len(nav_df) / 252)

        # 计算带风险利率的夏普比
        stats = results['group_stats'].copy()
        if 'sharpe' in stats.columns or '夏普比率' in stats.columns:
            sharpe_col = '夏普比率' if '夏普比率' in stats.columns else 'sharpe'
            ann_ret_col = '年化收益_%' if '年化收益_%' in stats.columns else 'ann_return'
            vol_col = '年化波动_%' if '年化波动_%' in stats.columns else 'volatility'

            # 重新计算夏普比率 (考虑无风险利率)
            if ann_ret_col in stats.columns and vol_col in stats.columns:
                stats[sharpe_col] = (stats[ann_ret_col] / 100 - risk_free_rate) / (stats[vol_col] / 100)
                stats[sharpe_col] = stats[sharpe_col].round(2)

        # 格式化返回数据
        nav_data = results['group_nav'].copy()
        nav_data['trade_dt'] = pd.to_datetime(nav_data['trade_dt']).dt.strftime('%Y-%m-%d')

        ls_data = results['long_short'].copy()
        ls_data['trade_dt'] = pd.to_datetime(ls_data['trade_dt']).dt.strftime('%Y-%m-%d')

        # 因子分析 (使用缓存优化)
        # 创建分析器一次，内部所有计算都会被缓存
        print("开始因子分析...")
        api_timer.start("因子分析")
        factor_analysis_start = time.time()

        analyzer = FactorAnalyzer(
            data,
            factor_col="factor",
            price_col="adj_close",
            date_col="trade_dt",
            ticker_col="ticker"
        )

        # 这些方法内部已经实现了缓存，重复调用不会重复计算
        summary_start = time.time()
        factor_summary = analyzer.get_factor_summary()
        summary_time = time.time() - summary_start
        print(f"  [OK] 因子摘要计算完成 ({summary_time:.2f}s)")

        yearly_start = time.time()
        yearly_ic = analyzer.yearly_analysis()
        yearly_time = time.time() - yearly_start
        print(f"  [OK] 年度IC分析完成 ({yearly_time:.2f}s)")

        ic_start = time.time()
        ic_analysis = analyzer.get_full_ic_analysis(method="pearson")
        ic_time = time.time() - ic_start
        print(f"  [OK] 完整IC分析完成 ({ic_time:.2f}s)")

        factor_analysis_time = time.time() - factor_analysis_start
        api_timer.end("因子分析")
        print(f"  因子分析总耗时: {factor_analysis_time:.2f} 秒")

        # 格式化IC分析中的日期
        if 'cumulative' in ic_analysis:
            for item in ic_analysis['cumulative']:
                if isinstance(item.get('trade_dt'), pd.Timestamp):
                    item['trade_dt'] = item['trade_dt'].strftime('%Y-%m-%d')

        response_data = {
            'group_stats': stats.to_dict('records'),
            'group_nav': nav_data.to_dict('records'),
            'long_short': ls_data.to_dict('records'),
            'factor_summary': {
                k: (float(v) if isinstance(v, (np.floating, np.integer)) else v)
                for k, v in factor_summary.items()
            },
            'yearly_ic': yearly_ic.to_dict('records'),
            'ic_analysis': ic_analysis,
            'params': {
                'start_date': start_date,
                'end_date': end_date,
                'rebalance_freq': rebalance_freq,
                'transaction_cost': transaction_cost * 100,
                'slippage': slippage * 100,
                'risk_free_rate': risk_free_rate * 100,
                'benchmark': benchmark,
                'allow_short': allow_short,
                'initial_capital': initial_capital,
                'weight_method': weight_method,
                'n_groups': n_groups
            }
        }

        # 计算总耗时并打印
        end_time = time.time()
        elapsed_time = end_time - start_time

        # 计算其他部分的时间（数据处理等）
        other_time = elapsed_time - backtest_time - factor_analysis_time

        # 生成API计时器报告
        api_timer.report()

        print(f"\n{'=' * 60}")
        print(f"时间统计:")
        print(f"  数据加载与处理: {other_time:.2f} 秒 ({other_time / elapsed_time * 100:.1f}%)")
        print(f"  回测引擎计算:   {backtest_time:.2f} 秒 ({backtest_time / elapsed_time * 100:.1f}%)")
        print(f"  因子分析:       {factor_analysis_time:.2f} 秒 ({factor_analysis_time / elapsed_time * 100:.1f}%)")
        print(f"    - 因子摘要:   {summary_time:.2f} 秒")
        print(f"    - 年度IC:     {yearly_time:.2f} 秒")
        print(f"    - IC分析:     {ic_time:.2f} 秒")
        print(f"  总耗时:         {elapsed_time:.2f} 秒")
        print(f"{'=' * 60}\n")

        # 添加详细的时间统计信息
        response_data['elapsed_time'] = round(elapsed_time, 2)
        response_data['time_breakdown'] = {
            'total': round(elapsed_time, 2),
            'data_processing': round(other_time, 2),
            'backtest_engine': round(backtest_time, 2),
            'factor_analysis': round(factor_analysis_time, 2),
            'factor_summary': round(summary_time, 2),
            'yearly_ic': round(yearly_time, 2),
            'ic_analysis': round(ic_time, 2)
        }

        # 添加详细的分段计时信息
        response_data['detailed_timing'] = {
            name: round(elapsed, 4)
            for name, elapsed in api_timer.sections.items()
        }

        # 添加实际使用的日期范围
        response_data['actual_date_range'] = {
            'start': str(data['trade_dt'].min().date()),
            'end': str(data['trade_dt'].max().date())
        }

        # 加载基准指数数据（如果指定了基准）
        print(f"[回测] 基准参数: {benchmark}")
        if benchmark != 'none':
            try:
                print(f"[回测] 开始加载基准数据...")
                benchmark_nav = load_benchmark_data(benchmark, start_date, end_date)

                if not benchmark_nav.empty:
                    # 对齐日期
                    nav_dates = pd.to_datetime(nav_data['trade_dt'])
                    benchmark_aligned = benchmark_nav.reindex(nav_dates, method='ffill')
                    benchmark_aligned = benchmark_aligned.fillna(1.0)

                    # 计算超额净值（每个分组相对于基准）
                    excess_nav_data = nav_data.copy()
                    for col in excess_nav_data.columns:
                        if col != 'trade_dt':
                            # 计算超额收益（净值相除）
                            excess_nav_data[col] = excess_nav_data[col] / benchmark_aligned.values

                    # 格式化基准净值
                    benchmark_nav_formatted = pd.DataFrame({
                        'trade_dt': nav_dates.dt.strftime('%Y-%m-%d'),
                        'benchmark_nav': benchmark_aligned.values
                    })

                    response_data['benchmark_nav'] = benchmark_nav_formatted.to_dict('records')
                    response_data['excess_nav'] = excess_nav_data.to_dict('records')

                    print(f"[OK] 基准指数数据已加载: {benchmark}")
            except Exception as e:
                print(f"[WARN]️ 加载基准指数数据失败: {e}")
                # 不阻断回测，只是没有基准数据

        return jsonify({'success': True, 'data': response_data})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})
    finally:
        stream_logger.unregister_thread()

def load_custom_factor_csv(csv_data: str, start_date: str = None, end_date: str = None):
    """
    解析用户上传的 CSV 因子数据，并与本地开盘价数据合并。
    支持宽表（第一列日期，其余列股票代码）和长表（trade_dt/ticker/factor）。
    返回与 load_factor_from_database 相同格式的 DataFrame，价格列名为 adj_open。
    """
    from io import StringIO

    df = pd.read_csv(StringIO(csv_data))
    if df.empty:
        return None

    # 识别列
    lower_map = {c.lower().strip(): c for c in df.columns}
    date_col = next((lower_map[k] for k in ['trade_dt', 'date', 'datetime', 'dt'] if k in lower_map), None)
    ticker_col = next((lower_map[k] for k in ['ticker', 'code', 'ts_code', 'symbol'] if k in lower_map), None)
    factor_col = next((lower_map[k] for k in ['factor', 'value', 'factor_value'] if k in lower_map), None)

    # 宽表 vs 长表
    if date_col is None:
        first_col = df.columns[0]
        df = df.rename(columns={first_col: 'trade_dt'})
        value_cols = [c for c in df.columns if c != 'trade_dt']
        value_cols = [c for c in value_cols if c != 'factor']
        if not value_cols:
            return None
        df = df.melt(id_vars='trade_dt', var_name='ticker', value_name='factor')
    elif ticker_col and factor_col:
        df = df.rename(columns={date_col: 'trade_dt', ticker_col: 'ticker', factor_col: 'factor'})
    else:
        df = df.rename(columns={date_col: 'trade_dt'})
        value_cols = [c for c in df.columns if c != 'trade_dt']
        value_cols = [c for c in value_cols if c != 'factor']
        if not value_cols:
            return None
        df = df.melt(id_vars='trade_dt', var_name='ticker', value_name='factor')

    # 类型转换 & 清洗（与 parse_local_csv 共用，避免 YYYYMMDD 整数被当成纳秒时间戳）
    df['trade_dt'] = _coerce_csv_trade_dt_column(df['trade_dt'])

    df = df.dropna(subset=['trade_dt', 'ticker'])
    df['factor'] = pd.to_numeric(df['factor'], errors='coerce')
    df = df.dropna(subset=['factor'])
    df['ticker'] = df['ticker'].astype(str).str.strip().str.upper()

    if start_date:
        df = df[df['trade_dt'] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df['trade_dt'] <= pd.to_datetime(end_date)]
    if df.empty:
        return None

    # 加载本地开盘价数据（宽表格式）
    global _price_cache
    from data.data_io import resolve_data_csv_path, safe_read_csv

    price_path = resolve_data_csv_path(DATA_DIR, "adjopen_wide.csv")
    if not price_path:
        print("[WARN] 本地开盘价数据不存在: adjopen_wide.csv 或 adjopen_wide.csv.gz")
        return None

    if _price_cache is None:
        price_wide = safe_read_csv(price_path)
        first_col = price_wide.columns[0]
        if first_col in ['', 'Unnamed: 0', 'trade_dt', 'date']:
            price_wide = price_wide.rename(columns={first_col: 'trade_dt'})
        price_wide['trade_dt'] = pd.to_datetime(price_wide['trade_dt'])
        # 确保列名不冲突，melt后命名为 adj_open
        if 'adj_open' in price_wide.columns:
            price_wide = price_wide.rename(columns={'adj_open': '_original_adj_open'})
        _price_cache = price_wide.melt(id_vars=['trade_dt'], var_name='ticker', value_name='adj_open')
        _price_cache['ticker'] = _price_cache['ticker'].astype(str).str.strip().str.upper()

    factor_min_date = df['trade_dt'].min()
    factor_max_date = df['trade_dt'].max()
    unique_tickers = df['ticker'].unique()

    price_df = _price_cache[
        (_price_cache['trade_dt'] >= factor_min_date) &
        (_price_cache['trade_dt'] <= factor_max_date) &
        (_price_cache['ticker'].isin(unique_tickers))
        ]
    if price_df.empty:
        return None

    # 合并因子和开盘价
    factor_idx = df.set_index(['ticker', 'trade_dt'])
    price_idx = price_df.set_index(['ticker', 'trade_dt'])
    merged = factor_idx.join(price_idx, how='inner').reset_index()
    merged = merged.dropna(subset=['factor', 'adj_open'])   # 改为 adj_open
    merged['market_value'] = merged['factor']

    print(f"✅ CSV 因子加载 (开盘价): {len(merged)} 条, {merged['ticker'].nunique()} 只股票")
    return merged

def _simulate_progress(request_id, start_pct, max_pct, stage_name, detail_prefix, expected_seconds):
    """通用后台线程：模拟进度推进"""

    def _run():
        current = start_pct
        start_time = time.time()

        while True:
            task_info = _progress_store.get(request_id, {})
            if task_info.get("status") in ("completed", "error"):
                break

            time.sleep(0.5)
            elapsed = time.time() - start_time
            progress_ratio = (elapsed / expected_seconds) if expected_seconds > 0 else 1

            if progress_ratio >= 1.0:
                # 【修复】超过预期时间了，停在 max_pct - 1，不要超过预设上限，避免影响下一阶段
                current = max(current, max_pct - 1)
                detail_text = f"{detail_prefix} (处理较慢，请稍候...)"
            else:
                # 正常预期时间内，先快后慢跑
                current = start_pct + (max_pct - start_pct) * (progress_ratio ** 0.6)
                detail_text = f"{detail_prefix} ({int(progress_ratio * 100)}%)"

            _set_progress(request_id, int(current), stage_name, detail=detail_text)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

@app.route('/api/run_backtest_csv', methods=['POST'])
def run_backtest_from_csv():
    """使用用户上传的 CSV 因子数据执行回测"""
    stream_logger.register_thread()
    try:
        params = request.get_json()
        request_id = params.get('request_id', f"job_{int(time.time() * 1000)}")
        _set_progress(request_id, 1, "初始化", detail="正在初始化 CSV 回测任务")
        response_data = execute_csv_backtest(
            params=params,
            request_id=request_id,
            load_custom_factor_csv=load_custom_factor_csv,
            load_benchmark_data=load_benchmark_data,
            set_last_backtest=set_last_backtest,
            set_progress=_set_progress,
            simulate_progress=_simulate_progress,
        )
        return jsonify({'success': True, 'data': response_data})

    except Exception as e:
        import traceback
        traceback.print_exc()
        err_msg = str(e)
        if "未提供 CSV 数据" in err_msg:
            _set_progress(locals().get('request_id', ''), 100, "失败", status="error", detail="未提供 CSV 数据")
            return jsonify({'success': False, 'error': '未提供 CSV 数据'})
        if "CSV 解析失败或与价格数据无交集" in err_msg:
            _set_progress(locals().get('request_id', ''), 100, "失败", status="error", detail="CSV 解析失败或与价格数据无交集")
            return jsonify({'success': False, 'error': 'CSV 解析失败或与价格数据无交集'})
        if "筛选后数据为空" in err_msg:
            _set_progress(locals().get('request_id', ''), 100, "失败", status="error", detail="筛选后数据为空")
            return jsonify({'success': False, 'error': '筛选后数据为空'})
        _set_progress(locals().get('request_id', ''), 100, "失败", status="error", detail=err_msg)
        return jsonify({'success': False, 'error': err_msg})
    finally:
        stream_logger.unregister_thread()

@app.route('/api/run_backtest_db', methods=['POST'])
def run_backtest_from_database():
    """
    使用数据库因子执行回测 (修复版 - 进度条在加载时也会动)
    """
    stream_logger.register_thread()

    try:
        params = request.get_json()
        request_id = params.get('request_id', f"job_{int(time.time() * 1000)}")
        _set_progress(request_id, 0, "初始化", detail="正在初始化回测任务")
        response_data = execute_database_backtest(
            params=params,
            request_id=request_id,
            resolve_factor_parquet_path=_factor_data_gateway.resolve_local_parquet_path,
            load_factor_from_database=load_factor_from_database,
            load_benchmark_data=load_benchmark_data,
            set_last_backtest=set_last_backtest,
            set_progress=_set_progress,
        )
        return jsonify({'success': True, 'data': response_data})

    except Exception as e:
        import traceback
        traceback.print_exc()
        err_msg = str(e)
        if "数据库返回数据为空" in err_msg:
            _set_progress(locals().get('request_id', ''), 100, "失败", status="error", detail="数据库返回数据为空")
            return jsonify({'success': False, 'error': '数据库返回数据为空'})
        if "筛选后数据为空" in err_msg:
            _set_progress(locals().get('request_id', ''), 100, "失败", status="error", detail="筛选后数据为空")
            return jsonify({'success': False, 'error': '筛选后数据为空'})
        _set_progress(locals().get('request_id', ''), 100, "失败", status="error", detail=err_msg)
        return jsonify({'success': False, 'error': str(e)})
    finally:
        stream_logger.unregister_thread()

@app.route('/api/factor_correlation', methods=['POST'])
def analyze_factor_correlation():
    """
    执行因子相关性分析（重构版）

    返回简化的数据结构，只包含三张图表的数据：
    1. 因子相关性图表 - 横轴时间，纵轴相关性
    2. IC图表 - 横轴时间，纵轴IC
    3. 收益率图表 - 横轴时间，纵轴多空累计净值
    """
    import time

    # 将相关性分析日志接入 stream_logger，保证既能在终端看到，也能被 /api/stream_logs 读取
    stream_logger.register_thread()

    def log(msg):
        stream_logger.log(f"[相关性分析] {msg}")

    try:
        params = request.get_json()
        log("开始执行...")
        analysis_result = execute_factor_correlation(
            params=params,
            get_last_backtest_data=get_last_backtest_data,
            get_last_backtest_params=get_last_backtest_params,
            load_custom_factor_csv=load_custom_factor_csv,
            load_factor_from_database=load_factor_from_database,
            get_target_factor_columns=lambda table_name: get_factor_database().get_all_factor_columns(table_name),
            load_multiple_factors=_factor_data_gateway.load_multiple_factors,
            log=log,
        )
        return jsonify({'success': True, 'data': analysis_result})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})

    finally:
        stream_logger.unregister_thread()

@app.route('/api/detect_new_tables')
def api_detect_new_tables():
    """检测新增的因子表"""
    try:
        force = request.args.get('force_rescan', 'false').lower() == 'true'
        new_tables = detect_new_factor_tables(force_rescan=force)
        return jsonify({'success': True, 'data': new_tables})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    print("=" * 60)
    print("  量化回测结果展示系统")
    print("=" * 60)
    print(f"  项目目录: {PROJECT_ROOT}")
    print(f"  结果目录: {RESULTS_DIR}")
    # 获取端口配置
    port = get_server_port(5000)
    print(f"  本机访问: http://127.0.0.1:{port}")
    print(f"  局域网访问: http://<你的IP>:{port}")
    print("=" * 60)

    # 设置环境变量使控制台输出UTF-8
    os.environ['PYTHONIOENCODING'] = 'utf-8'

    # debug 默认会启用 reloader（双进程），Cursor/Windows 下常出现日志看不到当前终端。
    # 默认改为单进程；如需热重载可显式 set FLASK_USE_RELOADER=1
    use_reloader = os.environ.get('FLASK_USE_RELOADER', 'false').lower() in ('1', 'true', 'yes')
    print(f"  日志文件: {os.environ.get('QUANT_APP_LOG', 'logs/quant_app.log')}")

    app.run(debug=True, host='0.0.0.0', port=port, use_reloader=use_reloader)

