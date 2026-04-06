"""
Shared pytest fixtures.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_conn():
    """A mock psycopg connection with a cursor that supports dict_row-style returns."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = lambda s: cursor
    cursor.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    return conn, cursor
