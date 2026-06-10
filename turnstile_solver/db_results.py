"""
数据库结果存储模块 - 优化版
同步自 D3vin/Turnstile-Solver-NEW v1.2b

使用 SQLite + WAL 模式实现高性能异步存储
特性:
- WAL 模式提升并发性能
- PRAGMA 优化设置
- 自动清理过期数据
"""

import aiosqlite
import json
import logging
from typing import Dict, Any, Optional, Union

DB_PATH = "turnstile_results.db"

# PRAGMA 优化设置
PRAGMA_SETTINGS = [
    "PRAGMA journal_mode=WAL",      # WAL 模式提升并发性能
    "PRAGMA synchronous=NORMAL",    # 平衡安全性和性能
    "PRAGMA cache_size=10000",      # 增大缓存
    "PRAGMA temp_store=MEMORY",     # 临时表存内存
    "PRAGMA busy_timeout=30000"     # 忙等待超时 30s
]


async def _apply_pragma_settings(db):
    """应用 PRAGMA 优化设置"""
    for pragma in PRAGMA_SETTINGS:
        await db.execute(pragma)


async def init_db():
    """初始化数据库 (WAL 模式)"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await _apply_pragma_settings(db)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS results (
                    task_id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()
            logging.getLogger("TurnstileAPIServer").info(f"数据库初始化完成 (WAL 模式): {DB_PATH}")
    except Exception as e:
        logging.getLogger("TurnstileAPIServer").error(f"数据库初始化失败: {e}")
        raise


async def save_result(task_id: str, task_type: str, data: Union[Dict[str, Any], str]) -> None:
    """保存结果到数据库"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await _apply_pragma_settings(db)

            data_json = json.dumps(data) if isinstance(data, dict) else data

            await db.execute(
                "REPLACE INTO results (task_id, type, data) VALUES (?, ?, ?)",
                (task_id, task_type, data_json)
            )
            await db.commit()
    except Exception as e:
        logging.getLogger("TurnstileAPIServer").error(f"保存结果失败 {task_id}: {e}")
        raise


async def load_result(task_id: str) -> Optional[Union[Dict[str, Any], str]]:
    """从数据库加载结果"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await _apply_pragma_settings(db)

            async with db.execute("SELECT data FROM results WHERE task_id = ?", (task_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    try:
                        return json.loads(row[0])
                    except json.JSONDecodeError:
                        return row[0]
        return None
    except Exception as e:
        logging.getLogger("TurnstileAPIServer").error(f"加载结果失败 {task_id}: {e}")
        return None


async def load_all_results() -> Dict[str, Any]:
    """加载所有结果"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await _apply_pragma_settings(db)

            results = {}
            async with db.execute("SELECT task_id, data FROM results") as cursor:
                async for row in cursor:
                    try:
                        results[row[0]] = json.loads(row[1])
                    except json.JSONDecodeError:
                        results[row[0]] = row[1]
            return results
    except Exception as e:
        logging.getLogger("TurnstileAPIServer").error(f"加载所有结果失败: {e}")
        return {}


async def delete_result(task_id: str) -> None:
    """删除指定结果"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await _apply_pragma_settings(db)
            await db.execute("DELETE FROM results WHERE task_id = ?", (task_id,))
            await db.commit()
    except Exception as e:
        logging.getLogger("TurnstileAPIServer").error(f"删除结果失败 {task_id}: {e}")


async def get_pending_count() -> int:
    """获取待处理任务数量"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await _apply_pragma_settings(db)

            async with db.execute("SELECT COUNT(*) FROM results WHERE data LIKE '%CAPTCHA_NOT_READY%'") as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0
    except Exception as e:
        logging.getLogger("TurnstileAPIServer").error(f"获取待处理数量失败: {e}")
        return 0


async def cleanup_old_results(days_old: int = 1) -> int:
    """清理过期结果"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await _apply_pragma_settings(db)

            cursor = await db.execute(
                f"DELETE FROM results WHERE created_at < datetime('now', '-{days_old} days')"
            )
            deleted_count = cursor.rowcount
            await db.commit()

            if deleted_count > 0:
                logging.getLogger("TurnstileAPIServer").info(f"清理了 {deleted_count} 条过期结果")
            return deleted_count
    except Exception as e:
        logging.getLogger("TurnstileAPIServer").error(f"清理过期结果失败: {e}")
        return 0


async def get_stats() -> Dict[str, int]:
    """获取统计信息"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await _apply_pragma_settings(db)

            # 总数
            async with db.execute("SELECT COUNT(*) FROM results") as cursor:
                total = (await cursor.fetchone())[0]

            # 待处理
            async with db.execute("SELECT COUNT(*) FROM results WHERE data LIKE '%CAPTCHA_NOT_READY%'") as cursor:
                pending = (await cursor.fetchone())[0]

            # 成功 (有 token 且不是失败)
            async with db.execute("SELECT COUNT(*) FROM results WHERE data NOT LIKE '%CAPTCHA_NOT_READY%' AND data NOT LIKE '%CAPTCHA_FAIL%'") as cursor:
                success = (await cursor.fetchone())[0]

            # 失败
            async with db.execute("SELECT COUNT(*) FROM results WHERE data LIKE '%CAPTCHA_FAIL%'") as cursor:
                failed = (await cursor.fetchone())[0]

            return {
                'total': total,
                'pending': pending,
                'success': success,
                'failed': failed
            }
    except Exception as e:
        logging.getLogger("TurnstileAPIServer").error(f"获取统计信息失败: {e}")
        return {'total': 0, 'pending': 0, 'success': 0, 'failed': 0}
