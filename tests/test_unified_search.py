"""Synthetic local search fixtures; no installed Cadence or PDK data."""
import multiprocessing
from pathlib import Path
import sqlite3
import threading

import pytest

from virtuoso_bridge import unified_search as search


def _build_files_after_other_writer(cache, files, ready, release):
    """Child process with its own build lock; pause at the SQLite write boundary."""
    search.cache_root = lambda: Path(cache)
    original_connect = sqlite3.connect

    class PausedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "BEGIN IMMEDIATE":
                ready.set()
                if not release.wait(10):
                    raise TimeoutError("other index writer did not finish")
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        return original_connect(*args, factory=PausedConnection, **kwargs)

    sqlite3.connect = connect
    try:
        search.build(kinds=['file'], roots={'file': [Path(files)]})
    finally:
        sqlite3.connect = original_connect


def test_build_search_refresh_and_kind_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(search, 'cache_root', lambda: tmp_path / 'cache')
    docs = tmp_path / 'docs'
    docs.mkdir()
    page = docs / 'example.html'
    page.write_text('<title>Synthetic capacitor</title><p>capacitance alpha</p>')
    files = tmp_path / 'project'
    files.mkdir()
    (files / 'model.scs').write_text('synthetic transistor beta')
    roots = {'doc': [docs], 'file': [files]}
    search.build(kinds=['doc', 'file'], roots=roots)
    assert search.search('capacitance', kinds=['doc'])
    assert not search.search('capacitance', kinds=['file'])
    assert search.search('transistor', kinds=['file'])
    page.write_text('<title>Synthetic capacitor</title><p>gamma</p>')
    search.build(kinds=['doc'], roots=roots)
    assert not search.search('capacitance')
    assert search.search('gamma', kinds=['doc'])
    assert search.search('transistor', kinds=['file'])


def test_interrupted_refresh_rolls_back_rows_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(search, 'cache_root', lambda: tmp_path / 'cache')
    def old(_roots, _limit):
        yield {'kind': 'doc', 'title': 'old', 'body': 'originalcapacitor'}
    monkeypatch.setattr(search, 'GROUPS', [('doc', ('doc',), old)])
    original = search.build(kinds=['doc'], roots={})
    def interrupted(_roots, _limit):
        for i in range(2001):
            yield {'kind': 'doc', 'title': str(i), 'body': 'replacementinductor'}
        raise RuntimeError('collector interrupted after first batch')
    monkeypatch.setattr(search, 'GROUPS', [('doc', ('doc',), interrupted)])
    with pytest.raises(RuntimeError, match='collector interrupted'):
        search.build(kinds=['doc'], roots={}, rebuild=True)
    assert search.search('originalcapacitor', kinds=['doc'])
    assert not search.search('replacementinductor', kinds=['doc'])
    assert search._read_manifest()['kinds'] == original['kinds']


def test_reader_sees_previous_complete_index_during_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(search, 'cache_root', lambda: tmp_path / 'cache')
    monkeypatch.setattr(search, 'GROUPS', [('doc', ('doc',), lambda _r, _n: iter([
        {'kind': 'doc', 'title': 'old', 'body': 'stablecapacitor'}]))])
    search.build(kinds=['doc'], roots={})
    entered = threading.Event()
    release = threading.Event()
    failures = []
    def delayed(_roots, _limit):
        for i in range(2000):
            yield {'kind': 'doc', 'title': str(i), 'body': 'newinductor'}
        entered.set()
        if not release.wait(5):
            raise TimeoutError('reader did not finish')
    monkeypatch.setattr(search, 'GROUPS', [('doc', ('doc',), delayed)])
    def build_in_thread():
        try:
            search.build(kinds=['doc'], roots={}, rebuild=True)
        except Exception as exc:
            failures.append(exc)
    worker = threading.Thread(target=build_in_thread)
    worker.start()
    try:
        assert entered.wait(5)
        assert search.search('stablecapacitor', kinds=['doc'])
        assert not search.search('newinductor', kinds=['doc'])
    finally:
        release.set()
        worker.join(5)
    assert not failures
    assert not search.search('stablecapacitor', kinds=['doc'])
    assert search.search('newinductor', kinds=['doc'])


def test_independent_writers_publish_counts_for_all_committed_kinds(tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    monkeypatch.setattr(search, 'cache_root', lambda: cache)
    skill = tmp_path / 'skill'
    docs = tmp_path / 'docs'
    files = tmp_path / 'project'
    for root in (skill, docs, files):
        root.mkdir()
    (skill / 'api.fnd').write_text(
        '("syntheticSkill"\n"syntheticSkill()"\n"syntheticskillfixture")\n',
        encoding='utf-8',
    )
    (docs / 'guide.html').write_text('<p>syntheticdocfixture</p>', encoding='utf-8')
    (files / 'model.scs').write_text('syntheticfilefixture', encoding='utf-8')
    search.build(kinds=['skill'], roots={'skill': [skill]})

    context = multiprocessing.get_context('spawn')
    ready, release = context.Event(), context.Event()
    worker = context.Process(
        target=_build_files_after_other_writer,
        args=(str(cache), str(files), ready, release),
    )
    worker.start()
    try:
        assert ready.wait(10), 'file writer never reached its transaction'
        # The file writer has opened the existing index but not acquired its
        # write lock. Another process can commit a new kind in this gap.
        search.build(kinds=['doc'], roots={'doc': [docs]})
        release.set()
        worker.join(10)
        assert not worker.is_alive(), 'file writer did not finish'
        assert worker.exitcode == 0
    finally:
        release.set()
        if worker.is_alive():
            worker.terminate()
        worker.join(5)

    for kind in ('skill', 'doc', 'file'):
        assert search.search(f'synthetic{kind}fixture', kinds=[kind])
    status = search.status()
    assert status['counts'] == {'skill': 1, 'doc': 1, 'file': 1}
    assert status['built_kinds'] == ['doc', 'file', 'skill']


def test_cds_includes_expand_complete_install_variable_and_stop_cycles(tmp_path, monkeypatch):
    install = tmp_path / 'cadence'
    lib = install / 'libs' / 'RF'
    lib.mkdir(parents=True)
    (lib / 'cdsinfo.tag').touch()
    setup = tmp_path / 'setup'
    setup.mkdir()
    main = setup / 'cds.lib'
    nested = setup / 'nested.cds'
    main.write_text('INCLUDE nested.cds\n', encoding='utf-8')
    nested.write_text('SOFTINCLUDE cds.lib\nDEFINE RF $CDS_INST_DIR/libs/RF\n', encoding='utf-8')
    monkeypatch.setenv('CDS_INST_DIR', str(install))
    monkeypatch.setattr(search, '_cds_lib_files', lambda: [main])
    assert ('RF', lib) in search._discover_libs({'pdk': []})


@pytest.mark.parametrize('keyword', ['INCLUDE', 'SOFTINCLUDE'])
@pytest.mark.parametrize('symlinked', [False, True])
def test_symlinked_cds_lib_indexes_relative_includes_and_defines(
    tmp_path, monkeypatch, keyword, symlinked
):
    """A relocated setup file still resolves paths beside its logical alias."""
    logical = tmp_path / 'setup'
    physical = tmp_path / 'legacy'
    nested = logical / 'dfII'
    nested.mkdir(parents=True)
    physical.mkdir()
    for name in ('SyntheticDirect', 'SyntheticNested'):
        lib = logical / name
        view = lib / 'syntheticCell' / 'symbol'
        view.mkdir(parents=True)
        (lib / 'syntheticCell' / 'data.dm').touch()
    alias = logical / 'cds.lib'
    target = physical / 'cds.lib' if symlinked else alias
    target.write_text(
        f'{keyword} dfII/cds.lib\nDEFINE SyntheticDirect SyntheticDirect\n',
        encoding='utf-8',
    )
    (nested / 'cds.lib').write_text(
        'SOFTINCLUDE ../cds.lib\nDEFINE SyntheticNested ../SyntheticNested\n',
        encoding='utf-8',
    )
    if symlinked:
        try:
            alias.symlink_to(target)
        except OSError as exc:
            pytest.skip(f'symlink creation unavailable: {exc}')
    monkeypatch.setattr(search, '_cds_lib_files', lambda: [alias])
    monkeypatch.setattr(search, 'cache_root', lambda: tmp_path / 'cache')
    assert dict(search._discover_libs({'pdk': []})) == {
        'SyntheticDirect': logical / 'SyntheticDirect',
        'SyntheticNested': nested / '../SyntheticNested',
    }
    search.build(kinds=['lib', 'cell'], roots={'pdk': []})
    for name in ('SyntheticDirect', 'SyntheticNested'):
        assert search.search(name, kinds=['lib']), name
        assert search.search(name, kinds=['cell']), name


def test_skill_hit_has_inspectable_nested_source_path(tmp_path):
    nested = tmp_path / 'area' / 'nested'
    nested.mkdir(parents=True)
    source = nested / 'api.fnd'
    source.write_text('(\"mySkill\"\n\"mySkill()\"\n\"Description\")\n', encoding='utf-8')
    rows = list(search.collect_skill({'skill': [tmp_path]}, 1000))
    assert rows[0]['path'] == str(source.resolve())
