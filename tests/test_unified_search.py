"""Synthetic local search fixtures; no installed Cadence or PDK data."""
import threading

import pytest

from virtuoso_bridge import unified_search as search


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


def test_skill_hit_has_inspectable_nested_source_path(tmp_path):
    nested = tmp_path / 'area' / 'nested'
    nested.mkdir(parents=True)
    source = nested / 'api.fnd'
    source.write_text('(\"mySkill\"\n\"mySkill()\"\n\"Description\")\n', encoding='utf-8')
    rows = list(search.collect_skill({'skill': [tmp_path]}, 1000))
    assert rows[0]['path'] == str(source.resolve())
