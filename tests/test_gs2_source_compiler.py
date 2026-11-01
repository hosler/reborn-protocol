"""Shared optional compiler discovery, caching, and error reporting."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from reborn_protocol.gs2 import compiler


def test_locator_prefers_environment_then_checkout_then_path(tmp_path, monkeypatch):
    fake_module = tmp_path / 'reborn_protocol' / 'gs2' / 'compiler.py'
    monkeypatch.setattr(compiler, '__file__', str(fake_module))
    build = tmp_path / 'tests' / 'tools' / 'gs2test'
    build.parent.mkdir(parents=True)
    override = tmp_path / 'override'
    on_path = tmp_path / 'path-compiler'
    for path in (build, override, on_path):
        path.write_text('')
        path.chmod(0o755)
    monkeypatch.setenv('GS2TEST_BIN', str(override))
    monkeypatch.setattr(compiler.shutil, 'which', lambda _: str(on_path))
    assert compiler.find_compiler() == str(override)
    override.chmod(0o644)
    assert compiler.find_compiler() == str(build)
    build.unlink()
    assert compiler.find_compiler() == str(on_path)
    on_path.unlink()
    assert compiler.find_compiler() is None


def test_cache_reuses_source_across_names_and_recompiles_changes(monkeypatch):
    def run(args, **kwargs):
        Path(args[-1]).write_bytes(Path(args[1]).read_bytes())
        return SimpleNamespace(returncode=0, stderr='', stdout='')
    run = Mock(side_effect=run)
    monkeypatch.setattr(compiler.subprocess, 'run', run)
    wrapper = compiler.Gs2Compiler(binary='/fake/gs2test')
    assert wrapper.compile('temp.x = 1;', 'one') == b'temp.x = 1;'
    assert wrapper.compile('temp.x = 1;', 'two') == b'temp.x = 1;'
    assert run.call_count == 1
    assert wrapper.compile('temp.x = 2;', 'one') == b'temp.x = 2;'
    assert run.call_count == 2


def test_failed_compile_is_cached_with_first_diagnostic(monkeypatch):
    run = Mock(return_value=SimpleNamespace(
        returncode=1, stderr='syntax error\nmore detail', stdout=''))
    monkeypatch.setattr(compiler.subprocess, 'run', run)
    wrapper = compiler.Gs2Compiler(binary='/fake/gs2test')
    for name in ('one', 'two'):
        assert wrapper.compile('broken;', name) is None
        assert wrapper.last_error == 'syntax error'
    run.assert_called_once()


@pytest.mark.parametrize('error', [OSError('missing binary'), subprocess.TimeoutExpired('gs2test', 30)])
def test_process_failures_return_none_with_diagnostic(monkeypatch, error):
    monkeypatch.setattr(compiler.subprocess, 'run', Mock(side_effect=error))
    wrapper = compiler.Gs2Compiler(binary='/fake/gs2test')
    assert wrapper.compile('temp.x = 1;') is None
    assert wrapper.last_error


def test_missing_compiler_and_empty_source_never_spawn(monkeypatch):
    run = Mock()
    monkeypatch.setattr(compiler.subprocess, 'run', run)
    monkeypatch.setattr(compiler, 'find_compiler', lambda: None)
    wrapper = compiler.Gs2Compiler()
    assert not wrapper.available
    assert wrapper.compile('temp.x = 1;') is None
    assert wrapper.last_error == 'gs2test compiler unavailable'
    wrapper.binary = '/fake/gs2test'
    assert wrapper.compile('  ') is None
    run.assert_not_called()
