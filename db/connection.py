import os
import mysql.connector
from mysql.connector import pooling
from dotenv import load_dotenv

load_dotenv()

_pool = None

def _get_pool():
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name="home_pricing",
            pool_size=5,
            host=os.getenv("DB_HOST", "127.0.0.1"),
            port=int(os.getenv("DB_PORT", 3306)),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASS"),
            database=os.getenv("DB_NAME", "home_pricing"),
            autocommit=False,
        )
    return _pool


def get_conn():
    return _get_pool().get_connection()


def execute(sql: str, params=None, *, fetch=False):
    conn = get_conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params or ())
        if fetch:
            result = cur.fetchall()
            return result
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def executemany(sql: str, rows: list):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.executemany(sql, rows)
        conn.commit()
        return cur.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
