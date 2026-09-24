"""轻量数据库迁移：在启动时幂等地补齐历史库缺失的结构。

项目使用 SQLite，未引入 Alembic。``Base.metadata.create_all`` 只能创建缺失的
表，无法给已存在的表增加列，因此历史版本的 ``invest_ledger.db`` 升级后需要
在这里做与新增模型字段对应的幂等变更。每个迁移只执行一次，通过检查
``PRAGMA table_info`` 判断是否已经应用。
"""

from sqlalchemy import inspect, text

from .database import engine


# 增量列：表名 -> (列名, 列定义)
_ADDED_COLUMNS = {
    "cooperation_intents": [
        ("review_stage", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _table_columns(conn, table_name: str):
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table_name})"))}


def run_startup_migrations(bind_engine=None) -> None:
    target_engine = bind_engine or engine
    inspector = inspect(target_engine)
    existing_tables = set(inspector.get_table_names())
    with target_engine.begin() as conn:
        for table_name, columns in _ADDED_COLUMNS.items():
            if table_name not in existing_tables:
                # 全新数据库，create_all 已按最新模型建表，跳过。
                continue
            present = _table_columns(conn, table_name)
            for column_name, column_ddl in columns:
                if column_name not in present:
                    conn.execute(
                        text(
                            f"ALTER TABLE {table_name} "
                            f"ADD COLUMN {column_name} {column_ddl}"
                        )
                    )
