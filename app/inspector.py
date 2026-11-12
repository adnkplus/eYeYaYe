#!/usr/bin/env python3
"""Guarded source inspection, transactional edits, and staged dry-run validation."""
from __future__ import annotations
import ast
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _resolve_callable(callback, root: Path):
    """Resolve a Tk lambda to the actual bound project method it invokes."""
    if inspect.ismethod(callback) or inspect.isfunction(callback):
        if not (inspect.isfunction(callback) and callback.__name__ == '<lambda>'):
            return [callback]
    code = getattr(callback, '__code__', None)
    if code is None:
        return []
    owners = []
    for cell in getattr(callback, '__closure__', ()) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if value is not callback and (hasattr(value, '__dict__') or inspect.ismodule(value)):
            owners.append(value)
    for value in getattr(callback, '__defaults__', ()) or ():
        if hasattr(value, '__dict__'):
            owners.append(value)
    found = []
    for owner in owners:
        for name in code.co_names:
            try:
                candidate = getattr(owner, name)
            except Exception:
                continue
            if inspect.ismethod(candidate) or inspect.isfunction(candidate):
                filename = inspect.getsourcefile(candidate)
                if filename and _inside(Path(filename), root):
                    found.append(candidate)
    # Prefer the most specific project callback, not helper names such as
    # 'after', 'configure', or 'winfo_exists'.
    skip = {'after','after_cancel','configure','winfo_exists','winfo_ismapped',
            'set','get','pack','grid','place','destroy','update','update_idletasks'}
    unique = []
    seen = set()
    for item in found:
        if item.__name__ in skip:
            continue
        key = (inspect.getsourcefile(item), item.__name__, item.__code__.co_firstlineno)
        if key not in seen:
            seen.add(key); unique.append(item)
    return unique


def _function_span(source: str, callback):
    target = getattr(callback, '__func__', callback)
    code = getattr(target, '__code__', None)
    if code is None:
        raise ValueError('The selected control has no editable Python function.')
    tree = ast.parse(source)
    matches = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == code.co_name:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            end = getattr(node, 'end_lineno', node.lineno)
            if start <= code.co_firstlineno <= end or node.lineno <= code.co_firstlineno <= end:
                matches.append((start, end, node))
    if not matches:
        # Exact source line fallback for functions nested in class definitions.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == code.co_name:
                matches.append((node.lineno, getattr(node, 'end_lineno', node.lineno), node))
    if not matches:
        raise ValueError(f'Cannot locate {code.co_name} in its source file.')
    start, end, _node = min(matches, key=lambda item: abs(item[0] - code.co_firstlineno))
    lines = source.splitlines(keepends=True)
    return start, end, ''.join(lines[start-1:end])


def target_for(callback, root: Path):
    """Return an editable source-function descriptor for a project callback."""
    candidates = _resolve_callable(callback, root)
    for candidate in candidates:
        try:
            filename = inspect.getsourcefile(candidate)
            if not filename:
                continue
            path = Path(filename).resolve()
            if path.suffix != '.py' or not _inside(path, root):
                continue
            source = path.read_text(encoding='utf-8')
            start, end, snippet = _function_span(source, candidate)
            return {
                'path': path, 'name': candidate.__qualname__,
                'start_line': start, 'end_line': end,
                'original': snippet, 'source': source,
            }
        except (OSError, UnicodeError, SyntaxError, ValueError, TypeError):
            continue
    return None


def replace_target_text(root: Path, target: dict, edited: str) -> tuple[Path, str]:
    path = Path(target['path']).resolve()
    if not _inside(path, root) or path.suffix != '.py':
        raise ValueError('Inspector edits are restricted to Python files inside this project.')
    current = path.read_text(encoding='utf-8')
    lines = current.splitlines(keepends=True)
    start, end = int(target['start_line']), int(target['end_line'])
    current_snippet = ''.join(lines[start-1:end])
    if current_snippet != target['original']:
        raise RuntimeError('This source section changed on disk after inspection. Inspect it again before editing.')
    replacement = edited.replace('\r\n', '\n').replace('\r', '\n')
    if not replacement.endswith('\n'):
        replacement += '\n'
    updated = ''.join(lines[:start-1]) + replacement + ''.join(lines[end:])
    ast.parse(updated, filename=str(path))
    compile(updated, str(path), 'exec')
    return path, updated


def commit_target(root: Path, target: dict, edited: str) -> Path:
    """Atomically back up and commit an AST-valid function replacement."""
    path, updated = replace_target_text(root, target, edited)
    backups = (root / 'backups' / 'inspector').resolve()
    if not _inside(backups, root):
        raise ValueError('Inspector backup directory must remain inside the project.')
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime('%Y%m%d_%H%M%S_%f')
    backup = backups / f'{path.name}.{stamp}.bak'
    mode = path.stat().st_mode & 0o777
    old = path.read_bytes()
    backup.write_bytes(old)
    os.chmod(backup, mode)
    temporary = path.with_name(path.name + '.inspector.tmp')
    try:
        temporary.write_text(updated, encoding='utf-8')
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        compile(path.read_text(encoding='utf-8'), str(path), 'exec')
    except Exception:
        temporary.unlink(missing_ok=True)
        rollback = path.with_name(path.name + '.inspector.rollback')
        rollback.write_bytes(old); os.chmod(rollback, mode); os.replace(rollback, path)
        raise
    return backup


def dry_run(root: Path, target: dict, edited: str, timeout: int = 300) -> dict:
    """Validate staged project source in a disposable copy without writing live files."""
    root = root.resolve()
    with tempfile.TemporaryDirectory(prefix='eyeyaye-v9-dryrun-') as temp:
        stage = Path(temp) / root.name
        ignored_names = {
            '.git','__pycache__','.pytest_cache','backups','cache','data','logs','runtime',
            '.restore_tmp','.restore_recovery'
        }
        def ignore(_directory, names):
            return {name for name in names if name in ignored_names or name.endswith('.pyc')}
        shutil.copytree(root, stage, ignore=ignore)
        staged_target = dict(target)
        staged_path = stage / Path(target['path']).resolve().relative_to(root)
        staged_target['path'] = staged_path
        staged_target['source'] = staged_path.read_text(encoding='utf-8')
        # Rebase the optimistic-concurrency source snippet to the fresh staged copy.
        lines = staged_target['source'].splitlines(keepends=True)
        staged_target['original'] = ''.join(lines[int(target['start_line'])-1:int(target['end_line'])])
        _path, updated = replace_target_text(stage, staged_target, edited)
        staged_path.write_text(updated, encoding='utf-8')

        py_files = sorted(stage.rglob('*.py'))
        compile_errors = []
        for path in py_files:
            try:
                compile(path.read_text(encoding='utf-8'), str(path), 'exec')
            except Exception as exc:
                compile_errors.append(f'{path.relative_to(stage)}: {type(exc).__name__}: {exc}')
        if compile_errors:
            return {'ok': False, 'phase': 'compile', 'message': '\n'.join(compile_errors),
                    'python_files': len(py_files), 'tests': 'NOT RUN'}

        command = [sys.executable, '-m', 'pytest', '-q', 'tests']
        try:
            result = subprocess.run(command, cwd=stage, capture_output=True, text=True,
                                    timeout=timeout, env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'})
        except subprocess.TimeoutExpired as exc:
            output = ((exc.stdout or '') + '\n' + (exc.stderr or '')).strip()[-16000:]
            return {'ok': False, 'phase': 'tests-timeout', 'message': output or 'Test suite timed out.',
                    'python_files': len(py_files), 'tests': 'TIMEOUT'}
        except OSError as exc:
            return {'ok': False, 'phase': 'tests-start', 'message': str(exc),
                    'python_files': len(py_files), 'tests': 'NOT RUN'}
        output = (result.stdout + '\n' + result.stderr).strip()[-20000:]
        return {'ok': result.returncode == 0, 'phase': 'tests' if result.returncode else 'complete',
                'message': output or ('All staged tests passed.' if result.returncode == 0 else 'Test suite failed.'),
                'python_files': len(py_files), 'tests': 'PASSED' if result.returncode == 0 else f'FAILED ({result.returncode})'}
