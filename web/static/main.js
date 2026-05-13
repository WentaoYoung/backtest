/**
 * 量化回测系统 - 前端逻辑
 * 处理参数提交、数据获取、图表渲染
 */

// ========================================
// 全局状态
// ========================================

const state = {
    isLoading: false,
    isLoadingFactors: false,  // 因子数据加载中
    dataRange: null,
    results: null,
    currentViewMode: 'pure_long',  // 'pure_long' 或 'long_short'
    benchmarkData: null,  // 存储基准对比数据
    charts: {
        nav: null,
        longShort: null,
        benchmarkComparison: null,  // 基准对比图表
        icDecay: null,
        icDist: null,
        icCum: null,
        icAutocorr: null
    },
    // 因子相关性分析状态（重构版）
    correlationCharts: {
        main: null  // 单一图表，切换显示不同内容
    },
    correlationResults: null,  // 存储相关性分析结果
    currentCorrelationView: 'factor_correlation',  // 当前显示的图表类型
    // 因子库状态
    factorLibrary: {
        connected: false,
        factors: [],
        categories: [],
        dateRange: { min: null, max: null },
        tickerCount: 0
    },
       progress: {
        requestId: null,
        pollTimer: null,
        targetProgress: 0,      // 后端发来的真实目标进度
        displayProgress: 0,     // 当前显示在界面上的进度（用于动画）
        smoothTimer: null,      // 平滑动画定时器
        finishing: false        // 任务已结束，正平滑收束到 100%
    },
    csvUpload: {
        data: null,
        fileName: null,
        parsed: null
    }
};


// 图表颜色配置
const chartColors = {
    groups: ['#ef4444', '#f97316', '#eab308', '#22c55e', '#3b82f6', '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16', '#f43f5e'],
    longShort: '#8b5cf6',
    ic: '#3b82f6',
    icPositive: '#22c55e',
    icNegative: '#ef4444'
};

/** 图表横轴日期：只显示到日（YYYY-MM-DD），去掉 T00:00:00 或空格后的时间 */
function formatChartAxisDate(v) {
    if (v == null || v === '') return '';
    const s = String(v).trim();
    const t = s.indexOf('T');
    if (t >= 0) return s.slice(0, t);
    const sp = s.indexOf(' ');
    if (sp >= 0 && /^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, sp);
    return s;
}

// Chart.js 全局配置
Chart.defaults.color = '#9898a8';
Chart.defaults.borderColor = '#2a2a3a';
Chart.defaults.font.family = "'JetBrains Mono', 'Consolas', monospace";

// ========================================
// 初始化
// ========================================

document.addEventListener('DOMContentLoaded', () => {
    if (!document.getElementById('backtest-form')) {
        return;
    }
    initApp();
});

async function initApp() {
    // 绑定表单提交
    const form = document.getElementById('backtest-form');
    form.addEventListener('submit', handleFormSubmit);
    
    // 初始化日期验证
    initDateValidation();
    
    // 加载数据范围（带加载状态显示）
    await loadDataRange();
    
    // 初始化因子库
    await initFactorLibrary();
    //本地因子库上传
    initCSVUpload();
    // 初始化因子相关性分析模块
    initCorrelationAnalysis();
}

/**
 * 计算超额收益
 */
function calculateExcessReturns(groupNav, benchmarkNav) {
    // 将基准净值转换为对象，便于查找
    const benchmarkMap = {};
    benchmarkNav.forEach(row => {
        benchmarkMap[row.trade_dt] = row.benchmark_nav;
    });
    
    // 计算每个分组的超额净值
    const excessNav = groupNav.map(row => {
        const excessRow = { trade_dt: row.trade_dt };
        const benchmark = benchmarkMap[row.trade_dt] || 1.0;
        
        Object.keys(row).forEach(key => {
            if (key !== 'trade_dt') {
                // 超额净值 = 分组净值 / 基准净值
                excessRow[key] = row[key] / benchmark;
            }
        });
        
        return excessRow;
    });
    
    return excessNav;
}

// ========================================
// 数据加载
// ========================================

async function loadDataRange() {
    // 显示加载状态
    showFactorLoading('正在加载价格数据范围...');
    
    try {
        const response = await fetch('/api/data_range');
        const json = await response.json();
        
        if (json.success) {
            state.dataRange = json.data;
            
            // 注意：不再自动设置默认日期，让用户自己选择
            // 只在日期框为空时设置一个合理的默认值提示
            const startDateInput = document.getElementById('start_date');
            const endDateInput = document.getElementById('end_date');
            
            // 设置日期输入框的min/max属性，限制可选范围
            startDateInput.min = json.data.min_date;
            startDateInput.max = json.data.max_date;
            endDateInput.min = json.data.min_date;
            endDateInput.max = json.data.max_date;
            
            // 设置placeholder提示（虽然date类型不支持placeholder，但可以通过title提示）
            startDateInput.title = `可选范围: ${json.data.min_date} ~ ${json.data.max_date}`;
            endDateInput.title = `可选范围: ${json.data.min_date} ~ ${json.data.max_date}`;
            
            // 如果是首次加载，可以设置一个近期的默认范围
            if (!startDateInput.value && !endDateInput.value) {
                // 默认设置为数据范围的最近一段时间
                startDateInput.value = json.data.min_date;
                endDateInput.value = json.data.max_date;
            }
            
            // 更新数据信息
            document.getElementById('data-info').innerHTML = 
                `价格数据范围: ${json.data.min_date} ~ ${json.data.max_date}<br>` +
                `股票数量: ${json.data.n_tickers} 只`;
            
            setStatus('数据加载完成，请选择日期范围后开始回测', 'success');
        } else {
            setStatus('数据加载失败: ' + json.error, 'error');
        }
    } catch (error) {
        setStatus('无法连接服务器', 'error');
        console.error(error);
    } finally {
        hideFactorLoading();
    }
}

// ========================================
// 加载状态控制
// ========================================

/**
 * 显示因子加载中状态
 */
function showFactorLoading(text = '正在加载因子数据...') {
    const overlay = document.getElementById('factor-loading-overlay');
    const loadingText = document.getElementById('loading-text');
    if (overlay) {
        overlay.style.display = 'flex';
        if (loadingText) loadingText.textContent = text;
    }
    state.isLoadingFactors = true;
}

/**
 * 隐藏因子加载中状态
 */
function hideFactorLoading() {
    const overlay = document.getElementById('factor-loading-overlay');
    if (overlay) {
        overlay.style.display = 'none';
    }
    state.isLoadingFactors = false;
}

// ========================================
// 日期验证
// ========================================

/**
 * 初始化日期验证
 */
function initDateValidation() {
    const startDateInput = document.getElementById('start_date');
    const endDateInput = document.getElementById('end_date');
    
    if (!startDateInput || !endDateInput) return;
    
    // 监听日期变化
    startDateInput.addEventListener('change', validateDates);
    endDateInput.addEventListener('change', validateDates);
}

/**
 * 验证日期范围
 */
function validateDates() {
    const startDate = document.getElementById('start_date').value;
    const endDate = document.getElementById('end_date').value;
    const hintEl = document.getElementById('date-validation-hint');
    const hintText = document.getElementById('date-validation-text');
    
    if (!hintEl || !startDate || !endDate) {
        if (hintEl) hintEl.style.display = 'none';
        return true;
    }
    
    let isValid = true;
    let messages = [];
    
    // 检查日期顺序
    if (new Date(startDate) >= new Date(endDate)) {
        messages.push('起始日期必须早于结束日期');
        isValid = false;
    }
    
    // 仅支持因子库：检查日期是否在有效范围内
    if (state.factorLibrary.connected) {
        const selectedTable = document.getElementById('factor_table')?.value;
        const tableInfo = selectedTable && state.factorLibrary?.tables
            ? state.factorLibrary.tables.find(t => t.name === selectedTable)
            : null;
        
        const dbMinDate = tableInfo?.date_range?.min || state.factorLibrary.dateRange.min;
        const dbMaxDate = tableInfo?.date_range?.max || state.factorLibrary.dateRange.max;
        
        if (dbMinDate && new Date(startDate) < new Date(dbMinDate)) {
            messages.push(`起始日期早于因子库最早日期 (${dbMinDate})`);
            isValid = false;
        }
        if (dbMaxDate && new Date(endDate) > new Date(dbMaxDate)) {
            messages.push(`结束日期晚于因子库最晚日期 (${dbMaxDate})`);
            isValid = false;
        }
    }
    
    // 显示或隐藏提示
    if (messages.length > 0) {
        hintEl.style.display = 'flex';
        hintText.textContent = messages.join('；');
        hintEl.className = isValid ? 'date-validation-hint' : 'date-validation-hint error';
    } else {
        hintEl.style.display = 'none';
    }
    
    return isValid;
}

// ========================================
// 因子库初始化
// ========================================

/**
 * 初始化因子库连接
 */
async function initFactorLibrary() {
    const statusEl = document.getElementById('db-status');
    
    try {
        showFactorLoading('正在连接因子库...');
        
        const response = await fetch('/api/factor_library');
        const json = await response.json();
        
        console.log('因子库API返回:', json);
        
        if (json.success && json.data.connected) {
            state.factorLibrary = {
                connected: true,
                factors: json.data.factors || [],
                categories: [],
                dateRange: json.data.date_range || {},
                tickerCount: json.data.ticker_count || 0
            };
            
            // 更新状态指示器
            statusEl.classList.remove('disconnected');
            statusEl.classList.add('connected');
            statusEl.title = '因子库已连接';
            
            // 先更新因子库信息（在 loadFactorTables 之前）
            const factorCountEl = document.getElementById('factor-count');
            const tickerCountEl = document.getElementById('db-ticker-count');
            const dateRangeEl = document.getElementById('db-date-range');
            
            if (factorCountEl) factorCountEl.textContent = (json.data.factors || []).length;
            if (tickerCountEl) tickerCountEl.textContent = (json.data.ticker_count || 0).toLocaleString();
            if (dateRangeEl) dateRangeEl.textContent = 
                `${json.data.date_range?.min || '-'} ~ ${json.data.date_range?.max || '-'}`;
            
            console.log('因子库已连接，开始加载因子表...');
            
            // 加载因子表列表（可能较慢）
            try {
                await loadFactorTables();
                console.log('因子表加载完成');
            } catch (tableError) {
                console.error('加载因子表失败:', tableError);
            }
            
            console.log('因子库初始化完成');
            const startDateInput = document.getElementById('start_date');
            const endDateInput = document.getElementById('end_date');
            if (!startDateInput.value && json.data.date_range?.min) {
                startDateInput.value = json.data.date_range.min;
            }
            if (!endDateInput.value && json.data.date_range?.max) {
                endDateInput.value = json.data.date_range.max;
            }
        } else {
            statusEl.classList.remove('connected');
            statusEl.classList.add('disconnected');
            statusEl.title = '因子库未连接: ' + (json.data?.message || json.error || '未知错误');
            console.log('因子库未连接:', json.data?.message);
        }
    } catch (error) {
        statusEl.classList.remove('connected');
        statusEl.classList.add('disconnected');
        statusEl.title = '因子库连接失败';
        console.error('因子库连接错误:', error);
    } finally {
        hideFactorLoading();
    }
}

/**
 * 加载因子表列表
 */
async function loadFactorTables() {
    try {
        const response = await fetch('/api/factor_tables');
        const json = await response.json();
        
        if (json.success) {
            state.factorLibrary.tables = json.data;
            
            // 填充因子表下拉框（主界面）
            const tableSelect = document.getElementById('factor_table');
            if (tableSelect) {
                tableSelect.innerHTML = '<option value="">请选择因子表...</option>';
                json.data.forEach(table => {
                    const option = document.createElement('option');
                    option.value = table.name;
                    option.textContent = `${table.name} (${table.factor_count} 个因子)`;
                    option.title = `日期范围: ${table.date_range.min || '-'} ~ ${table.date_range.max || '-'}, 股票数: ${table.ticker_count}`;
                    tableSelect.appendChild(option);
                });
            }
            
            // 填充因子表下拉框（相关性分析界面 - 源因子表）
            const correlationTableSelect = document.getElementById('correlation_factor_table');
            if (correlationTableSelect) {
                correlationTableSelect.innerHTML = '<option value="">请选择因子表...</option>';
                json.data.forEach(table => {
                    const option = document.createElement('option');
                    option.value = table.name;
                    option.textContent = `${table.name} (${table.factor_count} 个因子)`;
                    option.title = `日期范围: ${table.date_range.min || '-'} ~ ${table.date_range.max || '-'}, 股票数: ${table.ticker_count}`;
                    correlationTableSelect.appendChild(option);
                });
            }
            
            // 填充因子表下拉框（相关性分析界面 - 对比因子表）
            const correlationTargetTableSelect = document.getElementById('correlation_target_table');
            if (correlationTargetTableSelect) {
                correlationTargetTableSelect.innerHTML = '<option value="">请选择对比因子表...</option>';
                json.data.forEach(table => {
                    const option = document.createElement('option');
                    option.value = table.name;
                    option.textContent = `${table.name} (${table.factor_count} 个因子)`;
                    option.title = `日期范围: ${table.date_range.min || '-'} ~ ${table.date_range.max || '-'}, 股票数: ${table.ticker_count}`;
                    correlationTargetTableSelect.appendChild(option);
                });
            }
            
            // 绑定表选择事件
            bindTableSelectEvents();
        }
    } catch (error) {
        console.error('加载因子表列表失败:', error);
    }
}

/**
 * 绑定因子表选择事件
 */
function bindTableSelectEvents() {
    // 主界面的表选择
    const tableSelect = document.getElementById('factor_table');
    if (tableSelect) {
        tableSelect.addEventListener('change', async (e) => {
            const tableName = e.target.value;
            if (tableName) {
                await loadFactorsForTable(tableName, 'factor_name');
                // 更新提示信息
                const tableInfo = state.factorLibrary.tables.find(t => t.name === tableName);
                const hintEl = document.getElementById('factor-table-hint');
                if (hintEl && tableInfo) {
                    hintEl.textContent = `日期范围: ${tableInfo.date_range.min || '-'} ~ ${tableInfo.date_range.max || '-'}, 股票数: ${tableInfo.ticker_count}`;
                    hintEl.style.display = 'block';
                }
            } else {
                const factorSelect = document.getElementById('factor_name');
                if (factorSelect) {
                    factorSelect.innerHTML = '<option value="">请先选择因子表...</option>';
                    factorSelect.disabled = true;
                }
                const hintEl = document.getElementById('factor-table-hint');
                if (hintEl) hintEl.style.display = 'none';
            }
        });
    }
    
    // 相关性分析界面的表选择
    const correlationTableSelect = document.getElementById('correlation_factor_table');
    if (correlationTableSelect) {
        correlationTableSelect.addEventListener('change', async (e) => {
            const tableName = e.target.value;
            if (tableName) {
                await loadFactorsForTable(tableName, 'correlation_factor_name');
            } else {
                const factorSelect = document.getElementById('correlation_factor_name');
                if (factorSelect) {
                    factorSelect.innerHTML = '<option value="">请先选择因子表...</option>';
                    factorSelect.disabled = true;
                }
            }
        });
    }

    // 检测新增因子表按钮
    const detectBtn = document.getElementById('detect_new_tables_btn');
    if (detectBtn) {
        detectBtn.addEventListener('click', async () => {
            const resultEl = document.getElementById('detect-new-tables-result');
            resultEl.textContent = '正在检测...';
            resultEl.style.display = 'block';

            try {
                const response = await fetch('/api/detect_new_tables?force_rescan=true');
                const json = await response.json();

                if (json.success && json.data.length > 0) {
                    const tables = json.data.map(t =>
                        `${t.name} (${t.factor_count} 个因子)`
                    ).join(', ');
                    resultEl.innerHTML = `发现新表: ${tables}`;
                } else {
                    resultEl.textContent = '没有发现新增的因子表';
                }
            } catch (e) {
                resultEl.textContent = '检测失败: ' + e.message;
            }
        });
    }
}

/**
 * 加载指定表的因子列表
 */
async function loadFactorsForTable(tableName, selectId) {
    try {
        const response = await fetch(`/api/available_factors?table_name=${encodeURIComponent(tableName)}`);
        const json = await response.json();
        
        if (json.success) {
            const factorSelect = document.getElementById(selectId);
            if (factorSelect) {
                factorSelect.innerHTML = '<option value="">请选择因子...</option>';
                json.data.forEach(factor => {
                    const option = document.createElement('option');
                    option.value = factor.value;
                    option.textContent = `${factor.value} - ${factor.label}`;
                    option.title = factor.description || '';
                    factorSelect.appendChild(option);
                });
                factorSelect.disabled = false;
            }
        }
    } catch (error) {
        console.error(`加载表 ${tableName} 的因子列表失败:`, error);
    }
}

// ========================================
// CSV 上传处理
// ========================================
// CSV 上传处理
// ========================================

function handleCSVFileSelect(e) {
    const file = e.target.files[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.csv')) {
        setStatus('请上传 .csv 格式的文件', 'error');
        return;
    }
    const reader = new FileReader();
    reader.onload = async function(event) {
        const csvText = event.target.result;
        state.csvUpload.data = csvText;
        state.csvUpload.fileName = file.name;
        try {
            setStatus('正在解析 ' + file.name + ' ...', 'info');
            const response = await fetch('/api/parse_local_csv', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ csv_data: csvText })
            });
            const json = await response.json();
            if (json.success) {
                state.csvUpload.parsed = json.data;
                const d = json.data;
                setStatus('✅ ' + file.name + ' | ' + (d.format === 'wide' ? '宽表' : '长表') +
                    ' | ' + d.n_dates + '天 × ' + d.n_tickers + '只股票 | ' +
                    d.min_date + ' ~ ' + d.max_date + ' | 覆盖率 ' + d.factor_coverage_pct + '%', 'success');
                const startDateInput = document.getElementById('start_date');
                const endDateInput = document.getElementById('end_date');
                if (!startDateInput.value) startDateInput.value = d.min_date;
                if (!endDateInput.value) endDateInput.value = d.max_date;

                // 隐藏因子库选择区域
                const factorTableGroup = document.getElementById('factor-table-group');
                const factorNameGroup = document.getElementById('factor-select-group-main');
                if (factorTableGroup) factorTableGroup.style.display = 'none';
                if (factorNameGroup) factorNameGroup.style.display = 'none';
            } else {
                setStatus('CSV 解析失败: ' + json.error, 'error');
                state.csvUpload.data = null;
                state.csvUpload.fileName = null;
            }
        } catch (err) {
            setStatus('CSV 解析失败: ' + err.message, 'error');
            state.csvUpload.data = null;
            state.csvUpload.fileName = null;
        }
    };
    reader.readAsText(file);
}

function clearCSVUpload() {
    state.csvUpload.data = null;
    state.csvUpload.fileName = null;
    state.csvUpload.parsed = null;
    const fileInput = document.getElementById('csv_file_input');
    if (fileInput) fileInput.value = '';

    // 恢复因子库选择区域 (注意这里必须是 block，之前你写成了 none)
    const factorTableGroup = document.getElementById('factor-table-group');
    const factorNameGroup = document.getElementById('factor-select-group-main');
    if (factorTableGroup) factorTableGroup.style.display = 'block';
    if (factorNameGroup) factorNameGroup.style.display = 'block';

    document.getElementById('factor_table').disabled = false;
    document.getElementById('factor_name').disabled = false;
    setStatus('已清除 CSV 数据，将使用因子库模式', 'info');
}

function initCSVUpload() {
    // 防御 1：如果找不到上传框，直接撤退
    const fileInput = document.getElementById('csv_file_input');
    if (!fileInput) {
        console.warn('未找到 csv_file_input 元素，跳过 CSV 初始化');
        return;
    }

    fileInput.addEventListener('change', handleCSVFileSelect);

    const clearBtn = document.getElementById('csv_clear_btn');
    if (clearBtn) clearBtn.addEventListener('click', clearCSVUpload);

    // 防御 2：如果已经生成过按钮，不要重复生成
    if (document.getElementById('download-template-btn')) {
        return;
    }

    // 防御 3：极其安全的父容器查找逻辑
    let uploadContainer = fileInput.closest('.csv-upload-group');
    if (!uploadContainer) {
        uploadContainer = fileInput.parentElement;
    }
    if (!uploadContainer) {
        // 如果连父节点都找不到，直接把按钮塞进 body 里凑合用
        uploadContainer = document.body;
    }

    try {
        const templateWrapper = document.createElement('div');
        templateWrapper.style.cssText = 'margin-bottom: 10px; display: flex; gap: 10px; align-items: center;';

        const hintText = document.createElement('span');
        hintText.style.cssText = 'font-size: 12px; color: #94a3b8;';
        hintText.textContent = '不确定格式？下载官方模板：';

        const btnLong = document.createElement('button');
        btnLong.id = 'download-template-btn';
        btnLong.textContent = '📥 下载长表模板(推荐)';
        btnLong.style.cssText = 'background-color: #0f3460; color: #60a5fa; border: 1px solid #1e40af; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px;';
        btnLong.onclick = function() {
            downloadCSVTemplate('long');
        };

        const btnWide = document.createElement('button');
        btnWide.textContent = '📥 下载宽表模板';
        btnWide.style.cssText = 'background-color: #0f3460; color: #94a3b8; border: 1px solid #334155; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px;';
        btnWide.onclick = function() {
            downloadCSVTemplate('wide');
        };

        templateWrapper.appendChild(hintText);
        templateWrapper.appendChild(btnLong);
        templateWrapper.appendChild(btnWide);

        uploadContainer.insertBefore(templateWrapper, uploadContainer.firstChild);
    } catch (error) {
        console.error('生成下载模板按钮时发生错误:', error);
    }
}
/**
 * 生成并下载 CSV 模板
 */
function downloadCSVTemplate(type) {
    let csvContent = '';
    let fileName = '';

    if (type === 'long') {
        fileName = '因子上传模板_长表格式.csv';
        csvContent = `trade_dt,ticker,factor
2023-01-03,000001.SZ,0.0521
2023-01-03,000002.SZ,-0.0132
2023-01-03,600036.SH,0.0089
2023-01-03,300750.SZ,-0.0451
2023-01-04,000001.SZ,0.0535
2023-01-04,000002.SZ,-0.0128
2023-01-04,600036.SH,0.0095
2023-01-04,300750.SZ,-0.0432
2023-01-05,000001.SZ,0.0519
2023-01-05,000002.SZ,-0.0141`;
    } else {
        fileName = '因子上传模板_宽表格式.csv';
        csvContent = `trade_dt,000001.SZ,000002.SZ,600036.SH,300750.SZ
2023-01-03,0.0521,-0.0132,0.0089,-0.0451
2023-01-04,0.0535,-0.0128,0.0095,-0.0432
2023-01-05,0.0519,-0.0141,0.0088,-0.0460`;
    }

    const blob = new Blob(['\ufeff' + csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = fileName;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(link.href);

    setStatus(`✅ 已下载 ${type === 'long' ? '长表' : '宽表'} 模板，请按格式填写数据后上传`, 'success');
}

// ========================================
// 表单处理
// ========================================

async function handleFormSubmit(e) {
    e.preventDefault();

    console.log('[回测] 表单提交开始');
    console.log('[回测] state.isLoading =', state.isLoading);

    if (state.isLoading) {
        console.log('[回测] 已在加载中，直接返回');
        return;
    }

      // 判断数据源：CSV 上传 or 因子库
    const dataSource = state.csvUpload.data ? 'csv' : 'database';
    console.log('[回测] 数据源=', dataSource);

    // 收集表单数据
    const requestId = `bt_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const formData = {
        start_date: document.getElementById('start_date').value,
        end_date: document.getElementById('end_date').value,
        rebalance_freq: document.getElementById('rebalance_freq').value,
        n_groups: parseInt(document.getElementById('n_groups').value),
        weight_method: document.getElementById('weight_method').value,
        initial_capital: parseFloat(document.getElementById('initial_capital').value),
        transaction_cost: parseFloat(document.getElementById('transaction_cost').value),
        slippage: parseFloat(document.getElementById('slippage').value),
        risk_free_rate: parseFloat(document.getElementById('risk_free_rate').value),
        benchmark: document.getElementById('benchmark').value,
        allow_short: document.getElementById('allow_short').checked,
        request_id: requestId
    };

    // 验证日期
    if (!formData.start_date || !formData.end_date) {
        console.log('[回测] 日期未选择，返回');
        setStatus('请选择回测日期范围', 'error');
        return;
    }

    console.log('[回测] 日期范围:', formData.start_date, '~', formData.end_date);
    if (!formData.start_date || !formData.end_date) {
        setStatus('请选择起始日期和结束日期', 'error');
        return;
    }
        // ========== CSV 模式 ==========
    if (dataSource === 'csv') {
        formData.csv_data = state.csvUpload.data;
        setLoading(true);
        startProgressPolling(requestId);
        setStatus('正在执行回测... (CSV: ' + state.csvUpload.fileName + ')', 'info');
        try {
            const response = await fetch('/api/run_backtest_csv', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(formData)
            });
            const json = await response.json();
            if (json.success) {
                state.results = json.data;
                setStatus('回测完成 (' + json.data.elapsed_time + 's)', 'success');
                renderResults(json.data);
                completeProgress('完成', '数据加载完毕', '0 秒');
            } else {
                setStatus('回测失败: ' + json.error, 'error');
                completeProgress('失败', json.error || '回测失败', '--');
            }
        } catch (error) {
            setStatus('请求失败: ' + error.message, 'error');
            completeProgress('失败', error.message || '请求失败', '--');
        } finally {
            setLoading(false);
            stopProgressPolling();
        }
        return;
    }


    // 执行日期验证
    const dateValid = validateDates();
    console.log('[回测] 日期验证结果:', dateValid);
    if (!dateValid) {
        setStatus('日期范围无效，请根据提示调整', 'error');
        return;
    }

    // 仅支持因子库：添加因子名称和表名
    formData.factor_name = document.getElementById('factor_name').value;
    formData.table_name = document.getElementById('factor_table').value;

    if (!formData.table_name || !formData.factor_name) {
        setStatus('请选择因子表和因子', 'error');
        return;
    }

    // 检查因子库是否连接
    if (!state.factorLibrary.connected) {
        setStatus('因子库未连接，请检查网络', 'error');
        return;
    }

    // 检查日期是否超出因子库范围（更严格的校验由 validateDates 负责）
    const dbMaxDate = state.factorLibrary.dateRange.max;
    if (dbMaxDate && new Date(formData.end_date) > new Date(dbMaxDate)) {
        setStatus(`结束日期 (${formData.end_date}) 超出因子库数据范围 (截止到 ${dbMaxDate})，请调整日期`, 'error');
        return;
    }

    // 开始加载
    console.log('[回测] 准备发送请求...');
    setLoading(true);
    startProgressPolling(requestId);

    // 固定使用数据库因子回测接口
    const apiEndpoint = '/api/run_backtest_db';
    const factorInfo = ` (因子: ${formData.factor_name})`;
    console.log('[回测] API端点:', apiEndpoint);
    setStatus('正在执行回测...' + factorInfo, 'info');

    try {
        console.log('[回测] 发送fetch请求...');
        const response = await fetch(apiEndpoint, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(formData)
        });

        const json = await response.json();

        if (json.success) {
            state.results = json.data;

            // 构建成功消息（包含回测耗时）
            let message = '回测完成！';
            if (json.data.elapsed_time) {
                message += ` 总耗时 ${json.data.elapsed_time.toFixed(2)} 秒`;

                // 如果有详细的时间分解，添加到消息中
                if (json.data.time_breakdown) {
                    const tb = json.data.time_breakdown;
                    message += ` (回测引擎: ${tb.backtest_engine.toFixed(1)}s, 因子分析: ${tb.factor_analysis.toFixed(1)}s)`;
                }
            }

            completeProgress('完成', '回测完成', '0 秒');

            setStatus(message, 'success');

            // 在控制台打印详细的时间统计（供开发调试）
            if (json.data.time_breakdown) {
                console.log('回测时间分解:', json.data.time_breakdown);
            }

            renderResults(json.data);
        } else {
            setStatus('回测失败: ' + json.error, 'error');
            completeProgress('失败', json.error || '回测失败', '--');
        }
    } catch (error) {
        setStatus('请求失败: ' + error.message, 'error');
        console.error(error);
        completeProgress('失败', error.message || '请求失败', '--');
    } finally {
        setLoading(false);
        stopProgressPolling();
    }
}

// ========================================
// UI 状态控制
// ========================================

function formatEta(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return '--';
    seconds = Math.round(seconds);
    if (seconds < 60) return `${Math.max(1, seconds)} 秒`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m} 分 ${s} 秒`;
}

 function showProgressPanel() {
        const panel = document.getElementById('progress-panel');
        if (panel) panel.style.display = 'block';
        // 重置状态
        state.progress.targetProgress = 0;
        state.progress.displayProgress = 0;
        state.progress.finishing = false;
        if (state.progress.smoothTimer) clearInterval(state.progress.smoothTimer);
        updateProgressBarUI(0, '准备中', '任务已提交', '--');
        startSmoothAnimation(); // 启动动画循环
    }

function hideProgressPanel() {
        const panel = document.getElementById('progress-panel');
        if (panel) panel.style.display = 'none';
        if (state.progress.smoothTimer) {
            clearInterval(state.progress.smoothTimer);
            state.progress.smoothTimer = null;
        }
    }

function updateProgressPanel(progressData) {
    const progress = Math.max(0, Math.min(100, Number(progressData.progress || 0)));
    const stage = progressData.stage || '执行中';
    const detail = progressData.detail || stage;
    const eta = formatEta(progressData.eta_seconds);

    const bar = document.getElementById('progress-bar');
    const percent = document.getElementById('progress-percent');
    const stageEl = document.getElementById('progress-stage');
    const detailEl = document.getElementById('progress-detail');
    const etaEl = document.getElementById('progress-eta');

    if (bar) bar.style.width = `${progress}%`;
    if (percent) percent.textContent = `${progress}%`;
    if (stageEl) stageEl.textContent = stage;
    if (detailEl) detailEl.textContent = detail;
    if (etaEl) etaEl.textContent = eta;
}

function stopProgressPolling() {
        if (state.progress.pollTimer) {
            clearInterval(state.progress.pollTimer);
            state.progress.pollTimer = null;
        }
    }

/**
 * 任务结束时的进度收口：不瞬间跳满，由平滑动画追到 100% 后再停表。
 */
function completeProgress(stage = '完成', detail = '', eta = '0 秒') {
    state.progress.targetProgress = 100;
    state.progress.finishing = true;
    updateProgressBarUI(stage, detail || stage, eta);
    stopProgressPolling();
    if (!state.progress.smoothTimer) {
        startSmoothAnimation();
    }
}

 function startProgressPolling(requestId) {
        state.progress.requestId = requestId;
        showProgressPanel();
        stopProgressPolling();

        let isFetching = false; // 【核心】防抖锁，防止请求积压卡死界面

        state.progress.pollTimer = setInterval(async () => {
            // 如果上一次请求还没回来，直接跳过，绝不并发
            if (isFetching) return;
            isFetching = true;

            try {
                const resp = await fetch(`/api/progress/${encodeURIComponent(requestId)}`);
                const json = await resp.json();
                if (!json.success || !json.data) return;

                const data = json.data;
                // 更新后端给的真实目标进度
                state.progress.targetProgress = Math.min(100, Number(data.progress || 0));

                // 直接使用后端算好的 ETA
                const etaStr = data.eta_seconds != null ? formatEta(data.eta_seconds) : '--';

                // 如果完成或失败
                if (data.status === 'completed' || data.status === 'error') {
                    completeProgress(
                        data.status === 'error' ? '失败' : '完成',
                        data.detail || '',
                        '0 秒'
                    );
                } else {
                    // 只更新文字信息，进度条的视觉移动交给动画函数
                    updateProgressBarUI(state.progress.targetProgress, data.stage, data.detail, etaStr);
                }
            } catch (err) {
                // 静默失败
            } finally {
                // 无论成功失败，必须释放锁
                isFetching = false;
            }
        }, 1000); // 每秒问后端要一次数据
    }

    //* 核心：丝滑动画，让进度条慢慢追上目标值

    function startSmoothAnimation() {
        if (state.progress.smoothTimer) clearInterval(state.progress.smoothTimer);

        // 每 50ms 刷新一次（约 20帧/秒，肉眼极度平滑）
        state.progress.smoothTimer = setInterval(() => {
            const target = state.progress.targetProgress;
            const current = state.progress.displayProgress;

            // 已追上目标：若正在收尾到 100%，则定格并停表
            if (current >= target) {
                if (state.progress.finishing) {
                    state.progress.displayProgress = 100;
                    const bar = document.getElementById('progress-bar');
                    const percent = document.getElementById('progress-percent');
                    if (bar) bar.style.width = '100%';
                    if (percent) percent.textContent = '100%';
                    clearInterval(state.progress.smoothTimer);
                    state.progress.smoothTimer = null;
                    state.progress.finishing = false;
                }
                return;
            }

            // 计算步长：差距越大跑得越快，越接近越慢（先快后慢的视觉欺骗）
            const gap = target - current;
            let step;
            if (state.progress.finishing && target >= 99) {
                // 收尾阶段：略加快最后一段，仍保持连续变化
                if (gap > 15) step = gap * 0.22;
                else if (gap > 3) step = Math.max(0.9, gap * 0.18);
                else step = Math.max(0.45, gap * 0.35);
            } else {
                if (gap > 20) step = gap * 0.2;
                else if (gap > 5) step = gap * 0.1;
                else step = 0.5;
            }

            state.progress.displayProgress = Math.min(target, current + step);

            // 只更新 DOM 的宽度和百分比数字（不碰文字，防止文字闪烁）
            const bar = document.getElementById('progress-bar');
            const percent = document.getElementById('progress-percent');
            if (bar) bar.style.width = `${state.progress.displayProgress}%`;
            if (percent) percent.textContent = `${Math.floor(state.progress.displayProgress)}%`;

        }, 50);
    }

    /**
     * 纯粹更新文字状态（不干涉进度条宽度）
     */
    function updateProgressBarUI(progress, stage, detail, eta) {
        const stageEl = document.getElementById('progress-stage');
        const detailEl = document.getElementById('progress-detail');
        const etaEl = document.getElementById('progress-eta');
        if (stageEl) stageEl.textContent = stage;
        if (detailEl) detailEl.textContent = detail;
        if (etaEl) etaEl.textContent = eta;
    }
function setStatus(message, type = 'info') {
    const el = document.getElementById('status-message');
    el.textContent = message;
    el.className = `status-message ${type}`;
}

function setLoading(isLoading) {
    state.isLoading = isLoading;
    const btn = document.getElementById('btn-run');
    const btnText = btn.querySelector('.btn-text');
    const btnLoading = btn.querySelector('.btn-loading');

    if (isLoading) {
        btnText.style.display = 'none';
        btnLoading.style.display = 'flex';
        btn.disabled = true;
    } else {
        btnText.style.display = 'block';
        btnLoading.style.display = 'none';
        btn.disabled = false;
    }
}

// ========================================
// 结果渲染
// ========================================

function renderResults(data) {
    showDownloadButton();

    const btnDl = document.getElementById('btn-download-report');
    if (btnDl) btnDl.style.display = 'block';

    document.getElementById('factor-summary-section').style.display = 'block';
    document.getElementById('group-analysis-section').style.display = 'block';
    document.getElementById('ic-analysis-section').style.display = 'block';

    // 显示相关性分析板块（仅单因子主页存在完整控件时）
    const corrSec = document.getElementById('correlation-section');
    if (corrSec && document.getElementById('btn-analyze-correlation')) {
        corrSec.style.display = 'block';
    }

    // 检查是否有基准数据，如果有则显示基准对比区域
    if (data.benchmark_nav && data.benchmark_nav.length > 0) {
        const benchmarkSection = document.getElementById('benchmark-comparison-section');
        if (benchmarkSection) benchmarkSection.style.display = 'block';
    }

    // 渲染各个部分
    renderMetrics(data);
    renderNavChart(data.group_nav);
    initNavToggle();  // 初始化绝对/超额收益切换按钮
    renderGroupStatsTable(data.group_stats);
    renderYearlyStatsTable(data.yearly_ic);
    renderLongShortChart(data.long_short);
    renderLongShortStats(data.long_short);
    renderICAnalysis(data.ic_analysis);

    // 如果有基准数据，渲染基准对比
    if (data.benchmark_nav && data.benchmark_nav.length > 0) {
        renderBenchmarkComparison(data);
    }

    // 初始化基准切换按钮
    initBenchmarkToggle();

    // 更新相关性分析按钮状态（因为现在有了回测数据）
    updateCorrelationButtonAfterBacktest();
}

// ========================================
// 统计指标渲染
// ========================================

function renderMetrics(data) {
    const stats = data.group_stats;
    const summary = data.factor_summary;
    const icStats = data.ic_analysis?.statistics || {};

    // 与 BacktestEngine 一致：G1 = 因子值最高组，G{n} = 最低组；多空 = G1 - G{n}
    const topGroup = stats[0];

    // 因子收益（多空收益）
    const ls = data.long_short;
    const factorReturn = ls.length > 0
        ? ((ls[ls.length - 1]['Long-Short'] - 1) * 100).toFixed(2) + '%'
        : '--';
    document.getElementById('metric-factor-return').textContent = factorReturn;

    // 夏普比率
    document.getElementById('metric-sharpe').textContent =
        topGroup ? topGroup['夏普比率']?.toFixed(2) || '--' : '--';

    // 年化收益
    document.getElementById('metric-annual-return').textContent =
        topGroup ? (topGroup['年化收益_%']?.toFixed(2) + '%') || '--' : '--';

    // IC
    document.getElementById('metric-ic').textContent =
        icStats.IC_mean ? icStats.IC_mean.toFixed(4) : (summary.IC_pearson?.toFixed(4) || '--');

    // Rank IC
    document.getElementById('metric-rank-ic').textContent =
        icStats.Rank_IC ? icStats.Rank_IC.toFixed(4) : (summary.IC_spearman?.toFixed(4) || '--');

    // 最大回撤
    document.getElementById('metric-max-dd').textContent =
        topGroup ? (topGroup['最大回撤_%']?.toFixed(2) + '%') || '--' : '--';
}

// ========================================
// 分组净值图表
// ========================================

// 当前净值图表视图模式：'absolute' 或 'excess'
let currentNavViewMode = 'absolute';

/**
 * 切换净值图表视图（绝对收益 vs 超额收益）
 * @param {string} mode - 'absolute' 或 'excess'
 */
function switchNavView(mode) {
    currentNavViewMode = mode;

    // 更新按钮状态
    const btnAbsolute = document.getElementById('btn-absolute-nav');
    const btnExcess = document.getElementById('btn-excess-nav');
    const chartTitle = document.getElementById('nav-chart-title');

    if (mode === 'absolute') {
        btnAbsolute?.classList.add('active');
        btnExcess?.classList.remove('active');
        if (chartTitle) chartTitle.textContent = '分组净值曲线';

        // 渲染绝对净值
        if (state.results && state.results.group_nav) {
            renderNavChart(state.results.group_nav);
        }
    } else if (mode === 'excess') {
        btnAbsolute?.classList.remove('active');
        btnExcess?.classList.add('active');
        if (chartTitle) chartTitle.textContent = '分组超额净值曲线 (相对基准)';

        // 计算并渲染超额净值
        if (state.results && state.results.group_nav && state.results.benchmark_nav) {
            const excessNav = calculateExcessReturns(state.results.group_nav, state.results.benchmark_nav);
            renderNavChart(excessNav, true);  // 第二个参数表示是超额收益模式
        }
    }
}

/**
 * 初始化净值图表切换按钮（有基准数据时显示）
 */
function initNavToggle() {
    const toggleGroup = document.getElementById('nav-toggle-group');
    if (!toggleGroup) return;

    // 调试日志
    console.log('[initNavToggle] state.results:', state.results ? 'exists' : 'null');
    console.log('[initNavToggle] benchmark_nav:', state.results?.benchmark_nav);
    console.log('[initNavToggle] excess_nav:', state.results?.excess_nav);

    // 只有在有基准数据时才显示切换按钮
    if (state.results && state.results.benchmark_nav && state.results.benchmark_nav.length > 0) {
        console.log('[initNavToggle] 显示切换按钮');
        toggleGroup.style.display = 'flex';
    } else {
        console.log('[initNavToggle] 隐藏切换按钮 (无基准数据)');
        toggleGroup.style.display = 'none';
    }

    // 重置为绝对收益模式
    currentNavViewMode = 'absolute';
    const btnAbsolute = document.getElementById('btn-absolute-nav');
    const btnExcess = document.getElementById('btn-excess-nav');
    btnAbsolute?.classList.add('active');
    btnExcess?.classList.remove('active');
}

function renderNavChart(navData, isExcessMode = false) {
    const ctx = document.getElementById('navChart').getContext('2d');

    // 销毁旧图表
    if (state.charts.nav) {
        state.charts.nav.destroy();
    }

    if (!navData || navData.length === 0) {
        return;
    }

    const labels = navData.map(row => formatChartAxisDate(row.trade_dt));

    // 修改这里：按数字顺序排序 G1, G2, G3, ..., G10
    const groups = Object.keys(navData[0])
        .filter(k => k !== 'trade_dt')
        .sort((a, b) => {
            // 提取 "G1", "G2" 中的数字部分进行比较
            const numA = parseInt(a.replace(/\D/g, ''));
            const numB = parseInt(b.replace(/\D/g, ''));
            return numA - numB;
        });

    const datasets = groups.map((group, idx) => ({
        label: group,
        data: navData.map(row => row[group]),
        borderColor: chartColors.groups[idx % chartColors.groups.length],
        backgroundColor: chartColors.groups[idx % chartColors.groups.length],
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.1
    }));

    state.charts.nav = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false
            },
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        usePointStyle: true,
                        padding: 20
                    }
                },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            if (isExcessMode) {
                                // 超额收益模式：显示百分比
                                return `${context.dataset.label}: ${((context.parsed.y - 1) * 100).toFixed(2)}%`;
                            }
                            return `${context.dataset.label}: ${context.parsed.y.toFixed(4)}`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: {
                        display: false
                    },
                    ticks: {
                        maxTicksLimit: 10
                    }
                },
                y: {
                    grid: {
                        color: '#2a2a3a'
                    },
                    title: isExcessMode ? {
                        display: true,
                        text: '超额净值 (相对基准)',
                        color: '#9898a8'
                    } : undefined,
                    ticks: {
                        callback: function(value) {
                            if (isExcessMode) {
                                // 超额收益模式：显示百分比
                                return ((value - 1) * 100).toFixed(0) + '%';
                            }
                            return value.toFixed(2);
                        }
                    }
                }
            }
        }
    });
}

// ========================================
// Long-Short 图表
// ========================================

function renderLongShortChart(lsData) {
    const ctx = document.getElementById('longShortChart').getContext('2d');

    if (state.charts.longShort) {
        state.charts.longShort.destroy();
    }

    if (!lsData || lsData.length === 0) {
        return;
    }

    const labels = lsData.map(row => formatChartAxisDate(row.trade_dt));
    const data = lsData.map(row => row['Long-Short']);

    state.charts.longShort = new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: 'Long-Short NAV (Top - Bottom)',
                data,
                borderColor: chartColors.longShort,
                backgroundColor: 'rgba(139, 92, 246, 0.1)',
                borderWidth: 2,
                pointRadius: 0,
                fill: true,
                tension: 0.1
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: true,
                    position: 'bottom'
                },
                title: {
                    display: true,
                    text: '因子多空组合净值 (最高分组 - 最低分组)',
                    color: '#9898a8'
                }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { maxTicksLimit: 6,
                             maxRotation: 45,      // 【新增】最大旋转45度
                             minRotation: 45,      // 【新增】最小旋转45度（强制倾斜）
                             color: '#9898a8'}
                },
                y: {
                    grid: { color: '#2a2a3a' },
                    title: {
                        display: true,
                        text: '净值',
                        color: '#9898a8'
                    }
                }
            }
        }
    });
}

/**
 * 渲染 Long-Short 统计表格
 */
function renderLongShortStats(lsData) {
    const tbody = document.getElementById('long-short-stats-body');
    if (!tbody || !lsData || lsData.length === 0) return;

    tbody.innerHTML = '';

    // 计算统计指标
    const navValues = lsData.map(row => row['Long-Short']).filter(v => v != null && !isNaN(v));
    if (navValues.length < 2) return;

    // 计算收益率序列
    const returns = [];
    for (let i = 1; i < navValues.length; i++) {
        returns.push(navValues[i] / navValues[i-1] - 1);
    }

    // 计算统计指标
    const meanReturn = returns.reduce((a, b) => a + b, 0) / returns.length;
    const stdReturn = Math.sqrt(returns.reduce((sum, r) => sum + Math.pow(r - meanReturn, 2), 0) / returns.length);

    const cumReturn = (navValues[navValues.length - 1] / navValues[0] - 1) * 100;
    const annReturn = meanReturn * 252 * 100;
    const annVol = stdReturn * Math.sqrt(252) * 100;
    const sharpe = annVol > 0 ? annReturn / annVol : 0;

    // 计算最大回撤
    let maxDD = 0;
    let peak = navValues[0];
    for (let i = 1; i < navValues.length; i++) {
        if (navValues[i] > peak) peak = navValues[i];
        const dd = (peak - navValues[i]) / peak;
        if (dd > maxDD) maxDD = dd;
    }
    maxDD = maxDD * 100;

    const row = document.createElement('tr');
    row.innerHTML = `
        <td class="${cumReturn >= 0 ? 'positive' : 'negative'}">${cumReturn.toFixed(2)}%</td>
        <td class="${annReturn >= 0 ? 'positive' : 'negative'}">${annReturn.toFixed(2)}%</td>
        <td>${annVol.toFixed(2)}%</td>
        <td class="negative">${maxDD.toFixed(2)}%</td>
        <td>${sharpe.toFixed(2)}</td>
    `;
    tbody.appendChild(row);
}

// ========================================
// 分组统计表格
// ========================================

function renderGroupStatsTable(stats) {
    const tbody = document.getElementById('group-stats-body');
    tbody.innerHTML = '';

    if (!stats || stats.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6">暂无数据</td></tr>';
        return;
    }

    stats.forEach(row => {
        const tr = document.createElement('tr');

        const cumRet = row['累计收益_%'];
        const annRet = row['年化收益_%'];

        tr.innerHTML = `
            <td>${row['分组'] || '--'}</td>
            <td class="${cumRet >= 0 ? 'positive' : 'negative'}">${formatNumber(cumRet)}</td>
            <td class="${annRet >= 0 ? 'positive' : 'negative'}">${formatNumber(annRet)}</td>
            <td>${formatNumber(row['年化波动_%'])}</td>
            <td class="negative">${formatNumber(row['最大回撤_%'])}</td>
            <td>${formatNumber(row['夏普比率'])}</td>
        `;

        tbody.appendChild(tr);
    });
}

// ========================================
// 年度统计表格
// ========================================

function renderYearlyStatsTable(yearlyData) {
    const tbody = document.getElementById('yearly-stats-body');
    tbody.innerHTML = '';

    if (!yearlyData || yearlyData.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6">暂无数据</td></tr>';
        return;
    }

    yearlyData.forEach(row => {
        const tr = document.createElement('tr');
        const ic = row.IC_pearson;

        tr.innerHTML = `
            <td>${row.year}</td>
            <td class="${ic >= 0 ? 'positive' : 'negative'}">${formatNumber(ic, 4)}</td>
            <td>${formatNumber(row.IC_std, 4)}</td>
            <td>${formatNumber(row.IR, 4)}</td>
            <td>${formatNumber(row.mean_return * 100, 4)}%</td>
            <td>${row.trading_days}</td>
        `;

        tbody.appendChild(tr);
    });
}

// ========================================
// IC 分析渲染
// ========================================

function renderICAnalysis(icData) {
    if (!icData) return;

    // 渲染 IC 统计指标
    renderICStats(icData.statistics);

    // 渲染 IC 图表
    renderICDecayChart(icData.decay);
    renderICDistributionChart(icData.distribution);
    renderICCumulativeChart(icData.cumulative);
    renderICAutocorrChart(icData.autocorrelation);
}

function renderICStats(stats) {
    if (!stats) return;

    document.getElementById('ic-win-rate').textContent =
        (stats.IC_win_rate * 100).toFixed(2) + '%';
    document.getElementById('ic-mean').textContent =
        stats.IC_mean.toFixed(4);
    document.getElementById('ic-ir').textContent =
        stats.ICIR.toFixed(4);
    document.getElementById('ic-t-stat').textContent =
        stats.t_stat.toFixed(2);
    document.getElementById('ic-stability').textContent =
        (stats.stability * 100).toFixed(2) + '%';
    document.getElementById('ic-skewness').textContent =
        stats.skewness.toFixed(4);
    document.getElementById('ic-kurtosis').textContent =
        stats.kurtosis.toFixed(4);
}

// IC 衰减图
function renderICDecayChart(decayData) {
    const ctx = document.getElementById('icDecayChart').getContext('2d');

    if (state.charts.icDecay) {
        state.charts.icDecay.destroy();
    }

    if (!decayData || decayData.length === 0) return;

    const labels = decayData.map(row => `T+${row.lag}`);
    const icValues = decayData.map(row => row.IC_mean);

    state.charts.icDecay = new Chart(ctx, {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: 'IC',
                data: icValues,
                backgroundColor: icValues.map(v => v >= 0 ? chartColors.icPositive : chartColors.icNegative),
                borderWidth: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false }
            },
            scales: {
                x: {
                    grid: { display: false }
                },
                y: {
                    grid: { color: '#2a2a3a' },
                    ticks: {
                        callback: function(value) {
                            return value.toFixed(3);
                        }
                    }
                }
            }
        }
    });
}

// IC 分布图
function renderICDistributionChart(distData) {
    const ctx = document.getElementById('icDistChart').getContext('2d');

    if (state.charts.icDist) {
        state.charts.icDist.destroy();
    }

    if (!distData) return;

    const labels = distData.bin_centers.map(v => v.toFixed(3));

    state.charts.icDist = new Chart(ctx, {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: '频数',
                data: distData.hist_counts,
                backgroundColor: chartColors.ic,
                borderWidth: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                annotation: {
                    annotations: {
                        mean: {
                            type: 'line',
                            xMin: distData.mean,
                            xMax: distData.mean,
                            borderColor: '#ef4444',
                            borderWidth: 2,
                            label: {
                                display: true,
                                content: `均值: ${distData.mean.toFixed(4)}`
                            }
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: {
                        maxTicksLimit: 8
                    }
                },
                y: {
                    grid: { color: '#2a2a3a' }
                }
            }
        }
    });
}

// IC 累计图
function renderICCumulativeChart(cumData) {
    const ctx = document.getElementById('icCumChart').getContext('2d');

    if (state.charts.icCum) {
        state.charts.icCum.destroy();
    }

    if (!cumData || cumData.length === 0) return;

    const labels = cumData.map(row => formatChartAxisDate(row.trade_dt));
    const cumIC = cumData.map(row => row.cumulative_ic);

    state.charts.icCum = new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: '累计IC',
                data: cumIC,
                borderColor: chartColors.ic,
                backgroundColor: 'rgba(59, 130, 246, 0.1)',
                borderWidth: 2,
                pointRadius: 0,
                fill: true,
                tension: 0.1
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { maxTicksLimit: 6 }
                },
                y: {
                    grid: { color: '#2a2a3a' }
                }
            }
        }
    });
}

// IC 自相关图
function renderICAutocorrChart(autocorrData) {
    const ctx = document.getElementById('icAutocorrChart').getContext('2d');

    if (state.charts.icAutocorr) {
        state.charts.icAutocorr.destroy();
    }

    if (!autocorrData || autocorrData.length === 0) return;

    const labels = autocorrData.map(row => `Lag ${row.lag}`);
    const values = autocorrData.map(row => row.autocorr);

    state.charts.icAutocorr = new Chart(ctx, {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: '自相关系数',
                data: values,
                backgroundColor: values.map(v => v >= 0 ? chartColors.icPositive : chartColors.icNegative),
                borderWidth: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { maxTicksLimit: 10 }
                },
                y: {
                    grid: { color: '#2a2a3a' },
                    min: -1,
                    max: 1,
                    ticks: {
                        callback: function(value) {
                            return value.toFixed(2);
                        }
                    }
                }
            }
        }
    });
}

// ========================================
// 基准对比渲染
// ========================================

/**
 * 初始化基准切换按钮
 */
function initBenchmarkToggle() {
    const btnPureLong = document.getElementById('btn-pure-long-vs-benchmark');
    const btnLongShort = document.getElementById('btn-long-factor-short-benchmark');

    if (!btnPureLong || !btnLongShort) return;

    // 默认选中纯多头
    btnPureLong.classList.add('active');
    btnLongShort.classList.remove('active');

    btnPureLong.addEventListener('click', () => {
        switchBenchmarkView('pure_long');
    });

    btnLongShort.addEventListener('click', () => {
        switchBenchmarkView('long_short_benchmark');
    });
}

/**
 * 切换基准视图
 */
function switchBenchmarkView(viewType) {
    const btnPureLong = document.getElementById('btn-pure-long-vs-benchmark');
    const btnLongShort = document.getElementById('btn-long-factor-short-benchmark');
    const titleEl = document.getElementById('benchmark-chart-title');

    if (viewType === 'pure_long') {
        btnPureLong.classList.add('active');
        btnLongShort.classList.remove('active');
        if (titleEl) titleEl.textContent = '单纯做多因子策略 vs 基准';
        renderPureLongVsBenchmark();
    } else {
        btnPureLong.classList.remove('active');
        btnLongShort.classList.add('active');
        if (titleEl) titleEl.textContent = '多因子空基准策略（超额收益）';
        renderLongFactorShortBenchmark();
    }
}

/**
 * 识别「因子值最高」分组（与回测引擎一致：列名为 G1..Gn 时，G1 为 Top）
 */
function getTopGroup(groupNav) {
    if (!groupNav || groupNav.length === 0) return null;

    // 获取所有分组列（排除 trade_dt）
    const groups = Object.keys(groupNav[0]).filter(k => k !== 'trade_dt');

    // 按 G 后数字升序：G1, G2, ... —— Top 为 G1
    groups.sort((a, b) => {
        const numA = parseInt(a.replace(/\D/g, ''), 10);
        const numB = parseInt(b.replace(/\D/g, ''), 10);
        return numA - numB;
    });

    return groups.length > 0 ? groups[0] : null;
}

/**
 * 渲染基准对比（主函数）
 */
function renderBenchmarkComparison(data) {
    if (!data.group_nav || !data.benchmark_nav) return;

    // 识别最高分组
    const topGroup = getTopGroup(data.group_nav);
    if (!topGroup) return;

    // 存储到 state 以便切换时使用
    state.benchmarkData = {
        groupNav: data.group_nav,
        benchmarkNav: data.benchmark_nav,
        topGroup: topGroup
    };

    // 默认显示纯多头视图
    renderPureLongVsBenchmark();
    renderBenchmarkStats(data.group_nav, data.benchmark_nav, topGroup);
}

/**
 * 渲染视图A：单纯做多因子策略 vs 基准
 */
function renderPureLongVsBenchmark() {
    if (!state.benchmarkData) return;

    const { groupNav, benchmarkNav, topGroup } = state.benchmarkData;
    const ctx = document.getElementById('benchmarkComparisonChart');
    if (!ctx) return;

    // 销毁旧图表
    if (state.charts.benchmarkComparison) {
        state.charts.benchmarkComparison.destroy();
    }

    const rawDates = groupNav.map(row => row.trade_dt);
    const labels = rawDates.map(formatChartAxisDate);

    // 对齐基准数据到因子数据的日期
    const benchmarkMap = {};
    benchmarkNav.forEach(row => {
        benchmarkMap[row.trade_dt] = row.benchmark_nav;
    });

    const topGroupNav = groupNav.map(row => row[topGroup]);
    const alignedBenchmarkNav = rawDates.map(date => benchmarkMap[date] || null);

    state.charts.benchmarkComparison = new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [
                {
                    label: `因子策略 (${topGroup})`,
                    data: topGroupNav,
                    borderColor: '#3b82f6',
                    backgroundColor: 'rgba(59, 130, 246, 0.1)',
                    borderWidth: 2,
                    pointRadius: 0,
                    fill: false,
                    tension: 0.1
                },
                {
                    label: '基准指数',
                    data: alignedBenchmarkNav,
                    borderColor: '#9898a8',
                    backgroundColor: 'transparent',
                    borderWidth: 2,
                    borderDash: [5, 5],
                    pointRadius: 0,
                    fill: false,
                    tension: 0.1
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false
            },
            plugins: {
                legend: {
                    position: 'bottom'
                },
                title: {
                    display: true,
                    text: '因子策略 vs 基准指数净值对比',
                    color: '#9898a8'
                }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { maxTicksLimit: 10 }
                },
                y: {
                    grid: { color: '#2a2a3a' },
                    title: {
                        display: true,
                        text: '净值',
                        color: '#9898a8'
                    }
                }
            }
        }
    });
}

/**
 * 渲染视图B：多因子空基准策略（超额收益）
 */
function renderLongFactorShortBenchmark() {
    if (!state.benchmarkData) return;

    const { groupNav, benchmarkNav, topGroup } = state.benchmarkData;
    const ctx = document.getElementById('benchmarkComparisonChart');
    if (!ctx) return;

    // 销毁旧图表
    if (state.charts.benchmarkComparison) {
        state.charts.benchmarkComparison.destroy();
    }

    const rawDates = groupNav.map(row => row.trade_dt);
    const labels = rawDates.map(formatChartAxisDate);

    // 对齐基准数据
    const benchmarkMap = {};
    benchmarkNav.forEach(row => {
        benchmarkMap[row.trade_dt] = row.benchmark_nav;
    });

    // 计算超额净值 = 因子净值 / 基准净值
    const excessNav = rawDates.map(date => {
        const factorNav = groupNav.find(r => r.trade_dt === date)?.[topGroup];
        const benchNav = benchmarkMap[date];
        if (factorNav && benchNav && benchNav > 0) {
            return factorNav / benchNav;
        }
        return null;
    });

    state.charts.benchmarkComparison = new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [
                {
                    label: '超额净值 (因子策略 / 基准)',
                    data: excessNav,
                    borderColor: '#22c55e',
                    backgroundColor: 'rgba(34, 197, 94, 0.1)',
                    borderWidth: 2,
                    pointRadius: 0,
                    fill: true,
                    tension: 0.1
                },
                {
                    label: '基准线 (1.0)',
                    data: labels.map(() => 1.0),
                    borderColor: '#9898a8',
                    backgroundColor: 'transparent',
                    borderWidth: 1,
                    borderDash: [5, 5],
                    pointRadius: 0,
                    fill: false
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false
            },
            plugins: {
                legend: {
                    position: 'bottom'
                },
                title: {
                    display: true,
                    text: '超额收益 (Alpha) - 多因子空基准策略',
                    color: '#9898a8'
                }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { maxTicksLimit: 10 }
                },
                y: {
                    grid: { color: '#2a2a3a' },
                    title: {
                        display: true,
                        text: '超额净值',
                        color: '#9898a8'
                    }
                }
            }
        }
    });
}

/**
 * 渲染基准对比统计表格
 */
function renderBenchmarkStats(groupNav, benchmarkNav, topGroup) {
    const tbody = document.getElementById('benchmark-stats-body');
    if (!tbody) return;

    tbody.innerHTML = '';

    // 对齐数据
    const benchmarkMap = {};
    benchmarkNav.forEach(row => {
        benchmarkMap[row.trade_dt] = row.benchmark_nav;
    });

    const factorNavValues = [];
    const benchmarkNavValues = [];

    groupNav.forEach(row => {
        const factorNav = row[topGroup];
        const benchNav = benchmarkMap[row.trade_dt];
        if (factorNav && benchNav) {
            factorNavValues.push(factorNav);
            benchmarkNavValues.push(benchNav);
        }
    });

    if (factorNavValues.length < 2) return;

    // 计算因子策略统计
    const factorReturns = [];
    for (let i = 1; i < factorNavValues.length; i++) {
        factorReturns.push(factorNavValues[i] / factorNavValues[i-1] - 1);
    }

    const factorMeanRet = factorReturns.reduce((a, b) => a + b, 0) / factorReturns.length;
    const factorStdRet = Math.sqrt(factorReturns.reduce((sum, r) => sum + Math.pow(r - factorMeanRet, 2), 0) / factorReturns.length);
    const factorCumRet = (factorNavValues[factorNavValues.length - 1] / factorNavValues[0] - 1) * 100;
    const factorAnnRet = factorMeanRet * 252 * 100;

    // 计算基准统计
    const benchReturns = [];
    for (let i = 1; i < benchmarkNavValues.length; i++) {
        benchReturns.push(benchmarkNavValues[i] / benchmarkNavValues[i-1] - 1);
    }

    const benchMeanRet = benchReturns.reduce((a, b) => a + b, 0) / benchReturns.length;
    const benchCumRet = (benchmarkNavValues[benchmarkNavValues.length - 1] / benchmarkNavValues[0] - 1) * 100;
    const benchAnnRet = benchMeanRet * 252 * 100;

    // 计算超额收益
    const excessCumRet = factorCumRet - benchCumRet;
    const excessAnnRet = factorAnnRet - benchAnnRet;

    // 计算信息比率（超额收益 / 跟踪误差）
    const excessReturns = factorReturns.map((r, i) => r - benchReturns[i]);
    const trackingError = Math.sqrt(excessReturns.reduce((sum, r) => sum + Math.pow(r - (excessReturns.reduce((a, b) => a + b, 0) / excessReturns.length), 2), 0) / excessReturns.length);
    const informationRatio = trackingError > 0 ? (excessReturns.reduce((a, b) => a + b, 0) / excessReturns.length) / trackingError * Math.sqrt(252) : 0;

    const rows = [
        ['累计收益', factorCumRet, benchCumRet, excessCumRet],
        ['年化收益', factorAnnRet, benchAnnRet, excessAnnRet],
        ['信息比率', '--', '--', informationRatio]
    ];

    rows.forEach(([metric, factor, bench, excess]) => {
        const tr = document.createElement('tr');
        const formatValue = (val) => {
            if (typeof val === 'number') {
                return metric === '信息比率' ? val.toFixed(2) : val.toFixed(2) + '%';
            }
            return val;
        };

        tr.innerHTML = `
            <td>${metric}</td>
            <td class="${typeof factor === 'number' && factor >= 0 ? 'positive' : typeof factor === 'number' ? 'negative' : ''}">${formatValue(factor)}</td>
            <td class="${typeof bench === 'number' && bench >= 0 ? 'positive' : typeof bench === 'number' ? 'negative' : ''}">${formatValue(bench)}</td>
            <td class="${typeof excess === 'number' && excess >= 0 ? 'positive' : typeof excess === 'number' ? 'negative' : ''}">${formatValue(excess)}</td>
        `;
        tbody.appendChild(tr);
    });
}

// ========================================
// 工具函数
// ========================================
/**
 * 下载完整的 HTML 分析报告
 */
function downloadFullReport() {
    if (!state.results) {
        alert('暂无回测结果，请先运行回测');
        return;
    }

    const data = state.results;
    const btn = document.getElementById('btn-download-report');
    const originalText = btn.textContent;
    btn.textContent = '⏳ 正在生成报告...';
    btn.disabled = true;

    // 使用 setTimeout 避免阻塞 UI，让按钮状态先更新
    setTimeout(() => {
        try {
            // 1. 获取 Chart.js 图表的 Base64 图片
            const getChartImage = (chartKey) => {
                if (state.charts[chartKey]) {
                    return state.charts[chartKey].toBase64Image('image/png', 1);
                }
                return '';
            };

            const navImage = getChartImage('nav');
            const lsImage = getChartImage('longShort');
            const benchmarkImage = getChartImage('benchmarkComparison');
            const icDecayImage = getChartImage('icDecay');
            const icDistImage = getChartImage('icDist');
            const icCumImage = getChartImage('icCum');
            const icAutocorrImage = getChartImage('icAutocorr');

            // 2. 准备数据表格 HTML
            const stats = data.group_stats || [];
            const yearlyData = data.yearly_ic || [];
            const lsData = data.long_short || [];
            const icStats = data.ic_analysis?.statistics || {};

            let groupTableRows = stats.map(r => `
                <tr>
                    <td>${r['分组'] || '--'}</td>
                    <td style="color: ${r['累计收益_%'] >= 0 ? '#22c55e' : '#ef4444'}">${formatNumber(r['累计收益_%'])}%</td>
                    <td style="color: ${r['年化收益_%'] >= 0 ? '#22c55e' : '#ef4444'}">${formatNumber(r['年化收益_%'])}%</td>
                    <td>${formatNumber(r['年化波动_%'])}%</td>
                    <td style="color: #ef4444">${formatNumber(r['最大回撤_%'])}%</td>
                    <td>${formatNumber(r['夏普比率'])}</td>
                </tr>
            `).join('');

            let yearlyTableRows = yearlyData.map(r => `
                <tr>
                    <td>${r.year}</td>
                    <td>${formatNumber(r.IC_pearson, 4)}</td>
                    <td>${formatNumber(r.IC_std, 4)}</td>
                    <td>${formatNumber(r.IR, 4)}</td>
                    <td>${formatNumber(r.mean_return * 100, 4)}%</td>
                    <td>${r.trading_days}</td>
                </tr>
            `).join('');

            // 3. 拼装完整的独立 HTML 文档
            const htmlContent = `
            <!DOCTYPE html>
            <html lang="zh-CN">
            <head>
                <meta charset="UTF-8">
                <title>量化回测分析报告</title>
                <style>
                    body { font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #1a1a2e; color: #e2e8f0; padding: 40px; line-height: 1.6; }
                    .container { max-width: 1200px; margin: 0 auto; background: #16213e; padding: 40px; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
                    h1 { text-align: center; color: #60a5fa; border-bottom: 2px solid #2a2a3a; padding-bottom: 20px; margin-bottom: 30px; }
                    h2 { color: #a78bfa; margin-top: 40px; border-left: 4px solid #8b5cf6; padding-left: 15px; }
                    .metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }
                    .metric-card { background: #0f3460; padding: 15px; border-radius: 8px; text-align: center; }
                    .metric-value { font-size: 24px; font-weight: bold; color: #fff; }
                    .metric-label { font-size: 12px; color: #94a3b8; margin-top: 5px; }
                    table { width: 100%; border-collapse: collapse; margin-top: 20px; background: #0f3460; }
                    th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #2a2a3a; }
                    th { background: #1a1a40; color: #60a5fa; }
                    .chart-container { background: #fff; padding: 20px; border-radius: 8px; margin-top: 20px; text-align: center; }
                    .chart-container img { max-width: 100%; height: auto; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
                    @media print { body { background: #fff; color: #000; } .container { box-shadow: none; } }
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>📊 量化因子回测分析报告</h1>
                    
                    <h2>1. 核心绩效指标</h2>
                    <div class="metrics-grid">
                        <div class="metric-card">
                            <div class="metric-value" style="color: ${data.factor_summary?.IC_pearson >= 0 ? '#22c55e' : '#ef4444'}">${formatNumber(icStats.IC_mean || data.factor_summary?.IC_pearson, 4)}</div>
                            <div class="metric-label">IC Mean</div>
                        </div>
                        <div class="metric-card">
                            <div class="metric-value">${formatNumber(icStats.ICIR, 4)}</div>
                            <div class="metric-label">ICIR</div>
                        </div>
                        <div class="metric-card">
                            <div class="metric-value">${formatNumber(stats[stats.length-1]?.['年化收益_%'])}%</div>
                            <div class="metric-label">最高组年化收益</div>
                        </div>
                        <div class="metric-card">
                            <div class="metric-value">${formatNumber(stats[stats.length-1]?.['夏普比率'])}</div>
                            <div class="metric-label">最高组夏普比率</div>
                        </div>
                        <div class="metric-card">
                            <div class="metric-value" style="color: #ef4444">${formatNumber(stats[stats.length-1]?.['最大回撤_%'])}%</div>
                            <div class="metric-label">最高组最大回撤</div>
                        </div>
                    </div>

                    <h2>2. 分组统计指标</h2>
                    <table>
                        <thead><tr><th>分组</th><th>累计收益</th><th>年化收益</th><th>年化波动</th><th>最大回撤</th><th>夏普比率</th></tr></thead>
                        <tbody>${groupTableRows}</tbody>
                    </table>

                    ${navImage ? `
                    <h2>3. 分组净值曲线</h2>
                    <div class="chart-container"><img src="${navImage}" alt="分组净值曲线"></div>
                    ` : ''}

                    <h2>4. Long-Short 多空组合</h2>
                    ${lsImage ? `<div class="chart-container"><img src="${lsImage}" alt="多空净值"></div>` : ''}
                    
                    <h2>5. 分年度 IC 统计</h2>
                    <table>
                        <thead><tr><th>年份</th><th>IC</th><th>IC_STD</th><th>IR</th><th>平均收益率</th><th>交易日数</th></tr></thead>
                        <tbody>${yearlyTableRows}</tbody>
                    </table>

                    <h2>6. IC 系列分析</h2>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                        ${icDecayImage ? `<div class="chart-container"><img src="${icDecayImage}" alt="IC衰减"></div>` : ''}
                        ${icDistImage ? `<div class="chart-container"><img src="${icDistImage}" alt="IC分布"></div>` : ''}
                        ${icCumImage ? `<div class="chart-container"><img src="${icCumImage}" alt="IC累计"></div>` : ''}
                        ${icAutocorrImage ? `<div class="chart-container"><img src="${icAutocorrImage}" alt="IC自相关"></div>` : ''}
                    </div>

                    ${benchmarkImage ? `
                    <h2>7. 基准对比 (超额收益)</h2>
                    <div class="chart-container"><img src="${benchmarkImage}" alt="基准对比"></div>
                    ` : ''}

                    <div style="margin-top: 50px; text-align: center; color: #64748b; font-size: 12px; border-top: 1px solid #334155; padding-top: 20px;">
                        报告生成时间：${new Date().toLocaleString()} | 量化回测系统自动生成
                    </div>
                </div>
            </body>
            </html>
            `;

            // 4. 触发下载
           const dataUri = 'data:application/octet-stream;charset=utf-8,' + encodeURIComponent(htmlContent);
           const link = document.createElement('a');
           link.href = dataUri;
           link.download = `回测报告_${new Date().getTime()}.html`;
           document.body.appendChild(link);
           link.click();
           document.body.removeChild(link);

        } catch (error) {
            console.error('生成报告失败:', error);
            alert('生成报告失败: ' + error.message);
        } finally {
            btn.textContent = originalText;
            btn.disabled = false;
        }
    }, 100);
}
function formatNumber(value, decimals = 2) {
    if (value === null || value === undefined || isNaN(value)) {
        return '--';
    }
    return Number(value).toFixed(decimals);
}

// 下载净值数据
function downloadNavData() {
    if (!state.results || !state.results.group_nav) {
        alert('暂无数据可下载');
        return;
    }

    const data = state.results.group_nav;
    const headers = Object.keys(data[0]);
    const csvContent = [
        headers.join(','),
        ...data.map(row => headers.map(h => row[h]).join(','))
    ].join('\n');

    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'group_nav.csv';
    link.click();
}

// ========================================
// 因子相关性分析
// ========================================

/**
 * 回测完成后更新相关性分析按钮状态
 */
function updateCorrelationButtonAfterBacktest() {
    const analyzeBtn = document.getElementById('btn-analyze-correlation');
    const hasTargetTable = document.getElementById('correlation_target_table')?.value;

    // 默认选择"当前回测因子"，只需要选择对比因子表即可
    if (analyzeBtn) {
        analyzeBtn.disabled = !hasTargetTable;
        if (!hasTargetTable) {
            analyzeBtn.title = "请选择对比因子表";
        } else {
            analyzeBtn.title = "";
        }
    }
}

/**
 * 初始化相关性分析模块
 * 绑定数据源切换与分析按钮事件
 */
function initCorrelationAnalysis() {
    const analyzeBtn = document.getElementById('btn-analyze-correlation');

    // 初始化时：默认选"当前回测因子"
    const databaseSelect = document.getElementById('correlation-database-select');
    const currentHint = document.getElementById('current-factor-hint');
    if (databaseSelect) databaseSelect.style.display = 'none';
    if (currentHint) currentHint.style.display = 'block';

    // 数据源切换
    const dataSourceRadios = document.querySelectorAll('input[name="correlation_data_source"]');
    dataSourceRadios.forEach(radio => {
        radio.addEventListener('change', (e) => {
            const databaseSelect = document.getElementById('correlation-database-select');
            const currentHint = document.getElementById('current-factor-hint');

            // 隐藏所有特定面板
            if (databaseSelect) databaseSelect.style.display = 'none';
            if (currentHint) currentHint.style.display = 'none';

            if (e.target.value === 'database') {
                if (databaseSelect) databaseSelect.style.display = 'block';
            } else if (e.target.value === 'current') {
                if (currentHint) currentHint.style.display = 'block';
            }

            // 更新按钮状态
            updateCorrelationAnalyzeButton();
        });
    });

    // 因子库选择变化时处理
    const correlationFactorSelect = document.getElementById('correlation_factor_name');
    if (correlationFactorSelect) {
        correlationFactorSelect.addEventListener('change', (e) => {
            updateCorrelationAnalyzeButton();
        });
    }

    // 对比因子表选择变化时处理
    const correlationTargetTableSelect = document.getElementById('correlation_target_table');
    if (correlationTargetTableSelect) {
        correlationTargetTableSelect.addEventListener('change', () => {
            updateCorrelationAnalyzeButton();
        });
    }

    // 更新分析按钮状态
    function updateCorrelationAnalyzeButton() {
        const dataSource = document.querySelector('input[name="correlation_data_source"]:checked')?.value || 'current';
        const hasTargetTable = document.getElementById('correlation_target_table')?.value;

        let hasSource = false;
        if (dataSource === 'current') {
            // 只要运行过回测就有结果 (state.results)
            hasSource = !!state.results;
        } else {
            hasSource = !!document.getElementById('correlation_factor_name')?.value;
        }

        if (analyzeBtn) {
            analyzeBtn.disabled = !(hasSource && hasTargetTable);
            if (!hasTargetTable) {
                analyzeBtn.title = "请选择对比因子表";
            } else if (!hasSource) {
                analyzeBtn.title = dataSource === 'current' ? "请先运行回测" : "请选择源因子（因子库）";
            } else {
                analyzeBtn.title = "";
            }
        }
    }

    // 分析按钮点击
    if (analyzeBtn) {
        analyzeBtn.addEventListener('click', runCorrelationAnalysis);
    }

    // 初始化按钮状态
    updateCorrelationAnalyzeButton();
}

/**
 * 运行因子相关性分析（重构版）
 */
async function runCorrelationAnalysis() {
    const dataSource = document.querySelector('input[name="correlation_data_source"]:checked')?.value || 'current';

    let factorName = null;
    let tableName = null;

    if (dataSource === 'database') {
        factorName = document.getElementById('correlation_factor_name')?.value;
        tableName = document.getElementById('correlation_factor_table')?.value;
        if (!factorName || !tableName) {
            alert('请先选择因子表和因子');
            return;
        }
    } else if (dataSource === 'current') {
        if (!state.results) {
            alert('请先运行回测以获取当前因子数据');
            return;
        }
    }

    const btn = document.getElementById('btn-analyze-correlation');
    btn.disabled = true;
    btn.textContent = '分析中...';

    try {
        const correlationTargetTableSelect = document.getElementById('correlation_target_table');
        const correlationTableName = correlationTargetTableSelect?.value;

        if (!correlationTableName) {
            alert('请先选择要对比的因子表（因子库）');
            return;
        }

        // 获取阈值参数
        const threshold = parseFloat(document.getElementById('correlation_threshold')?.value || 0);

        const requestBody = {
            method: 'spearman',
            n_groups: parseInt(document.getElementById('n_groups').value) || 5,
            correlation_table_name: correlationTableName,
            source_type: dataSource,
            threshold: threshold  // 传递阈值
        };

        if (dataSource === 'database') {
            requestBody.factor_name = factorName;
            requestBody.table_name = tableName;
        }

        const response = await fetch('/api/factor_correlation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody)
        });

        const json = await response.json();

        if (json.success) {
            console.log('相关性分析结果:', json.data);
            // 存储结果
            state.correlationResults = json.data;
            // 渲染结果
            renderCorrelationResults(json.data);
            setStatus(`相关性分析完成！耗时 ${json.data.elapsed_time.toFixed(2)} 秒`, 'success');
        } else {
            alert('分析失败: ' + json.error);
        }
    } catch (error) {
        alert('请求失败: ' + error.message);
        console.error(error);
    } finally {
        btn.disabled = false;
        btn.textContent = '开始相关性分析';
    }
}

/**
 * 渲染相关性分析结果（重构版 - 简化为三张图表）
 * @param {Object} data - 相关性分析数据
 */
function renderCorrelationResults(data) {
    // 显示结果区域
    document.getElementById('correlation-results').style.display = 'block';

    // 显示筛选信息
    const filterInfo = document.getElementById('correlation-filter-info');
    if (filterInfo) {
        const filtered = data.filtered_factors || [];
        const total = data.total_factors_in_table || 0;
        const threshold = data.threshold || 0;

        if (threshold > 0) {
            filterInfo.innerHTML = `
                <div class="info-banner">
                    📊 阈值筛选: ${total} 个因子 → ${filtered.length} 个因子 (相关性 ≥ ${threshold})
                </div>
            `;
            filterInfo.style.display = 'block';
        } else {
            filterInfo.innerHTML = `
                <div class="info-banner">
                    📊 显示全部 ${filtered.length} 个因子的相关性分析
                </div>
            `;
            filterInfo.style.display = 'block';
        }
    }

    // 重置为默认视图（因子相关性）
    state.currentCorrelationView = 'factor_correlation';

    // 更新按钮状态
    document.getElementById('btn-corr-factor')?.classList.add('active');
    document.getElementById('btn-corr-ic')?.classList.remove('active');
    document.getElementById('btn-corr-return')?.classList.remove('active');

    // 渲染默认图表（因子相关性）
    renderCurrentCorrelationChart();
}

/**
 * 切换相关性分析图表
 * @param {string} viewType - 'factor_correlation' | 'ic' | 'return'
 */
function switchCorrelationChart(viewType) {
    state.currentCorrelationView = viewType;

    // 更新按钮状态
    document.getElementById('btn-corr-factor')?.classList.toggle('active', viewType === 'factor_correlation');
    document.getElementById('btn-corr-ic')?.classList.toggle('active', viewType === 'ic');
    document.getElementById('btn-corr-return')?.classList.toggle('active', viewType === 'return');

    // 更新图表标题和说明
    const titles = {
        'factor_correlation': '因子相关性',
        'ic': 'IC',
        'return': '多空收益率'
    };
    const descriptions = {
        'factor_correlation': '横轴：时间 | 纵轴：当前因子与对比表中各因子的横截面相关性',
        'ic': '横轴：时间 | 纵轴：各因子的IC（因子值与下期收益的相关性）',
        'return': '横轴：时间 | 纵轴：各因子多空组合的累计净值'
    };

    document.getElementById('correlation-chart-title').textContent = titles[viewType];
    document.getElementById('correlation-chart-desc').textContent = descriptions[viewType];

    // 渲染当前选中的图表
    renderCurrentCorrelationChart();
}

/**
 * 渲染当前选中的相关性图表
 */
function renderCurrentCorrelationChart() {
    const data = state.correlationResults;
    if (!data) return;

    switch (state.currentCorrelationView) {
        case 'factor_correlation':
            renderFactorCorrelationChart(data.factor_correlation_chart, data.filtered_factors);
            break;
        case 'ic':
            renderICChart(data.ic_chart, data.new_factor, data.filtered_factors);
            break;
        case 'return':
            renderReturnChart(data.return_chart, data.new_factor, data.filtered_factors);
            break;
    }
}

/**
 * 渲染图表1：因子相关性
 * 横轴时间，纵轴相关性，显示各因子与当前因子的相关性
 */
function renderFactorCorrelationChart(chartData, factors) {
    const ctx = document.getElementById('correlationMainChart')?.getContext('2d');
    if (!ctx) return;

    // 销毁旧图表
    if (state.correlationCharts.main) {
        state.correlationCharts.main.destroy();
    }

    if (!chartData || chartData.length === 0) {
        return;
    }

    const labels = chartData.map(row => formatChartAxisDate(row.trade_dt));

    // 多种颜色区分不同因子
    const colorPalette = [
        '#ef4444', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899',
        '#06b6d4', '#84cc16', '#f43f5e', '#14b8a6', '#a855f7'
    ];

    const datasets = factors.map((factor, idx) => ({
        label: factor,
        data: chartData.map(row => row[factor]),
        borderColor: colorPalette[idx % colorPalette.length],
        backgroundColor: 'transparent',
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.1
    }));

    state.correlationCharts.main = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { position: 'bottom' },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            return `${context.dataset.label}: ${context.parsed.y?.toFixed(4) || 'N/A'}`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { maxTicksLimit: 10 }
                },
                y: {
                    grid: { color: '#2a2a3a' },
                    min: -1,
                    max: 1,
                    title: {
                        display: true,
                        text: '相关性',
                        color: '#9898a8'
                    },
                    ticks: {
                        callback: function(value) {
                            return value.toFixed(2);
                        }
                    }
                }
            }
        }
    });
}

/**
 * 渲染图表2：IC
 * 横轴时间，纵轴IC，显示当前因子和对比因子的IC
 */
function renderICChart(chartData, newFactor, factors) {
    const ctx = document.getElementById('correlationMainChart')?.getContext('2d');
    if (!ctx) return;

    // 销毁旧图表
    if (state.correlationCharts.main) {
        state.correlationCharts.main.destroy();
    }

    if (!chartData || chartData.length === 0) {
        return;
    }

    const labels = chartData.map(row => formatChartAxisDate(row.trade_dt));
    const allFactors = [newFactor, ...factors];

    // 颜色配置：当前因子蓝色加粗，其他因子不同颜色
    const colorPalette = [
        '#3b82f6',  // 当前因子 - 蓝色
        '#ef4444', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899',
        '#06b6d4', '#84cc16', '#f43f5e', '#14b8a6', '#a855f7'
    ];

    const datasets = allFactors.map((factor, idx) => ({
        label: factor === newFactor ? `${factor} (当前因子)` : factor,
        data: chartData.map(row => row[factor]),
        borderColor: colorPalette[idx % colorPalette.length],
        backgroundColor: 'transparent',
        borderWidth: factor === newFactor ? 2.5 : 1.5,  // 当前因子线条更粗
        pointRadius: 0,
        tension: 0.1
    }));

    state.correlationCharts.main = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { position: 'bottom' },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            return `${context.dataset.label}: ${context.parsed.y?.toFixed(4) || 'N/A'}`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { maxTicksLimit: 10 }
                },
                y: {
                    grid: { color: '#2a2a3a' },
                    title: {
                        display: true,
                        text: 'IC',
                        color: '#9898a8'
                    }
                }
            }
        }
    });
}

/**
 * 渲染图表3：多空收益率
 * 横轴时间，纵轴累计净值，显示当前因子和对比因子的多空组合净值
 */
function renderReturnChart(chartData, newFactor, factors) {
    const ctx = document.getElementById('correlationMainChart')?.getContext('2d');
    if (!ctx) return;

    // 销毁旧图表
    if (state.correlationCharts.main) {
        state.correlationCharts.main.destroy();
    }

    if (!chartData || chartData.length === 0) {
        return;
    }

    const labels = chartData.map(row => formatChartAxisDate(row.trade_dt));
    const allFactors = [newFactor, ...factors];

    // 颜色配置：当前因子蓝色加粗，其他因子不同颜色
    const colorPalette = [
        '#3b82f6',  // 当前因子 - 蓝色
        '#ef4444', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899',
        '#06b6d4', '#84cc16', '#f43f5e', '#14b8a6', '#a855f7'
    ];

    const datasets = allFactors.map((factor, idx) => ({
        label: factor === newFactor ? `${factor} (当前因子)` : factor,
        data: chartData.map(row => row[factor]),
        borderColor: colorPalette[idx % colorPalette.length],
        backgroundColor: 'transparent',
        borderWidth: factor === newFactor ? 2.5 : 1.5,  // 当前因子线条更粗
        pointRadius: 0,
        tension: 0.1
    }));

    state.correlationCharts.main = new Chart(ctx, {
        type: 'line',
        data: {labels, datasets},
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {mode: 'index', intersect: false},
            plugins: {
                legend: {position: 'bottom'},
                tooltip: {
                    callbacks: {
                        label: function (context) {
                            const value = context.parsed.y;
                            if (value === null || value === undefined) return `${context.dataset.label}: N/A`;
                            // 显示净值和收益率
                            const returnPct = ((value - 1) * 100).toFixed(2);
                            return `${context.dataset.label}: ${value.toFixed(4)} (${returnPct}%)`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: {display: false},
                    ticks: {maxTicksLimit: 10}
                },
                y: {
                    grid: {color: '#2a2a3a'},
                    title: {
                        display: true,
                        text: '累计净值',
                        color: '#9898a8'
                    },
                    ticks: {
                        callback: function (value) {
                            return value.toFixed(2);
                        }
                    }
                }
            }
        }
    });
}
// ========================================
// 一键生成并下载分析报告 (无需修改HTML)
// ========================================
// ========================================
// 一键生成并下载完整分析报告 (增强版)
// ========================================
function downloadFullReport() {
    if (!state.results) { alert('暂无回测结果'); return; }

    const btn = document.getElementById('dynamic-download-btn');
    const originalText = btn.textContent;
    btn.textContent = '⏳ 生成中...';
    btn.disabled = true;

    setTimeout(() => {
        try {
            const data = state.results;
            const getImg = (key) => state.charts[key] ? state.charts[key].toBase64Image() : '';

            // 提取数据
            const stats = data.group_stats || [];
            const yearlyData = data.yearly_ic || [];
            const lsData = data.long_short || [];
            const icStats = data.ic_analysis?.statistics || {};
            const topGroup = stats.length > 0 ? stats[0] : null;

            // 1. 核心指标计算
            const factorReturn = lsData.length > 0 ? ((lsData[lsData.length - 1]['Long-Short'] - 1) * 100).toFixed(2) + '%' : '--';
            const sharpe = topGroup ? topGroup['夏普比率']?.toFixed(2) || '--' : '--';
            const annRet = topGroup ? (topGroup['年化收益_%']?.toFixed(2) + '%') || '--' : '--';
            const icMean = icStats.IC_mean ? icStats.IC_mean.toFixed(4) : '--';
            const rankIc = icStats.Rank_IC ? icStats.Rank_IC.toFixed(4) : '--';
            const maxDD = topGroup ? (topGroup['最大回撤_%']?.toFixed(2) + '%') || '--' : '--';

            // 2. 分组统计表格
            let groupRows = stats.map(r => `<tr><td>${r['分组']||'--'}</td><td style="color:${r['累计收益_%']>=0?'#22c55e':'#ef4444'}">${formatNumber(r['累计收益_%'])}%</td><td style="color:${r['年化收益_%']>=0?'#22c55e':'#ef4444'}">${formatNumber(r['年化收益_%'])}%</td><td>${formatNumber(r['年化波动_%'])}%</td><td style="color:#ef4444">${formatNumber(r['最大回撤_%'])}%</td><td>${formatNumber(r['夏普比率'])}</td></tr>`).join('');

            // 3. 多空统计计算
            let lsStatsHtml = '';
            if (lsData.length > 1) {
                const navValues = lsData.map(r => r['Long-Short']).filter(v => v != null && !isNaN(v));
                const returns = [];
                for(let i=1; i<navValues.length; i++) returns.push(navValues[i]/navValues[i-1]-1);
                const meanRet = returns.reduce((a,b)=>a+b,0)/returns.length;
                const stdRet = Math.sqrt(returns.reduce((s,r)=>s+Math.pow(r-meanRet,2),0)/returns.length);
                const cumRet = (navValues[navValues.length-1]/navValues[0]-1)*100;
                const lsAnnRet = meanRet*252*100;
                const lsAnnVol = stdRet*Math.sqrt(252)*100;
                const lsSharpe = lsAnnVol > 0 ? lsAnnRet/lsAnnVol : 0;
                let lsMaxDD = 0, peak = navValues[0];
                for(let i=1; i<navValues.length; i++){ if(navValues[i]>peak) peak=navValues[i]; const dd=(peak-navValues[i])/peak; if(dd>lsMaxDD) lsMaxDD=dd; }

                lsStatsHtml = `<table><tr><th>累计收益</th><th>年化收益</th><th>年化波动</th><th>最大回撤</th><th>夏普比率</th></tr>
                <tr><td style="color:${cumRet>=0?'#22c55e':'#ef4444'}">${cumRet.toFixed(2)}%</td><td style="color:${lsAnnRet>=0?'#22c55e':'#ef4444'}">${lsAnnRet.toFixed(2)}%</td><td>${lsAnnVol.toFixed(2)}%</td><td style="color:#ef4444">${(lsMaxDD*100).toFixed(2)}%</td><td>${lsSharpe.toFixed(2)}</td></tr></table>`;
            }

            // 4. 年度统计表格
            let yearlyRows = yearlyData.map(r => `<tr><td>${r.year}</td><td style="color:${r.IC_pearson>=0?'#22c55e':'#ef4444'}">${formatNumber(r.IC_pearson,4)}</td><td>${formatNumber(r.IC_std,4)}</td><td>${formatNumber(r.IR,4)}</td><td>${formatNumber(r.mean_return*100,4)}%</td><td>${r.trading_days}</td></tr>`).join('');

            // 5. IC 统计指标
            const icStatCards = `
            <div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:15px;margin-top:15px;">
                <div style="background:#0f3460;padding:15px;border-radius:8px;text-align:center"><div style="font-size:20px;font-weight:bold">${(icStats.IC_win_rate*100).toFixed(2)}%</div><div style="font-size:12px;color:#94a3b8">IC胜率</div></div>
                <div style="background:#0f3460;padding:15px;border-radius:8px;text-align:center"><div style="font-size:20px;font-weight:bold">${icStats.IC_mean?.toFixed(4)||'--'}</div><div style="font-size:12px;color:#94a3b8">IC均值</div></div>
                <div style="background:#0f3460;padding:15px;border-radius:8px;text-align:center"><div style="font-size:20px;font-weight:bold">${icStats.ICIR?.toFixed(4)||'--'}</div><div style="font-size:12px;color:#94a3b8">ICIR</div></div>
                <div style="background:#0f3460;padding:15px;border-radius:8px;text-align:center"><div style="font-size:20px;font-weight:bold">${icStats.t_stat?.toFixed(2)||'--'}</div><div style="font-size:12px;color:#94a3b8">T统计量</div></div>
                <div style="background:#0f3460;padding:15px;border-radius:8px;text-align:center"><div style="font-size:20px;font-weight:bold">${(icStats.stability*100).toFixed(2)}%</div><div style="font-size:12px;color:#94a3b8">IC稳定性</div></div>
                <div style="background:#0f3460;padding:15px;border-radius:8px;text-align:center"><div style="font-size:20px;font-weight:bold">${icStats.skewness?.toFixed(4)||'--'}</div><div style="font-size:12px;color:#94a3b8">IC偏度</div></div>
                <div style="background:#0f3460;padding:15px;border-radius:8px;text-align:center"><div style="font-size:20px;font-weight:bold">${icStats.kurtosis?.toFixed(4)||'--'}</div><div style="font-size:12px;color:#94a3b8">IC峰度</div></div>
            </div>`;

            // 拼装最终完整 HTML
            const html = `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>量化回测完整分析报告</title>
            <style>
                body { font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #1a1a2e; color: #e2e8f0; padding: 40px; line-height: 1.6; }
                .container { max-width: 1200px; margin: 0 auto; background: #16213e; padding: 40px; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
                h1 { text-align: center; color: #60a5fa; border-bottom: 2px solid #2a2a3a; padding-bottom: 20px; margin-bottom: 30px; }
                h2 { color: #a78bfa; margin-top: 40px; border-left: 4px solid #8b5cf6; padding-left: 15px; }
                table { width: 100%; border-collapse: collapse; margin-top: 15px; background: #0f3460; }
                th, td { padding: 12px; text-align: center; border: 1px solid #334155; }
                th { background: #1a1a40; color: #60a5fa; }
                .chart-box { background: #fff; padding: 15px; border-radius: 8px; margin-top: 15px; text-align: center; }
                .chart-box img { max-width: 100%; height: auto; }
                .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
                @media print { body { background: #fff; color: #000; } .container { box-shadow: none; } }
            </style></head><body>
            <div class="container">
                <h1>📊 量化因子完整分析报告</h1>
                
                <h2>1. 核心绩效指标概览</h2>
                <div style="display:grid;grid-template-columns:repeat(3, 1fr);gap:20px;">
                    <div style="background:#0f3460;padding:20px;border-radius:8px;text-align:center"><div style="font-size:28px;font-weight:bold;color:#22c55e">${factorReturn}</div><div style="font-size:14px;color:#94a3b8">因子多空收益</div></div>
                    <div style="background:#0f3460;padding:20px;border-radius:8px;text-align:center"><div style="font-size:28px;font-weight:bold">${sharpe}</div><div style="font-size:14px;color:#94a3b8">最高组夏普比率</div></div>
                    <div style="background:#0f3460;padding:20px;border-radius:8px;text-align:center"><div style="font-size:28px;font-weight:bold">${annRet}</div><div style="font-size:14px;color:#94a3b8">最高组年化收益</div></div>
                    <div style="background:#0f3460;padding:20px;border-radius:8px;text-align:center"><div style="font-size:28px;font-weight:bold">${icMean}</div><div style="font-size:14px;color:#94a3b8">IC均值</div></div>
                    <div style="background:#0f3460;padding:20px;border-radius:8px;text-align:center"><div style="font-size:28px;font-weight:bold">${rankIc}</div><div style="font-size:14px;color:#94a3b8">Rank IC</div></div>
                    <div style="background:#0f3460;padding:20px;border-radius:8px;text-align:center"><div style="font-size:28px;font-weight:bold;color:#ef4444">${maxDD}</div><div style="font-size:14px;color:#94a3b8">最高组最大回撤</div></div>
                </div>

                <h2>2. 分组统计指标</h2>
                <table><tr><th>分组</th><th>累计收益</th><th>年化收益</th><th>年化波动</th><th>最大回撤</th><th>夏普比率</th></tr>${groupRows}</table>

                ${getImg('nav')?`<h2>3. 分组净值曲线</h2><div class="chart-box"><img src="${getImg('nav')}"></div>`:''}

                <h2>4. Long-Short 多空组合</h2>
                ${lsStatsHtml}
                ${getImg('longShort')?`<div class="chart-box"><img src="${getImg('longShort')}"></div>`:''}

                ${getImg('benchmarkComparison')?`<h2>5. 基准对比与超额收益</h2><div class="chart-box"><img src="${getImg('benchmarkComparison')}"></div>`:''}

                <h2>6. 分年度 IC 统计</h2>
                <table><tr><th>年份</th><th>IC</th><th>IC_STD</th><th>IR</th><th>平均收益率</th><th>交易日数</th></tr>${yearlyRows}</table>

                <h2>7. IC 深度分析</h2>
                ${icStatCards}
                <div class="grid-2" style="margin-top:20px">
                  
                     ${getImg('icDecay')?`<div class="chart-box"><img src="${getImg('icDecay')}"><div style="text-align:center; margin-top:10px; color:#333; font-weight:bold; font-size:14px;">IC 衰减图 (IC Decay)</div></div>`:''}
                     ${getImg('icDist')?`<div class="chart-box"><img src="${getImg('icDist')}"><div style="text-align:center; margin-top:10px; color:#333; font-weight:bold; font-size:14px;">IC 分布直方图 (IC Distribution)</div></div>`:''}
                     ${getImg('icCum')?`<div class="chart-box"><img src="${getImg('icCum')}"><div style="text-align:center; margin-top:10px; color:#333; font-weight:bold; font-size:14px;">IC 累计曲线图 (Cumulative IC)</div></div>`:''}
                     ${getImg('icAutocorr')?`<div class="chart-box"><img src="${getImg('icAutocorr')}"><div style="text-align:center; margin-top:10px; color:#333; font-weight:bold; font-size:14px;">IC 自相关系数图 (IC Autocorrelation)</div></div>`:''}
</div>
                </div>

                <div style="margin-top: 50px; text-align: center; color: #64748b; font-size: 12px; border-top: 1px solid #334155; padding-top: 20px;">
                    报告生成时间：${new Date().toLocaleString()} | 量化回测系统自动生成
                </div>
            </div></body></html>`;

            // 使用 Data URI 下载
            const dataUri = 'data:application/octet-stream;charset=utf-8,' + encodeURIComponent(html);
            const link = document.createElement('a');
            link.href = dataUri;
            link.download = `完整回测报告_${Date.now()}.html`;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        } catch(e) {
            console.error('生成报告失败:', e);
            alert('生成失败: '+e.message);
        }
        finally { btn.textContent = originalText; btn.disabled = false; }
    }, 100);
}

function showDownloadButton() {
    if (document.getElementById('dynamic-download-btn')) return;
    const wrapper = document.createElement('div');
    wrapper.style.cssText = 'display: flex; justify-content: flex-end; margin-bottom: 15px; padding-top: 10px;';
    const btn = document.createElement('button');
    btn.id = 'dynamic-download-btn';
    btn.textContent = '📥 下载完整分析报告';
    btn.onclick = downloadFullReport;
    btn.style.cssText = 'background-color: #4f46e5; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: bold;';
    wrapper.appendChild(btn);
    const target = document.getElementById('factor-summary-section');
    if (target && target.parentNode) {
        target.parentNode.insertBefore(wrapper, target);
    }
}
// ========================================
// 旧的相关性分析函数已被删除，使用新的简化版函数
// ========================================
