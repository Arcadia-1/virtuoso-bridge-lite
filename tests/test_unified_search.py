"""Synthetic local search fixtures; no installed Cadence or PDK data."""
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
