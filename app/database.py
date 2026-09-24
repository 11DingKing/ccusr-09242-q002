import time
from typing import Any, Callable, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

from .config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},
)

# 单个写事务拿锁的等待上限与应用层重试次数。事务体都很小，串行等待通常
# 在毫秒级；超过 busy_timeout 仍拿不到锁时，应用层整体重来一次，
# 以便读到对方已提交的状态，给出语义化冲突（如重复审批）而不是数据库错误。
SQLITE_BUSY_TIMEOUT_MS = 2000
WRITE_LOCK_RETRIES = 3


def install_sqlite_write_guards(target_engine) -> None:
    """为 SQLite 引擎安装显式事务与写锁守卫。

    SQLite 默认会在首个写操作时静默把 DEFERRED 事务升级为保留锁，两个并发
    事务都"先读后写"会在 commit 时撞 SQLITE_BUSY。这里关闭 pysqlite 的隐式
    事务，改为由 begin 事件显式发出 BEGIN，写事务统一用 BEGIN IMMEDIATE
    在事务起点即取得写锁，把并发写入串行化并给出可解释的冲突错误。
    """

    @event.listens_for(target_engine, "connect")
    def _sqlite_connect(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.close()

    @event.listens_for(target_engine, "begin")
    def _sqlite_begin(conn):
        mode = conn.get_execution_options().get("sqlite_tx_begin", "DEFERRED")
        if mode not in ("DEFERRED", "IMMEDIATE", "EXCLUSIVE"):
            mode = "DEFERRED"
        conn.exec_driver_sql(f"BEGIN {mode}")


if engine.dialect.name == "sqlite":
    install_sqlite_write_guards(engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _is_lock_error(exc: OperationalError) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text


def run_immediate(unit_of_work: Callable[[Session], Any]) -> Any:
    """在串行化写事务中执行 unit_of_work(db)，锁冲突时整体重试。

    重试会重新开启事务并重新读取最新状态，因此败者在重试时能看到胜者已
    提交的意见/轮次，进而抛出业务语义冲突（如 REVIEW_DUPLICATE），
    而不是把数据库锁错误直接暴露给调用方。
    """
    last_exc: Optional[OperationalError] = None
    for attempt in range(WRITE_LOCK_RETRIES):
        db = SessionLocal()
        try:
            if engine.dialect.name == "sqlite":
                db.connection(execution_options={"sqlite_tx_begin": "IMMEDIATE"})
            result = unit_of_work(db)
            db.commit()
            return result
        except OperationalError as exc:
            db.rollback()
            last_exc = exc
            if not _is_lock_error(exc) or attempt == WRITE_LOCK_RETRIES - 1:
                raise
            time.sleep(0.02 * (attempt + 1))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    raise last_exc  # pragma: no cover
