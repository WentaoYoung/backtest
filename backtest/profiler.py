"""
性能分析器模块
提供统一的计时和性能分析工具

使用方法:
    1. 使用装饰器:
        @profile_function
        def my_function():
            ...
    
    2. 使用上下文管理器:
        with TimingContext("操作名称"):
            ...
    
    3. 手动计时:
        profiler = TimingProfiler()
        profiler.start("操作名称")
        ...
        profiler.end("操作名称")
        profiler.report()
"""

import time
import functools
from typing import Dict, List, Optional, Callable
from contextlib import contextmanager
import pandas as pd


class TimingProfiler:
    """
    性能计时分析器
    
    支持:
    - 多层级计时（支持嵌套）
    - 自动统计最小/最大/平均时间
    - 生成详细报告
    """
    
    # 全局单例实例
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self.timings: Dict[str, List[float]] = {}
        self.active_timers: Dict[str, float] = {}
        self.hierarchy: Dict[str, List[str]] = {}  # 父子关系
        self.current_parent: Optional[str] = None
        self.enabled = True  # 可以全局禁用计时
        
    def reset(self):
        """重置所有计时数据"""
        self.timings.clear()
        self.active_timers.clear()
        self.hierarchy.clear()
        self.current_parent = None
    
    def start(self, name: str, parent: str = None):
        """
        开始计时
        
        参数:
            name: 计时器名称
            parent: 父级计时器（用于层级显示）
        """
        if not self.enabled:
            return
            
        self.active_timers[name] = time.time()
        
        # 记录层级关系
        if parent:
            if parent not in self.hierarchy:
                self.hierarchy[parent] = []
            if name not in self.hierarchy[parent]:
                self.hierarchy[parent].append(name)
    
    def end(self, name: str) -> float:
        """
        结束计时
        
        参数:
            name: 计时器名称
            
        返回:
            经过的时间（秒）
        """
        if not self.enabled:
            return 0.0
            
        if name not in self.active_timers:
            return 0.0
        
        elapsed = time.time() - self.active_timers[name]
        del self.active_timers[name]
        
        # 记录时间
        if name not in self.timings:
            self.timings[name] = []
        self.timings[name].append(elapsed)
        
        return elapsed
    
    def get_elapsed(self, name: str) -> float:
        """获取当前计时器已经过的时间（不结束计时）"""
        if name in self.active_timers:
            return time.time() - self.active_timers[name]
        return 0.0
    
    def get_stats(self, name: str) -> Dict:
        """
        获取指定计时器的统计信息
        
        返回:
            包含 count, total, mean, min, max 的字典
        """
        if name not in self.timings or not self.timings[name]:
            return {"count": 0, "total": 0, "mean": 0, "min": 0, "max": 0}
        
        times = self.timings[name]
        return {
            "count": len(times),
            "total": sum(times),
            "mean": sum(times) / len(times),
            "min": min(times),
            "max": max(times)
        }
    
    def report(self, show_hierarchy: bool = True, min_time: float = 0.001) -> str:
        """
        生成性能报告
        
        参数:
            show_hierarchy: 是否显示层级结构
            min_time: 只显示耗时超过此值的计时器
            
        返回:
            格式化的报告字符串
        """
        if not self.timings:
            return "没有计时数据"
        
        lines = []
        lines.append("\n" + "=" * 70)
        lines.append("性能分析报告 (Performance Report)")
        lines.append("=" * 70)
        
        # 按总时间排序
        sorted_items = sorted(
            self.timings.items(),
            key=lambda x: sum(x[1]),
            reverse=True
        )
        
        # 计算总时间（用于百分比）
        total_time = sum(sum(times) for _, times in sorted_items)
        
        # 表头
        lines.append(f"{'操作名称':<35} {'调用次数':>8} {'总耗时':>10} {'平均':>8} {'占比':>8}")
        lines.append("-" * 70)
        
        for name, times in sorted_items:
            if sum(times) < min_time:
                continue
                
            stats = self.get_stats(name)
            pct = (stats['total'] / total_time * 100) if total_time > 0 else 0
            
            # 根据层级添加缩进
            indent = ""
            for parent, children in self.hierarchy.items():
                if name in children:
                    indent = "  └─ "
                    break
            
            lines.append(
                f"{indent}{name:<35} {stats['count']:>8} "
                f"{stats['total']:>9.3f}s {stats['mean']:>7.3f}s {pct:>7.1f}%"
            )
        
        lines.append("-" * 70)
        lines.append(f"{'总计':<35} {'':<8} {total_time:>9.3f}s")
        lines.append("=" * 70 + "\n")
        
        report = "\n".join(lines)
        print(report)
        return report
    
    def to_dataframe(self) -> pd.DataFrame:
        """将计时数据转换为 DataFrame"""
        data = []
        for name, times in self.timings.items():
            stats = self.get_stats(name)
            data.append({
                "操作名称": name,
                "调用次数": stats["count"],
                "总耗时(秒)": round(stats["total"], 4),
                "平均耗时(秒)": round(stats["mean"], 4),
                "最小耗时(秒)": round(stats["min"], 4),
                "最大耗时(秒)": round(stats["max"], 4)
            })
        
        df = pd.DataFrame(data)
        if not df.empty:
            df = df.sort_values("总耗时(秒)", ascending=False).reset_index(drop=True)
        return df
    
    def get_summary_dict(self) -> Dict:
        """获取简洁的摘要字典（用于API返回）"""
        result = {}
        for name, times in self.timings.items():
            result[name] = {
                "count": len(times),
                "total": round(sum(times), 4),
                "mean": round(sum(times) / len(times), 4) if times else 0
            }
        return result


# 全局实例
_profiler = TimingProfiler()


def get_profiler() -> TimingProfiler:
    """获取全局性能分析器实例"""
    return _profiler


def reset_profiler():
    """重置全局性能分析器"""
    _profiler.reset()


@contextmanager
def TimingContext(name: str, parent: str = None, print_time: bool = False):
    """
    计时上下文管理器
    
    用法:
        with TimingContext("数据加载"):
            load_data()
    """
    profiler = get_profiler()
    profiler.start(name, parent)
    try:
        yield
    finally:
        elapsed = profiler.end(name)
        if print_time:
            print(f"  ⏱ {name}: {elapsed:.3f}s")


def profile_function(name: str = None, print_time: bool = True):
    """
    函数计时装饰器
    
    用法:
        @profile_function("我的函数")
        def my_function():
            ...
    """
    def decorator(func: Callable):
        func_name = name or func.__name__
        
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            profiler = get_profiler()
            profiler.start(func_name)
            try:
                result = func(*args, **kwargs)
                return result
            finally:
                elapsed = profiler.end(func_name)
                if print_time:
                    print(f"  ⏱ {func_name}: {elapsed:.3f}s")
        
        return wrapper
    return decorator


class SectionTimer:
    """
    分段计时器 - 用于追踪一个函数内部多个步骤的耗时
    
    用法:
        timer = SectionTimer("回测引擎")
        
        timer.start("数据准备")
        # ... 数据准备代码 ...
        timer.end("数据准备")
        
        timer.start("分组计算")
        # ... 分组计算代码 ...
        timer.end("分组计算")
        
        timer.report()
    """
    
    def __init__(self, name: str, print_sections: bool = True):
        self.name = name
        self.sections: Dict[str, float] = {}
        self.active_section: Optional[str] = None
        self.active_start: Optional[float] = None
        self.total_start = time.time()
        self.print_sections = print_sections
    
    def start(self, section_name: str):
        """开始一个分段计时"""
        self.active_section = section_name
        self.active_start = time.time()
    
    def end(self, section_name: str = None) -> float:
        """结束一个分段计时"""
        if self.active_start is None:
            return 0.0
        
        name = section_name or self.active_section
        elapsed = time.time() - self.active_start
        self.sections[name] = elapsed
        
        if self.print_sections:
            print(f"    └─ {name}: {elapsed:.3f}s")
        
        self.active_section = None
        self.active_start = None
        
        return elapsed
    
    def get_total_time(self) -> float:
        """获取总耗时"""
        return time.time() - self.total_start
    
    def report(self) -> Dict:
        """
        生成报告
        
        返回:
            包含各分段耗时和统计的字典
        """
        total = self.get_total_time()
        sections_total = sum(self.sections.values())
        other = total - sections_total
        
        result = {
            "name": self.name,
            "total_time": round(total, 4),
            "sections": {},
            "other_time": round(other, 4)
        }
        
        for section_name, elapsed in self.sections.items():
            pct = (elapsed / total * 100) if total > 0 else 0
            result["sections"][section_name] = {
                "time": round(elapsed, 4),
                "percent": round(pct, 1)
            }
        
        # 打印汇总
        print(f"\n  [图表] [{self.name}] 耗时汇总:")
        for section_name, elapsed in sorted(self.sections.items(), key=lambda x: -x[1]):
            pct = (elapsed / total * 100) if total > 0 else 0
            bar = "#" * int(pct / 5)
            print(f"      {section_name:<25} {elapsed:>7.3f}s ({pct:>5.1f}%) {bar}")
        
        if other > 0.01:
            pct = (other / total * 100) if total > 0 else 0
            print(f"      {'(其他)':<25} {other:>7.3f}s ({pct:>5.1f}%)")
        
        print(f"      {'─' * 45}")
        print(f"      {'总计':<25} {total:>7.3f}s")
        
        return result


























