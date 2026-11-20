"""Enforce the SQL boundary across compatibility and new modules alike."""

import ast
from pathlib import Path


def test_sql_execution_stays_in_sqlite_adapters():
    root=Path(__file__).resolve().parents[1]/'src'/'pozor'
    violations=[]
    for file in root.rglob('*.py'):
        if file.is_relative_to(root/'infra'/'sqlite'):
            continue
        tree=ast.parse(file.read_text('utf-8'))
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr in {'execute','executemany','executescript'}:
                violations.append(f'{file.relative_to(root)}:{node.lineno}')
    assert violations==[]
