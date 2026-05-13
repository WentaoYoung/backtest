from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from web.services.factor_repository import BaseFactorRepository


class FactorDataGateway:
    """统一因子数据访问入口：优先本地 DuckDB/parquet，失败回退 MySQL。"""

    def __init__(self, repository: BaseFactorRepository, db_provider: Callable[[], Any]):
        self.repository = repository
        self.db_provider = db_provider

    def resolve_local_parquet_path(self, table_name: Optional[str] = None) -> Optional[str]:
        return self.repository.resolve_factor_parquet_path(table_name)

    def load_single_factor(
        self,
        factor_name: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        table_name: Optional[str] = None,
    ) -> Tuple[Optional[pd.DataFrame], str]:
        factor_df = self.repository.load_factor_from_parquet(
            factor_name=factor_name,
            start_date=start_date,
            end_date=end_date,
            table_name=table_name,
        )
        if factor_df is not None:
            return factor_df, "duckdb_parquet"

        db = self.db_provider()
        return db.load_factor(factor_name, start_date, end_date, table_name=table_name), "database"

    def load_multiple_factors(
        self,
        factor_names: List[str],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        table_name: Optional[str] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Tuple[Optional[pd.DataFrame], str]:
        df = self.repository.load_multiple_factors_from_parquet(
            factor_names=factor_names,
            start_date=start_date,
            end_date=end_date,
            table_name=table_name,
            progress_callback=progress_callback,
        )
        if df is not None:
            return df, "duckdb_parquet"

        db = self.db_provider()
        return (
            db.load_multiple_factors(
                factor_names,
                start_date=start_date,
                end_date=end_date,
                table_name=table_name,
                progress_callback=progress_callback,
            ),
            "database",
        )
