"""Standalone preview tests with synthetic data only."""
import importlib.util
from html.parser import HTMLParser
import json
import os
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

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


class ReportMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


@pytest.mark.parametrize('filename', [
    'shot #100% & snow 雪.png',
    pytest.param('shot" onerror="alert(1)" x=".png', marks=pytest.mark.skipif(
        os.name == 'nt', reason='Windows forbids quotes in filenames')),
    pytest.param('<svg onload=alert(2)>.png', marks=pytest.mark.skipif(
        os.name == 'nt', reason='Windows forbids angle brackets in filenames')),
])
def test_filenames_are_data_in_gallery_and_revision(tmp_path, monkeypatch, filename):
    preview = tmp_path / 'preview'
    shots = preview / 'shots'
    shots.mkdir(parents=True)
    # Captures are synthetic; the renderer copies bytes without decoding them.
    (shots / filename).write_bytes(b'synthetic capture')
    monkeypatch.setattr(signoff, 'PREVIEW', preview)
    monkeypatch.setattr(signoff, 'STATE', preview / 'data/state.json')
    monkeypatch.setattr(signoff, 'NOTELOG', preview / 'data/notes.jsonl')
    out = preview / 'report'

    def assert_safe(expected_images):
        markup = ReportMarkup()
        markup.feed((out / 'index.html').read_text(encoding='utf-8'))
        assert not any(tag in ('svg', 'script') for tag, _ in markup.elements)
        assert not any(key.lower().startswith('on') for _, attrs in markup.elements
                       for key in attrs)
        images = [attrs for tag, attrs in markup.elements if tag == 'img']
        assert len(images) == expected_images
        return images

    first = signoff.build(out, 'Review')
    assert first['added'] == [filename]
    image, = assert_safe(1)
    assert image['src'] == quote(filename, safe='')
    assert unquote(image['src']) == filename
    assert (out / unquote(image['src'])).read_bytes() == b'synthetic capture'

    # Both changed and removed revision names must remain plain text too.
    (shots / filename).write_bytes(b'changed synthetic capture')
    assert signoff.build(out, 'Review')['changed'] == [filename]
    assert_safe(1)
    assert signoff.build(out, 'Review', only_shots=['missing.png'])['removed'] == [filename]
    assert_safe(0)


def test_summaries_identify_supplied_files_not_invented_live_runs(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'wave.json').write_text('{"vout": [1, 1]}', encoding='utf-8')
    (data / 'netlist_check.txt').write_text('synthetic finding', encoding='utf-8')
    (data / 'tools.json').write_text('{"tools": [{}]}', encoding='utf-8')
    monkeypatch.setattr(signoff, 'PREVIEW', tmp_path)
    rows = signoff.sim_results()
    assert [row['run'] for row in rows] == ['wave.json', 'netlist_check.txt', 'bridge']
    assert rows[-1]['detail'] == '1 tools in supplied snapshot'
