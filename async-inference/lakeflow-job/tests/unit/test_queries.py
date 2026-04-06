"""
Unit tests for worker db queries using a mock connection.
Verifies the correct SQL is executed without a real database.
"""

import uuid
from unittest.mock import MagicMock

from worker.db import queries


def make_conn():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: cur
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur
    return conn, cur


# ── mark_streaming ────────────────────────────────────────────────────────────


def test_mark_streaming():
    conn, cur = make_conn()
    job_id = str(uuid.uuid4())

    queries.mark_streaming(conn, job_id)

    sql = cur.execute.call_args[0][0]
    assert "STREAMING" in sql
    conn.commit.assert_called_once()


# ── write_chunk ───────────────────────────────────────────────────────────────


def test_write_chunk():
    conn, cur = make_conn()
    job_id = str(uuid.uuid4())

    queries.write_chunk(conn, job_id, 3, "hello world")

    args = cur.execute.call_args[0][1]
    assert job_id in args
    assert 3 in args
    assert "hello world" in args
    conn.commit.assert_called_once()


# ── mark_done ─────────────────────────────────────────────────────────────────


def test_mark_done():
    conn, cur = make_conn()
    job_id = str(uuid.uuid4())

    queries.mark_done(conn, job_id, "full text", 500, 100, 12000)

    # Should have two execute calls: INSERT + UPDATE
    assert cur.execute.call_count == 2
    first_sql = cur.execute.call_args_list[0][0][0]
    second_sql = cur.execute.call_args_list[1][0][0]
    assert "job_results" in first_sql
    assert "DONE" in second_sql
    conn.commit.assert_called_once()


# ── mark_failed ───────────────────────────────────────────────────────────────


def test_mark_failed():
    conn, cur = make_conn()
    job_id = str(uuid.uuid4())

    queries.mark_failed(conn, job_id, "timeout error")

    sql = cur.execute.call_args[0][0]
    assert "FAILED" in sql
    args = cur.execute.call_args[0][1]
    assert "timeout error" in args
    conn.commit.assert_called_once()
