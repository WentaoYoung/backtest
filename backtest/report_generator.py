"""
回测报告生成器
生成 HTML 格式的回测结果展示页面
"""

import pandas as pd
import os
from typing import Dict
import base64


def generate_html_report(
    results: Dict,
    factor_summary: Dict,
    yearly_ic: pd.DataFrame,
    output_path: str = "./results/report.html"
) -> str:
    """
    生成 HTML 格式的回测报告
    
    参数:
        results: 回测结果字典 (包含 equal, mkt_val, factor_score 三种方法的结果)
        factor_summary: 因子分析摘要
        yearly_ic: 分年度 IC 数据
        output_path: 输出路径
    
    返回:
        生成的 HTML 文件路径
    """
    
    # 读取图片并转为 base64
    def img_to_base64(img_path):
        if os.path.exists(img_path):
            with open(img_path, "rb") as f:
                return base64.b64encode(f.read()).decode()
        return ""
    
    # 获取图片的 base64 编码
    group_nav_img = img_to_base64("./results/group_nav.png")
    long_short_img = img_to_base64("./results/long_short.png")
    comparison_img = img_to_base64("./results/comparison.png")
    
    # 生成各方法的统计表格
    def stats_to_html(stats_df):
        return stats_df.to_html(index=False, classes="stats-table", border=0)
    
    # 生成年度 IC 表格
    yearly_ic_html = yearly_ic.round(4).to_html(index=False, classes="stats-table", border=0)
    
    # 计算汇总数据
    weight_names = {"equal": "等权", "mkt_val": "市值加权", "factor_score": "因子加权"}
    
    summary_rows = ""
    for method in ["equal", "mkt_val", "factor_score"]:
        if method in results:
            r = results[method]
            g1_ret = r["group_stats"].iloc[0]["累计收益_%"]
            g5_ret = r["group_stats"].iloc[-1]["累计收益_%"]
            ls_ret = (r["long_short"]["Long-Short"].iloc[-1] - 1) * 100
            sharpe = r["group_stats"].iloc[-1]["夏普比率"]
            
            summary_rows += f"""
            <tr>
                <td>{weight_names[method]}</td>
                <td>{g1_ret:.2f}%</td>
                <td>{g5_ret:.2f}%</td>
                <td>{ls_ret:.2f}%</td>
                <td>{sharpe:.2f}</td>
            </tr>
            """
    
    # HTML 模板
    html_content = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>因子回测报告</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            color: #e0e0e0;
            line-height: 1.6;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 40px 20px;
        }}
        
        header {{
            text-align: center;
            margin-bottom: 50px;
            padding: 40px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 20px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
        }}
        
        h1 {{
            font-size: 2.5rem;
            font-weight: 700;
            background: linear-gradient(90deg, #00d4ff, #7b2cbf);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 10px;
        }}
        
        .subtitle {{
            color: #888;
            font-size: 1.1rem;
        }}
        
        .section {{
            background: rgba(255, 255, 255, 0.05);
            border-radius: 16px;
            padding: 30px;
            margin-bottom: 30px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
        }}
        
        .section-title {{
            font-size: 1.5rem;
            font-weight: 600;
            margin-bottom: 20px;
            color: #00d4ff;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        
        .section-title::before {{
            content: "";
            width: 4px;
            height: 24px;
            background: linear-gradient(180deg, #00d4ff, #7b2cbf);
            border-radius: 2px;
        }}
        
        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }}
        
        .metric-card {{
            background: rgba(0, 212, 255, 0.1);
            border-radius: 12px;
            padding: 20px;
            text-align: center;
            border: 1px solid rgba(0, 212, 255, 0.2);
            transition: transform 0.3s, box-shadow 0.3s;
        }}
        
        .metric-card:hover {{
            transform: translateY(-5px);
            box-shadow: 0 10px 30px rgba(0, 212, 255, 0.2);
        }}
        
        .metric-value {{
            font-size: 2rem;
            font-weight: 700;
            color: #00d4ff;
        }}
        
        .metric-label {{
            color: #888;
            font-size: 0.9rem;
            margin-top: 5px;
        }}
        
        .stats-table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
        }}
        
        .stats-table th,
        .stats-table td {{
            padding: 12px 15px;
            text-align: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }}
        
        .stats-table th {{
            background: rgba(0, 212, 255, 0.2);
            color: #00d4ff;
            font-weight: 600;
        }}
        
        .stats-table tr:hover {{
            background: rgba(255, 255, 255, 0.05);
        }}
        
        .chart-container {{
            margin: 20px 0;
            text-align: center;
        }}
        
        .chart-container img {{
            max-width: 100%;
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }}
        
        .chart-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(500px, 1fr));
            gap: 20px;
        }}
        
        .positive {{
            color: #00ff88;
        }}
        
        .negative {{
            color: #ff4757;
        }}
        
        footer {{
            text-align: center;
            padding: 30px;
            color: #666;
            font-size: 0.9rem;
        }}
        
        .badge {{
            display: inline-block;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 500;
        }}
        
        .badge-success {{
            background: rgba(0, 255, 136, 0.2);
            color: #00ff88;
        }}
        
        .badge-warning {{
            background: rgba(255, 193, 7, 0.2);
            color: #ffc107;
        }}
        
        .badge-danger {{
            background: rgba(255, 71, 87, 0.2);
            color: #ff4757;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>📊 因子回测报告</h1>
            <p class="subtitle">Market Size Factor - 截面分组回测分析</p>
        </header>
        
        <!-- 因子概览 -->
        <div class="section">
            <h2 class="section-title">因子概览</h2>
            <div class="metrics-grid">
                <div class="metric-card">
                    <div class="metric-value">{factor_summary.get('n_tickers', 'N/A')}</div>
                    <div class="metric-label">股票数量</div>
                </div>
                <div class="metric-card">
                    <div class="metric-value">{factor_summary.get('IC_pearson', 0):.4f}</div>
                    <div class="metric-label">IC (Pearson)</div>
                </div>
                <div class="metric-card">
                    <div class="metric-value">{factor_summary.get('IC_spearman', 0):.4f}</div>
                    <div class="metric-label">IC (Spearman)</div>
                </div>
                <div class="metric-card">
                    <div class="metric-value">{factor_summary.get('IR', 0):.4f}</div>
                    <div class="metric-label">IR</div>
                </div>
            </div>
        </div>
        
        <!-- 方法对比 -->
        <div class="section">
            <h2 class="section-title">三种加权方式对比</h2>
            <table class="stats-table">
                <thead>
                    <tr>
                        <th>加权方式</th>
                        <th>G1 累计收益</th>
                        <th>G5 累计收益</th>
                        <th>Long-Short 收益</th>
                        <th>夏普比率</th>
                    </tr>
                </thead>
                <tbody>
                    {summary_rows}
                </tbody>
            </table>
        </div>
        
        <!-- 图表展示 -->
        <div class="section">
            <h2 class="section-title">回测图表</h2>
            
            <h3 style="color: #888; margin: 20px 0 10px;">分组净值曲线</h3>
            <div class="chart-container">
                <img src="data:image/png;base64,{group_nav_img}" alt="分组净值曲线">
            </div>
            
            <h3 style="color: #888; margin: 20px 0 10px;">Long-Short 净值曲线</h3>
            <div class="chart-container">
                <img src="data:image/png;base64,{long_short_img}" alt="Long-Short净值">
            </div>
            
            <h3 style="color: #888; margin: 20px 0 10px;">综合对比</h3>
            <div class="chart-container">
                <img src="data:image/png;base64,{comparison_img}" alt="综合对比">
            </div>
        </div>
        
        <!-- 分年度IC -->
        <div class="section">
            <h2 class="section-title">分年度 IC 分析</h2>
            {yearly_ic_html}
        </div>
        
        <!-- 各组详细统计 -->
        <div class="section">
            <h2 class="section-title">等权回测详细统计</h2>
            {stats_to_html(results.get('equal', {}).get('group_stats', pd.DataFrame()))}
        </div>
        
        <footer>
            <p>Generated by Quant Backtest Platform</p>
        </footer>
    </div>
</body>
</html>
    """
    
    # 保存 HTML 文件
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    
    print(f"[OK] 报告已生成: {output_path}")
    return output_path

