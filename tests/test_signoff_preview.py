"""Standalone preview tests with synthetic data only."""
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location('signoff', Path(__file__).resolve().parents[1] / 'tools/signoff.py')
signoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(signoff)


def test_preview_statistics_escaping_and_revision(tmp_path, monkeypatch):
    preview = tmp_path / 'preview'
    data = preview / 'data'
    data.mkdir(parents=True)
    monkeypatch.setattr(signoff, 'PREVIEW', preview)
    monkeypatch.setattr(signoff, 'STATE', data / 'state.json')
    monkeypatch.setattr(signoff, 'NOTELOG', data / 'notes.jsonl')
    wave = data / 'wave.json'
    wave.write_text(json.dumps({'time': [0, 1, 2], '<out>': [1, 1, 1]}))
    assert signoff.wave_stats(wave)[0]['rms'] == 1
    out = preview / 'report'
    first = signoff.build(out, '<Review>', wave=wave)
    page = (out / 'index.html').read_text()
    assert '&lt;Review&gt;' in page and '&lt;OUT&gt;' in page
    assert 'Signed off' not in page
    assert first['rev'] == 1
    assert signoff.build(out, 'Review', wave=wave)['rev'] == 2
