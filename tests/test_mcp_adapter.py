"""Offline adapter regressions. No Cadence, SSH or GUI is contacted."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import jsonschema
import pytest

from virtuoso_bridge.models import ExecutionStatus, SimulationResult, VirtuosoResult
from virtuoso_bridge.virtuoso.requests import RequestRecoveryError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vb_mcp_test', ROOT / 'mcp_server.py')
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)


@pytest.fixture(autouse=True)
def isolated_adapter(monkeypatch):
    monkeypatch.setattr(mcp, '_CLIENT', {'obj': None, 'at': 0.0})
    monkeypatch.setattr(mcp, '_RATE', {})


def test_timeout_never_replays_mutation(monkeypatch):
    client = SimpleNamespace(execute_skill=Mock(side_effect=TimeoutError('response lost')))
    factory = Mock(return_value=client)
    monkeypatch.setattr(mcp, '_client', factory)
    response = mcp.dispatch_tool('evaluate', {'expr': 'mutate()'}, {})
    assert response['isError']
    assert response['structuredContent']['outcome'] == 'unknown'
    client.execute_skill.assert_called_once_with('mutate()', timeout=60, retry_connect=False)
    factory.assert_called_once()


def test_all_adapter_calls_disable_bridge_replay(monkeypatch):
    execute = Mock(return_value=VirtuosoResult(status=ExecutionStatus.SUCCESS))
    monkeypatch.setattr(mcp.VirtuosoClient, 'execute_skill', execute)
    client = object.__new__(mcp._AdapterClient)
    client.execute_skill('mutate()', timeout=9, retry_connect=True)
    execute.assert_called_once_with('mutate()', timeout=9, retry_connect=False)


def uncertain_result():
    return VirtuosoResult(status=ExecutionStatus.ERROR, errors=['response pending'],
        metadata={'outcome': 'unknown', 'request_handle': {
            'request_id': 'a' * 32, 'daemon_instance': 'b' * 32, 'virtuoso_pid': 42},
            'history': 'Interactive.7', 'session': 'session-1'})


@pytest.mark.parametrize('raised', [False, True])
def test_original_request_diagnostics_survive(monkeypatch, raised):
    result = uncertain_result()
    execute = Mock(side_effect=RequestRecoveryError(result)) if raised else Mock(return_value=result)
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(execute_skill=execute))
    response = mcp.dispatch_tool('evaluate', {'expr': 'mutate()'}, {})
    assert response['isError']
    assert response['structuredContent'] == result.model_dump(mode='json')
    assert execute.call_count == 1


def test_cache_keeps_original_client_for_receipts(monkeypatch):
    factory = Mock(return_value=object())
    monkeypatch.setattr(mcp._AdapterClient, 'from_env', factory)
    first = mcp._client()
    mcp._CLIENT['at'] -= 10000
    assert mcp._client() is first
    factory.assert_called_once_with()


def test_start_delegates_to_registered_cli_with_environment(monkeypatch, tmp_path):
    monkeypatch.setenv('VB_REMOTE_HOST', 'configured.example')
    monkeypatch.setenv('VB_REMOTE_PORT', '45678')
    monkeypatch.setenv('VB_VIRTUOSO_BIN', '/site/wrapper')
    run = Mock(return_value=subprocess.CompletedProcess([], 0, 'load("setup.il")', ''))
    monkeypatch.setattr(subprocess, 'run', run)
    response = mcp._impl_bridge_start({})
    argv = run.call_args.args[0]
    assert Path(argv[0]).name in ('virtuoso-bridge', 'virtuoso-bridge.exe')
    assert argv[1:] == ['start']
    assert 'env' not in run.call_args.kwargs
    assert 'cwd' not in run.call_args.kwargs
    assert response['structuredContent']['setup_complete']
    assert 'load("setup.il")' in response['structuredContent']['output']


def test_real_registered_entrypoint_is_available():
    executable = Path(sys.executable).with_name(
        'virtuoso-bridge.exe' if sys.platform == 'win32' else 'virtuoso-bridge')
    result = subprocess.run([str(executable), '--help'], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'start' in result.stdout


@pytest.mark.parametrize('replace', [False, True])
def test_new_schematic_guards_existing_view_in_same_batch(monkeypatch, replace):
    from virtuoso_bridge.virtuoso.schematic import SchematicOps
    execute = Mock(return_value=VirtuosoResult(status=ExecutionStatus.SUCCESS))
    client = SimpleNamespace(execute_operations=execute)
    client.schematic = SchematicOps(client)
    monkeypatch.setattr(mcp, '_client', lambda: client)
    args = {'lib': 'demo', 'cell': 'test'}
    if replace:
        args['replace'] = True
    mcp._impl_schematic_create(args)
    commands = execute.call_args.args[0]
    if replace:
        assert 'ddGetObj' not in commands[0]
    else:
        assert 'ddGetObj("demo" "test" "schematic")' in commands[0]
        assert 'error(' in commands[0]
        assert 'dbOpenCellViewByType' in commands[1]
    execute.assert_called_once()


@pytest.mark.parametrize('fresh,replace', [(None, False), (True, False), (False, False), (True, True)])
def test_batch_uses_existing_editor_with_explicit_overwrite_policy(monkeypatch, fresh, replace):
    from virtuoso_bridge.virtuoso.schematic import SchematicOps
    execute = Mock(return_value=VirtuosoResult(status=ExecutionStatus.SUCCESS))
    client = SimpleNamespace(execute_operations=execute)
    client.schematic = SchematicOps(client)
    monkeypatch.setattr(mcp, '_client', lambda: client)
    args = {'lib': 'demo', 'cell': 'test', 'ops': [], 'replace': replace}
    if fresh is not None:
        args['fresh'] = fresh
    mcp._impl_schematic_build(args)
    commands = execute.call_args.args[0]
    assert ('ddGetObj' in commands[0]) == (fresh is not False and not replace)
    assert any(('"a")' if fresh is False else '"w")') in c for c in commands)


def test_maestro_reads_explicit_session_and_history(monkeypatch):
    read = Mock(return_value={'history': 'Interactive.3'})
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(maestro=SimpleNamespace(read_results=read)))
    mcp._impl_maestro_read({'session': 's1', 'history': 'Interactive.3'})
    read.assert_called_once_with('s1', history='Interactive.3')


@pytest.mark.parametrize('status,history,reads', [('done', '"Interactive.4"', 1),
    ('running', 'Interactive.4', 0), ('done', 'nil', 0)])
def test_maestro_reads_only_confirmed_run(monkeypatch, status, history, reads):
    events = []
    def run(**kwargs):
        events.append(('run', kwargs))
        return history, status
    read = Mock(side_effect=lambda *a, **kw: events.append(('read', a, kw)) or {'outputs': []})
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(
        maestro=SimpleNamespace(run_and_wait=run, read_results=read)))
    response = mcp._impl_maestro_run({'session': 's1', 'timeout': 300})
    assert events[0] == ('run', {'session': 's1', 'timeout': 300})
    assert read.call_count == reads
    if reads:
        read.assert_called_once_with('s1', history='Interactive.4')
    else:
        assert response['isError']


def test_maestro_start_uncertainty_is_not_retried(monkeypatch):
    result = uncertain_result()
    maestro = SimpleNamespace(run_and_wait=Mock(side_effect=RequestRecoveryError(result)), read_results=Mock())
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(maestro=maestro))
    response = mcp.dispatch_tool('run_test', {'session': 's1'}, {})
    assert response['structuredContent']['metadata'] == result.metadata
    maestro.run_and_wait.assert_called_once()
    maestro.read_results.assert_not_called()


def test_waveform_samples_exact_nearest_and_boundaries(monkeypatch, tmp_path):
    path = tmp_path / 'wave.tran'
    path.touch()
    monkeypatch.setattr(mcp, '_safe_path', lambda *a, **kw: path)
    monkeypatch.setattr(mcp, '_wave_png', lambda *a, **kw: None)
    from virtuoso_bridge.spectre import parsers
    monkeypatch.setattr(parsers, 'parse_spectre_psf_ascii', lambda *a, **kw: SimpleNamespace(
        data={'time': [0., 1., 2.], 'OUT': [10., 20., 30.]}))
    response = mcp._impl_psf_read({'path': str(path), 'signals': ['OUT'],
                                  'at': [-1, 0, .4, .5, .8, 1, 2, 9]})
    rows = response['structuredContent']['samples']
    assert [r['t'] for r in rows] == [0, 0, 0, 0, 1, 1, 2, 2]
    assert [r['OUT'] for r in rows] == [10, 10, 10, 10, 20, 20, 30, 30]


def test_tiny_waveform_sample_keeps_full_precision(monkeypatch, tmp_path):
    path = tmp_path / 'wave.tran'
    path.touch()
    monkeypatch.setattr(mcp, '_safe_path', lambda *a, **kw: path)
    monkeypatch.setattr(mcp, '_wave_png', lambda *a, **kw: None)
    from virtuoso_bridge.spectre import parsers
    monkeypatch.setattr(parsers, 'parse_spectre_psf_ascii', lambda *a, **kw: SimpleNamespace(
        data={'time': [0., 1.], 'I': [0., 2e-9]}))
    response = mcp._impl_psf_read({'path': str(path), 'signals': ['I'], 'at': [1.]})
    assert response['structuredContent']['samples'][0]['I'] == 2e-9


def test_waveform_extrema_cover_samples_after_old_display_limit(monkeypatch, tmp_path):
    path = tmp_path / 'wave.tran'
    path.touch()
    monkeypatch.setattr(mcp, '_safe_path', lambda *a, **kw: path)
    monkeypatch.setattr(mcp, '_wave_png', lambda *a, **kw: None)
    from virtuoso_bridge.spectre import parsers
    monkeypatch.setattr(parsers, 'parse_spectre_psf_ascii', lambda *a, **kw: SimpleNamespace(
        data={'time': list(range(100001)), 'I': [0.] * 100000 + [2e-9]}))
    response = mcp._impl_psf_read({'path': str(path), 'signals': ['I']})
    stats = response['structuredContent']['stats']['I']
    assert stats['numeric'] == 100001
    assert stats['max'] == 2e-9


@pytest.mark.parametrize('name,args', [('list_libraries', {}), ('list_cells', {'lib': 'L'})])
def test_library_queries_preserve_uncertain_receipt(monkeypatch, name, args):
    result = uncertain_result()
    monkeypatch.setattr(mcp, '_eval_skill', lambda *a, **kw: result)
    response = mcp.dispatch_tool(name, args, {})
    assert response['isError']
    assert response['structuredContent']['metadata']['request_handle'] == result.metadata['request_handle']


def test_check_schematic_marks_uncertain_save_and_keeps_receipt(monkeypatch):
    from virtuoso_bridge.virtuoso.schematic.diagnostics import SchematicCheckSaveResult
    result = SchematicCheckSaveResult(
        status='operation_error', lib='L', cell='C', view='schematic',
        checked=False, saved=False, check_error_count=0, check_warning_count=0,
        log_capture_available=False, request_metadata=uncertain_result().metadata)
    check = Mock(return_value=result)
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(
        schematic=SimpleNamespace(check_and_save=check)))
    monkeypatch.setattr(mcp, '_editor_shot', lambda: None)
    response = mcp.dispatch_tool('check_schematic', {'lib': 'L', 'cell': 'C'}, {})
    assert response['isError']
    assert response['structuredContent']['request_metadata']['request_handle']
    assert response['structuredContent']['saved'] is False
    check.assert_called_once_with('L', 'C')


def test_screenshot_returns_real_png_block_and_original_failure(monkeypatch, tmp_path):
    from virtuoso_bridge.models import VirtuosoResult, ExecutionStatus
    shot = tmp_path / 'shot.png'
    shot.write_bytes(mcp._wave_png({'time': [0., 1.], 'OUT': [0., 1.]}))
    monkeypatch.setattr(mcp, '_safe_out_dir', lambda *a, **kw: tmp_path)
    take = Mock(return_value=VirtuosoResult(status=ExecutionStatus.SUCCESS, output=str(shot)))
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(screenshot=take))
    response = mcp.dispatch_tool('screenshot', {'target': 'schematic'}, {})
    assert any(part['type'] == 'image' and part['mimeType'] == 'image/png'
               for part in response['content'])
    assert response['structuredContent']['file'] == str(shot.resolve())
    take.assert_called_once_with(output=tmp_path, target='schematic', timeout=120)
    take.return_value = uncertain_result()
    response = mcp.dispatch_tool('screenshot', {'target': 'schematic'}, {})
    assert response['isError']
    assert response['structuredContent']['metadata']['request_handle']


def test_screenshot_rejects_target_path_escape_before_capture(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, '_safe_out_dir', lambda *a, **kw: tmp_path)
    capture = Mock()
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(screenshot=capture))
    response = mcp.dispatch_tool('screenshot', {'target': '../../outside'}, {})
    assert response['isError']
    capture.assert_not_called()


def test_load_file_uses_local_tunnel_without_ssh_upload(monkeypatch, tmp_path):
    from virtuoso_bridge.transport.tunnel import SSHClient
    source = tmp_path / 'params.il'
    source.write_text('1 + 2\n', encoding='utf-8')
    tunnel = SSHClient(remote_host='localhost', port=65432)
    assert tunnel.ssh_runner is None
    client = mcp._AdapterClient.from_tunnel(tunnel)
    client._log_to_ciw = False
    execute = Mock(return_value=VirtuosoResult(status=ExecutionStatus.SUCCESS))
    monkeypatch.setattr(client, 'execute_skill', execute)
    monkeypatch.setattr(mcp, '_safe_roots', lambda: [tmp_path])
    monkeypatch.setattr(mcp, '_client', lambda: client)
    response = mcp.dispatch_tool('load_file', {'file': str(source)}, {})
    assert not response.get('isError')
    assert response['structuredContent']['metadata']['uploaded'] is False
    assert 'load(' in execute.call_args.args[0]


def test_snapshot_uses_registered_cli_and_reports_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, '_safe_out_dir', lambda *a, **kw: tmp_path)
    run = Mock(return_value=subprocess.CompletedProcess([], 1, '', 'snapshot failed'))
    monkeypatch.setattr(subprocess, 'run', run)
    response = mcp._impl_snapshot({})
    assert response['isError']
    assert 'snapshot failed' in response['content'][0]['text']
    assert Path(run.call_args.args[0][0]).name in ('virtuoso-bridge', 'virtuoso-bridge.exe')


def test_wave_plot_uses_elapsed_time_and_retains_short_peak():
    import struct
    import zlib
    png = mcp._wave_png({'time': [0., 1., 2., 100.], 'V': [0., 1., 0., 0.]},
                        width=120, height=50)
    assert png and png.startswith(b'\x89PNG')
    pos = 8
    chunks = []
    while pos < len(png):
        size = struct.unpack('>I', png[pos:pos + 4])[0]
        tag = png[pos + 4:pos + 8]
        if tag == b'IDAT':
            chunks.append(png[pos + 8:pos + 8 + size])
        pos += 12 + size
    rows = zlib.decompress(b''.join(chunks))
    width = 120
    def pixel(x, y):
        offset = y * (1 + width * 3) + 1 + x * 3
        return rows[offset:offset + 3]
    # The peak is at t=1/100, near the left edge, rather than at sample 1/3.
    blue = bytes((37, 99, 235))
    peak_xs = [x for x in range(width) if pixel(x, 8) == blue]
    assert peak_xs and max(peak_xs) < 20


def test_spectre_failure_keeps_run_diagnostics(monkeypatch, tmp_path):
    netlist = tmp_path / 'amp.scs'
    netlist.touch()
    monkeypatch.setattr(mcp, '_safe_roots', lambda: [tmp_path])
    failed = SimulationResult(status=ExecutionStatus.ERROR,
        errors=['license checkout failed'], warnings=['site warning'],
        metadata={'output_dir': '/runs/amp.raw', 'returncode': 1})
    monkeypatch.setattr(mcp, '_spectre_sim', lambda *a: SimpleNamespace(
        run_simulation=Mock(return_value=failed)))
    response = mcp._impl_spectre_run({'netlist': str(netlist)})
    assert response['isError']
    details = response['structuredContent']
    assert details['netlist'] == str(netlist)
    assert details['errors'] == ['license checkout failed']
    assert details['metadata']['returncode'] == 1


def test_parallel_run_exposes_success_image_and_failed_run(monkeypatch, tmp_path):
    first = tmp_path / 'first.scs'
    second = tmp_path / 'second.scs'
    first.touch()
    second.touch()
    monkeypatch.setattr(mcp, '_safe_roots', lambda: [tmp_path])
    successful = SimulationResult(status=ExecutionStatus.SUCCESS,
        data={'time': [0., 1., 2.], 'VOUT': [0., 1., 0.]},
        metadata={'output_dir': '/runs/first.raw'})
    failed = SimulationResult(status=ExecutionStatus.ERROR, errors=['bad model'])
    monkeypatch.setattr(mcp, '_spectre_sim', lambda *a: SimpleNamespace(
        run_parallel=Mock(return_value=[successful, failed])))
    response = mcp._impl_spectre_parallel({'netlists': [str(first), str(second)]})
    assert response['isError']
    assert response['structuredContent']['runs'][1]['errors'] == ['bad model']
    assert any(item['type'] == 'image' for item in response['content'])


def wire(requests):
    env = dict(__import__('os').environ)
    env['PYTHONPATH'] = str(ROOT / 'src')
    result = subprocess.run([sys.executable, str(ROOT / 'mcp_server.py')],
        input=''.join(json.dumps(r) + '\n' for r in requests),
        capture_output=True, text=True, timeout=20, cwd=ROOT, env=env)
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_discovery_and_tools_validate_against_official_schema():
    schema = json.loads((ROOT / 'tests/fixtures/mcp/2026-07-28.schema.json').read_text())
    responses = wire([{'jsonrpc': '2.0', 'id': i, 'method': method,
        'params': {'_meta': {mcp.META_VER: '2026-07-28'}}}
        for i, method in enumerate(['server/discover', 'tools/list'])])
    for response, name in zip(responses, ['DiscoverResultResponse', 'ListToolsResultResponse']):
        contract = {**schema, '$ref': '#/$defs/' + name}
        jsonschema.validate(response, contract)
    assert 'io.modelcontextprotocol/tasks' not in responses[0]['result']['capabilities']['extensions']


@pytest.mark.parametrize('version,expected', [('2024-11-05', '2024-11-05'),
    ('2025-11-25', '2025-11-25'), ('2099-01-01', '2025-11-25')])
def test_legacy_negotiation(version, expected):
    response, = wire([{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                      'params': {'protocolVersion': version, 'capabilities': {},
                                 'clientInfo': {'name': 'test', 'version': '1'}}}])
    assert response['result']['protocolVersion'] == expected


@pytest.mark.parametrize('method', ['tools/list', 'server/discover'])
def test_unsupported_modern_version_lists_supported_versions(method):
    response, = wire([{'jsonrpc': '2.0', 'id': 1, 'method': method,
        'params': {'_meta': {mcp.META_VER: '2099-01-01'}}}])
    assert response['error']['code'] == -32022
    assert response['error']['data'] == {'supported': mcp.SUPPORTED_VERSIONS, 'requested': '2099-01-01'}


def test_old_maestro_arguments_cannot_start_all_tests(monkeypatch):
    client = Mock()
    monkeypatch.setattr(mcp, '_client', client)
    response = mcp.dispatch_tool('run_test', {'test': 'TRAN', 'session': 's1'}, {})
    assert response['isError']
    client.assert_not_called()


def test_official_schema_rejects_the_old_discovery_field():
    schema = json.loads((ROOT / 'tests/fixtures/mcp/2026-07-28.schema.json').read_text())
    discovery = mcp._discover()
    discovery['protocolVersions'] = discovery.pop('supportedVersions')
    contract = {**schema, '$ref': '#/$defs/DiscoverResultResponse'}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'jsonrpc': '2.0', 'id': 1, 'result': discovery}, contract)


def test_bridge_prints_do_not_corrupt_rpc_stdout(monkeypatch, capsys):
    def evaluate(args):
        print('bridge setup diagnostic')
        return mcp._text_result('ok')
    monkeypatch.setitem(mcp.IMPL, 'evaluate', evaluate)
    mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'evaluate', 'arguments': {'expr': '1+1'}}})
    captured = capsys.readouterr()
    assert json.loads(captured.out)['id'] == 1
    assert 'bridge setup diagnostic' in captured.err

def test_large_output_does_not_discard_original_receipt():
    result = uncertain_result()
    result.output = 'x' * (mcp.STRUCT_LIMIT + 1)
    response = mcp._bridge_result(result)
    assert response['structuredContent']['metadata'] == result.metadata
    assert response['structuredContent']['output_truncated']
    assert response['isError']


def test_adapter_has_no_followup_feature_dependencies():
    names = {t['name'] for t in mcp._tool_defs()}
    assert not names & {'search', 'search_status', 'update_search', 'signoff', 'log_note'}
    assert {'search_docs', 'read_waveform'} <= names
    source = (ROOT / 'mcp_server.py').read_text(encoding='utf-8')
    assert 'unified_search' not in source
    assert 'parsers_fast' not in source


def test_document_search_uses_existing_remote_aware_bridge(monkeypatch):
    search = Mock(return_value={'results': []})
    monkeypatch.setattr(mcp, '_client', lambda: SimpleNamespace(search_docs=search))
    mcp._impl_doc_search({'query': 'dbOpenCellViewByType', 'limit': 4})
    search.assert_called_once_with('dbOpenCellViewByType', limit=4)
