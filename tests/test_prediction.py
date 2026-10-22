"""Offline regression checks for frozen inputs, model bytes and execution contracts.

Fixtures are synthetic; these tests never read workstation models or download
weights. Numerical model materialization is exercised with tiny safe tensors.
"""
from __future__ import annotations


import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import torch
from safetensors.torch import save_file

from kcpredict import model_assets as assets
from kcpredict import prediction


def bars(n=30):
    close = np.linspace(20, 25, n)
    frame = pd.DataFrame({"date": pd.bdate_range("2026-01-01", periods=n),
                          "open": close - .2, "high": close + .8, "low": close - .8,
                          "close": close, "volume": np.linspace(1e5, 2e5, n),
                          "amount": close * 1e5, "has_trade_price": 1, "symbol": "000001.SZ",
                          "source": "sina_unadjusted_v1"})
    frame.attrs.update(price_basis="unadjusted", volume_unit="shares", amount_unit="CNY")
    return frame


def test_input_truncation_precedes_normalization_and_identity():
    frame = bars()
    end = frame.date.iloc[20].date().isoformat()
    future = frame.date.iloc[21:26].tolist()
    first = prediction.prepare_input(frame, future, symbol="000001.SZ", as_of=end, lookback=16)
    frame.loc[21:, "close"] = 99999
    second = prediction.prepare_input(frame, future, symbol="000001.SZ", as_of=end, lookback=16)
    assert first[2] == second[2]
    assert first[0].close.tolist() == second[0].close.tolist()


@pytest.mark.parametrize("mutation,error", [
    (lambda f: f.drop(columns="amount"), "Actual OHLC"),
    (lambda f: f.assign(high=f.low - 1), "geometry"),
    (lambda f: f.assign(volume=np.nan), "finite"),
])
def test_invalid_inputs_fail_without_model_load(mutation, error):
    frame = bars()
    with pytest.raises(ValueError, match=error):
        prediction.prepare_input(mutation(frame), pd.bdate_range("2026-03-01", periods=5),
                                  symbol="000001.SZ", as_of=str(frame.date.iloc[-1].date()), lookback=16)


def test_no_stale_date_or_intraday_close_substitution():
    frame = bars()
    future = pd.bdate_range("2026-03-01", periods=5)
    with pytest.raises(ValueError, match="No actual"):
        prediction.prepare_input(frame, future, symbol="000001.SZ", as_of="2026-02-28", lookback=16)
    with pytest.raises(ValueError, match="after 15"):
        prediction.prepare_input(frame, future, symbol="000001.SZ", as_of="2026-02-11T10:00:00+08:00", lookback=16)


def test_paths_are_filtered_as_whole_without_direction_bias_or_repair():
    data = np.tile([10., 12., 8., 11., 100., 1000.], (10, 5, 1))
    data[1, 2, 1] = 2  # invalid high; the whole path must be excluded
    data[3, 3, 4] = -1
    original = data.copy()
    result = prediction.summarize_samples(data, list("abcde"), 10)
    assert result["quality"]["valid_samples"] == 8
    assert result["quality"]["prediction_usable"]
    assert result["summary"]["up_path_fraction"] == 1
    np.testing.assert_array_equal(data, original)
    data[4, 0, 3] = np.nan
    assert not prediction.summarize_samples(data, list("abcde"), 10)["quality"]["prediction_usable"]
    assert not prediction.summarize_samples(data[:7], list("abcde"), 10)["quality"]["prediction_usable"]


def test_distribution_uses_individual_paths_not_the_mean_curve():
    data = np.tile([10., 12., 8., 11., 100., 1000.], (8, 5, 1))
    data[:4, :, 3] = 9
    result = prediction.summarize_samples(data, list("abcde"), 10)
    assert result["summary"]["up_path_fraction"] == .5
    assert result["forecast"][-1]["close_p10"] == 9
    assert result["forecast"][-1]["close_p90"] == 11


def test_asset_hash_rechecked_when_bytes_change_and_no_implicit_download(tmp_path, monkeypatch):
    raw = b'{"value": 1}'
    (tmp_path / "config.json").write_bytes(raw)
    monkeypatch.setattr(assets, "PINNED_ASSETS", ({"path": "config.json", "size": len(raw),
                       "sha256": hashlib.sha256(raw).hexdigest(), "repo": "test", "revision": "frozen"},))
    assert assets.asset_status(tmp_path)["ready"]
    (tmp_path / "config.json").write_bytes(b'{"value": 2}')
    assert not assets.asset_status(tmp_path)["ready"]
    with pytest.raises(ValueError, match="SHA"):
        assets.prepare_assets(tmp_path)


def test_invalid_asset_rejects_prediction_before_reusing_an_already_loaded_model(tmp_path, monkeypatch):
    raw = b'{"value": 1}'
    path = tmp_path / 'config.json'
    path.write_bytes(raw)
    monkeypatch.setattr(assets, 'PINNED_ASSETS', ({'path': 'config.json', 'size': len(raw),
        'sha256': hashlib.sha256(raw).hexdigest(), 'repo': 'test', 'revision': 'frozen'},))
    assert assets.asset_status(tmp_path)['ready']
    monkeypatch.setattr(assets, '_stat_identity', lambda _: ('same file metadata',))
    path.write_bytes(b'{"value": 2}')
    monkeypatch.setattr(prediction, '_SINGLETON', {'key': 'previously-loaded', 'model': object()})
    monkeypatch.setattr(prediction, '_load_model', lambda *_: pytest.fail('Invalid asset reused the loaded predictor'))
    frame = bars()
    future = pd.bdate_range(frame.date.iloc[-1] + pd.Timedelta(days=1), periods=5)
    with pytest.raises(FileNotFoundError, match='Kronos local assets unavailable'):
        prediction.predict(frame, future, symbol='000001.SZ', as_of=str(frame.date.iloc[-1].date()),
                            lookback=16, device='cpu', asset_path=tmp_path)


def test_predict_preserves_samples_and_has_no_production_qualification(monkeypatch, tmp_path):
    frame = bars()
    as_of = str(frame.date.iloc[-1].date())
    future = pd.bdate_range(frame.date.iloc[-1] + pd.Timedelta(days=1), periods=5)
    def inference(tokenizer, model, x, *_args, sample_count, **kwargs):
        return np.repeat(x.cpu().numpy()[:, None, :, :], sample_count, axis=1)
    monkeypatch.setattr(prediction, "asset_status", lambda _: {"ready": True, "files": []})
    monkeypatch.setattr(prediction, "_source_identity", lambda: {"vendor": "fixture"})
    monkeypatch.setattr(prediction, "_load_model", lambda *_: (None, None, inference, True))
    result = prediction.predict(frame, future, symbol="000001.SZ", as_of=as_of, lookback=16,
                                 device="cpu", asset_path=tmp_path, calendar_verified=True,
                                 calendar_identity="test_calendar")
    assert np.asarray(result["samples"]).shape == (8, 5, 6)
    assert result["quality"]["prediction_usable"]
    assert not result["qualification"]["positive_allocation_eligible"]
    json.dumps(result, allow_nan=False)
    assert list(tmp_path.iterdir()) == []
    no_calendar = prediction.predict(frame, future, symbol="000001.SZ", as_of=as_of,
                                      lookback=16, device="cpu", asset_path=tmp_path)
    assert not no_calendar["quality"]["prediction_usable"]
    assert no_calendar["cache_key"] != result["cache_key"]


def test_cancel_prevents_model_loading(monkeypatch):
    frame = bars()
    monkeypatch.setattr(prediction, "asset_status", lambda _: {"ready": True, "files": []})
    monkeypatch.setattr(prediction, "_source_identity", lambda: {})
    monkeypatch.setattr(prediction, "_load_model", lambda *_: pytest.fail("cancelled work loaded a model"))
    def cancelled():
        raise InterruptedError("cancelled")
    with pytest.raises(InterruptedError):
        prediction.predict(frame, pd.bdate_range("2026-03-01", periods=5), symbol="000001.SZ",
                            as_of=str(frame.date.iloc[-1].date()), lookback=16, device="cpu", cancel_check=cancelled)
    with pytest.raises(InterruptedError):
        prediction.predict(frame, pd.bdate_range("2026-03-01", periods=5), symbol="000001.SZ",
                            as_of=str(frame.date.iloc[-1].date()), lookback=16, device="cpu", cancel_check=lambda: True)


@pytest.mark.parametrize("mutation", [
    lambda frame: frame.assign(has_trade_price=0),
    lambda frame: frame.assign(source="unverified_feed"),
    lambda frame: frame.drop(columns="has_trade_price"),
])
def test_valid_generated_paths_cannot_certify_unverified_actual_prices(monkeypatch, tmp_path, mutation):
    original = bars()
    frame = mutation(original)
    frame.attrs.update(original.attrs)
    def inference(_tokenizer, _model, x, *_args, sample_count, **_kwargs):
        return np.repeat(x.cpu().numpy()[:, None, :, :], sample_count, axis=1)
    monkeypatch.setattr(prediction, "asset_status", lambda _: {"ready": True, "files": []})
    monkeypatch.setattr(prediction, "_source_identity", lambda: {"vendor": "fixture"})
    monkeypatch.setattr(prediction, "_load_model", lambda *_: (None, None, inference, True))
    result = prediction.predict(frame, pd.bdate_range("2026-03-01", periods=5),
                                 symbol="000001.SZ", as_of=str(frame.date.iloc[-1].date()),
                                 lookback=16, device="cpu", asset_path=tmp_path,
                                 calendar_verified=True, calendar_identity="test-calendar")
    assert result["quality"]["valid_samples"] == 8
    assert not result["quality"]["input_contract_verified"]
    assert not result["quality"]["prediction_usable"]


# Pinned bytes remain authoritative when filesystem timestamps have low precision.


@pytest.fixture(autouse=True)
def isolated_verification_cache(monkeypatch):
    monkeypatch.setattr(assets, '_VERIFIED', {})
    # Exercise the weight strategy with tiny fixtures, never workstation models.
    monkeypatch.setattr(assets, '_MEMO_MIN_BYTES', 1)
    monkeypatch.setattr(assets.urllib.request, 'urlopen',
                        lambda *args, **kwargs: pytest.fail('Verification attempted a download'))


def pin(tmp_path, raw=b'original pinned weight', name='model.safetensors', **extra):
    path = tmp_path / name
    path.write_bytes(raw)
    return path, {'path': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                  'repo': 'synthetic', 'revision': 'frozen', **extra}


@pytest.mark.parametrize('identity_kind', ['sha256', 'git_blob'])
def test_config_always_checks_bytes_when_all_observed_metadata_is_unchanged(tmp_path, monkeypatch, identity_kind):
    raw = b'{"value": 1}'
    path, entry = pin(tmp_path, raw, 'config.json')
    if identity_kind == 'git_blob':
        entry.pop('sha256')
        entry['git_blob'] = hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()
    monkeypatch.setattr(assets, 'PINNED_ASSETS', (entry,))
    monkeypatch.setattr(assets, '_stat_identity', lambda _: ('unchanged metadata',))
    monkeypatch.setattr(assets, '_memo_version', lambda _: ('unchanged version',))
    assert assets.asset_status(tmp_path)['ready']
    assert not assets._VERIFIED
    path.write_bytes(b'{"value": 2}')
    rejected = assets.asset_status(tmp_path)
    assert not rejected['ready'] and rejected['errors']
    with pytest.raises(ValueError, match='SHA|configuration identity'):
        assets.prepare_assets(tmp_path)


def test_windows_weight_rehashes_even_with_identical_file_metadata(tmp_path, monkeypatch):
    raw = b'original pinned weight'
    path, entry = pin(tmp_path, raw)
    monkeypatch.setattr(assets, 'PINNED_ASSETS', (entry,))
    monkeypatch.setattr(assets, '_WINDOWS', True)
    monkeypatch.setattr(assets, '_stat_identity', lambda _: ('same Windows stat',))
    observed = []
    original = assets.hashlib.file_digest

    def digest(handle, algorithm):
        observed.append(handle.name)
        return original(handle, algorithm)

    monkeypatch.setattr(assets.hashlib, 'file_digest', digest)
    assert assets.asset_status(tmp_path)['ready']
    assert assets.asset_status(tmp_path)['ready']
    assert len(observed) == 2 and not assets._VERIFIED
    path.write_bytes(b'changed! pinned weight')
    assert path.stat().st_size == len(raw)
    result = assets.asset_status(tmp_path)
    assert not result['ready'] and 'SHA mismatch' in result['errors'][0]
    assert len(observed) == 3
    with pytest.raises(ValueError, match='SHA mismatch'):
        assets.prepare_assets(tmp_path)


def test_posix_weight_memo_is_bounded_and_binds_the_whole_pin(tmp_path, monkeypatch):
    path, entry = pin(tmp_path)
    monkeypatch.setattr(assets, '_WINDOWS', False)
    if assets.os.name == 'nt':
        # Windows stat/fstat can disagree on creation time. This unit test
        # exercises POSIX memoization with stable file metadata.
        stat_identity = assets._stat_identity
        monkeypatch.setattr(assets, '_stat_identity', lambda info: stat_identity(info)[:-1])
    observed = []
    original = assets.hashlib.file_digest

    def digest(handle, algorithm):
        observed.append(handle.name)
        return original(handle, algorithm)

    monkeypatch.setattr(assets.hashlib, 'file_digest', digest)
    first = assets._verify(path, entry)
    assert assets._verify(path, entry) == first and len(observed) == 1
    assert assets._verify(path, {**entry, 'revision': 'new-pin'})['revision'] == 'new-pin'
    assert len(observed) == 2 and len(assets._VERIFIED) == 1
    with pytest.raises(ValueError, match='SHA mismatch'):
        assets._verify(path, {**entry, 'sha256': '0' * 64})
    assert len(observed) == 3
    for index in range(assets._MEMO_LIMIT + 2):
        directory = tmp_path / str(index)
        directory.mkdir()
        item, identity = pin(directory)
        assets._verify(item, identity)
    assert len(assets._VERIFIED) == assets._MEMO_LIMIT


def test_same_size_path_replacement_during_hash_cannot_publish_verification(tmp_path, monkeypatch):
    path, entry = pin(tmp_path)
    monkeypatch.setattr(assets, 'PINNED_ASSETS', (entry,))
    monkeypatch.setattr(assets, '_WINDOWS', True)
    original = assets.hashlib.file_digest

    def digest(handle, algorithm):
        result = original(handle, algorithm)
        replacement = tmp_path / 'replacement'
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        return result

    monkeypatch.setattr(assets.hashlib, 'file_digest', digest)
    result = assets.asset_status(tmp_path)
    # If Windows denies replacement of the open handle, that OSError also fails
    # closed. A successful replacement must be rejected by the path/file ID.
    assert not result['ready'] and result['errors'] and not assets._VERIFIED


def test_config_digests_share_one_observed_byte_sequence(tmp_path, monkeypatch):
    raw = b'{"value": 1}'
    path, entry = pin(tmp_path, raw, 'config.json')
    entry['git_blob'] = hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()
    original = Path.read_bytes

    def changed_read(current):
        if current == path:
            return b'{"value": 2}'
        return original(current)

    monkeypatch.setattr(Path, 'read_bytes', changed_read)
    with pytest.raises(ValueError, match='changed during verification'):
        assets._verify(path, entry)


# Strict pinned materialization and factual execution evidence, without model assets.


@pytest.fixture
def tiny_assets(tmp_path, monkeypatch):
    vendor = tmp_path / 'resources' / 'vendor' / 'kronos'
    vendor.mkdir(parents=True)
    (vendor / '__init__.py').write_text('''
import torch
from torch import nn
class Kronos(nn.Module):
    def __init__(self, extra_meta_buffer=False):
        super().__init__()
        self.projection = nn.Linear(4, 3)
        self.initialization_device = str(self.projection.weight.device)
        self.register_buffer('basis', torch.arange(3))
        if extra_meta_buffer:
            self.register_buffer('unprovided', torch.ones(2), persistent=False)
class KronosTokenizer(nn.Module):
    def __init__(self):
        super().__init__()
        self.projection = nn.Linear(3, 2)
        self.register_buffer('group_codebook', torch.arange(12).reshape(3,4), persistent=False)
def auto_regressive_inference(*args, **kwargs):
    raise AssertionError('Load test must never run inference')
''')
    assets = tmp_path / 'assets'
    (assets / 'tokenizer').mkdir(parents=True)
    (assets / 'config.json').write_text('{}')
    (assets / 'tokenizer/config.json').write_text('{}')
    main = {'projection.weight': torch.arange(12, dtype=torch.float32).reshape(3, 4),
            'projection.bias': torch.tensor([.2, .4, .8]), 'basis': torch.tensor([7, 8, 9])}
    tokenizer = {'projection.weight': torch.arange(6, dtype=torch.float32).reshape(2, 3),
                 'projection.bias': torch.tensor([.1, .9])}
    save_file(main, str(assets / 'model.safetensors'))
    save_file(tokenizer, str(assets / 'tokenizer/model.safetensors'))
    monkeypatch.setattr(prediction, '__file__', str(tmp_path / 'resources' / 'prediction.py'))
    monkeypatch.setattr(prediction, '_SINGLETON', {})
    # Avoid touching any accelerator allocator from a CPU-only fixture.
    monkeypatch.setattr(prediction, 'release_prediction_resources', prediction._SINGLETON.clear)
    return assets, main, tokenizer


def test_meta_model_strictly_assigns_every_weight_and_keeps_tokenizer_codebook(tiny_assets):
    assets, expected, token_expected = tiny_assets
    rng = torch.get_rng_state().clone()
    model, tokenizer, inference, reused = prediction._load_model(assets, 'cpu', {'assets': [], 'source': {}})
    assert not reused and model.initialization_device == 'meta'
    assert torch.equal(rng, torch.get_rng_state())
    for key, value in model.state_dict().items():
        assert torch.equal(value, expected[key])
    for key, value in tokenizer.state_dict().items():
        assert torch.equal(value, token_expected[key])
    assert torch.equal(tokenizer.group_codebook, torch.arange(12).reshape(3, 4))
    assert not any(value.is_meta for value in (*model.parameters(), *model.buffers(),
                                               *tokenizer.parameters(), *tokenizer.buffers()))
    assert all(not value.requires_grad for value in (*model.parameters(), *tokenizer.parameters()))
    assert not model.training and not tokenizer.training
    second = prediction._load_model(assets, 'cpu', {'assets': [], 'source': {}})
    assert second == (model, tokenizer, inference, True)


def test_missing_pinned_parameter_fails_strict_loading(tiny_assets):
    assets, expected, _ = tiny_assets
    del expected['projection.bias']
    save_file(expected, str(assets / 'model.safetensors'))
    with pytest.raises(RuntimeError, match='Missing key'):
        prediction._load_model(assets, 'cpu', {'assets': [], 'source': {}})
    assert not prediction._SINGLETON


def test_extra_unmaterialized_meta_buffer_cannot_reach_inference(tiny_assets):
    assets, _, _ = tiny_assets
    (assets / 'config.json').write_text(json.dumps({'extra_meta_buffer': True}))
    with pytest.raises(ValueError, match='materialize every'):
        prediction._load_model(assets, 'cpu', {'assets': [], 'source': {}})
    assert not prediction._SINGLETON


def test_execution_proof_checks_real_parameter_and_input_devices():
    model = torch.nn.Linear(2, 2)
    tokenizer = torch.nn.Linear(2, 2)
    inputs = (torch.ones(1, 3, 2), torch.ones(1, 3, 5), torch.ones(1, 5, 5))
    proof = prediction._execution_devices(model, tokenizer, inputs, 'cpu')
    assert proof == {'model_parameter_devices': ['cpu'], 'tokenizer_parameter_devices': ['cpu'],
                     'input_tensor_devices': ['cpu'], 'tensor_devices_verified': True}
    with pytest.raises(RuntimeError, match='differ'):
        prediction._execution_devices(model, tokenizer, inputs, 'mps')
    empty = prediction._execution_devices(None, None, inputs, 'cpu')
    assert not empty['tensor_devices_verified']


def test_seeded_cpu_state_restored_on_failure():
    before = torch.get_rng_state().clone()
    with pytest.raises(InterruptedError):
        with prediction._seeded(42, 'cpu'):
            torch.rand(10)
            raise InterruptedError('isolated cancellation')
    assert torch.equal(before, torch.get_rng_state())


@pytest.mark.parametrize('requested', ['mps', 'cuda', 'cuda:0'])
def test_unavailable_explicit_accelerator_never_falls_back(monkeypatch, requested):
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
    monkeypatch.setattr(torch.backends.mps, 'is_available', lambda: False)
    with pytest.raises(RuntimeError, match='unavailable'):
        prediction._device(requested)


def test_bare_cuda_resolves_actual_ordinal_without_gpu_access(monkeypatch):
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: True)
    monkeypatch.setattr(torch.cuda, 'current_device', lambda: 2)
    assert prediction._device('cuda') == 'cuda:2'
    assert prediction._device('cuda:1') == 'cuda:1'


def tiny_pin_directory(root, monkeypatch):
    """A complete portable layout with synthetic, tiny pinned bytes."""
    entries = []
    for name, raw in {
        'model.safetensors': b'pinned main model fixture',
        'config.json': b'{"fixture": 1}',
        'tokenizer/model.safetensors': b'pinned tokenizer fixture',
        'tokenizer/config.json': b'{"fixture": 2}',
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        entry = {'path': name, 'remote': Path(name).name, 'size': len(raw),
                 'sha256': hashlib.sha256(raw).hexdigest(),
                 'repo': 'synthetic/tokenizer' if name.startswith('tokenizer/') else 'synthetic/model',
                 'revision': 'frozen-revision'}
        entries.append(entry)
    monkeypatch.setattr(assets, 'PINNED_ASSETS', tuple(entries))
    return entries


def test_asset_home_is_independent_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('KCPREDICT_HOME', str(tmp_path / 'user-state'))
    monkeypatch.chdir(tmp_path)
    assert assets.asset_root() == tmp_path / 'user-state/models/kronos'
    assert assets.asset_root(tmp_path / 'explicit') == tmp_path / 'explicit'


def test_missing_assets_do_not_create_directories_or_fetch(tmp_path):
    destination = tmp_path / 'missing'
    assert not assets.asset_status(destination)['ready']
    with pytest.raises(FileNotFoundError, match='explicitly download'):
        assets.prepare_assets(destination)
    assert not destination.exists()


def test_local_asset_import_is_verified_and_independent_copy(tmp_path, monkeypatch):
    source, destination = tmp_path / 'source', tmp_path / 'portable'
    entries = tiny_pin_directory(source, monkeypatch)
    result = assets.prepare_assets(destination, source=source)
    assert result['ready'] and result['downloaded'] == []
    assert set(result['imported']) == {entry['path'] for entry in entries}
    (source / 'config.json').write_bytes(b'{"fixture": 9}')
    assert assets.asset_status(destination)['ready']
    assert not list(destination.rglob('*.prepare'))
    assert assets.prepare_assets(destination)['imported'] == []


def test_corrupt_local_source_is_rejected_before_destination_creation(tmp_path, monkeypatch):
    source, destination = tmp_path / 'source', tmp_path / 'portable'
    tiny_pin_directory(source, monkeypatch)
    (source / 'tokenizer/config.json').write_bytes(b'{"fixture": 9}')
    with pytest.raises(ValueError, match='SHA'):
        assets.prepare_assets(destination, source=source)
    assert not destination.exists()


def test_asset_import_never_overwrites_invalid_existing_bytes(tmp_path, monkeypatch):
    source, destination = tmp_path / 'source', tmp_path / 'portable'
    tiny_pin_directory(source, monkeypatch)
    destination.mkdir()
    path = destination / 'config.json'
    original = b'{"fixture": 7}'
    path.write_bytes(original)
    with pytest.raises(ValueError, match='SHA'):
        assets.prepare_assets(destination, source=source)
    assert path.read_bytes() == original
    assert sorted(item.name for item in destination.iterdir()) == ['config.json']


def test_download_requires_explicit_flag_and_uses_frozen_urls(tmp_path, monkeypatch):
    import io
    source, destination = tmp_path / 'source', tmp_path / 'portable'
    entries = tiny_pin_directory(source, monkeypatch)
    observed = []
    def fetch(url, timeout):
        observed.append(url)
        entry = next(entry for entry in entries if url == assets._asset_url(entry))
        assert timeout == 60
        return io.BytesIO((source / entry['path']).read_bytes())
    monkeypatch.setattr(assets.urllib.request, 'urlopen', fetch)
    with pytest.raises(FileNotFoundError):
        assets.prepare_assets(destination)
    assert not observed
    result = assets.prepare_assets(destination, download=True)
    assert result['ready'] and len(result['downloaded']) == 4
    assert observed == [assets._asset_url(entry) for entry in entries]
    assert all('/resolve/frozen-revision/' in url for url in observed)
    assert not list(destination.rglob('*.prepare'))


def test_manifest_is_fixed_and_offline():
    manifest = assets.asset_manifest()
    assert manifest['total_size'] == 425106905
    assert all('/resolve/' in entry['url'] for entry in manifest['files'])
    assert len(manifest['files']) == 4


def test_vendored_algorithm_files_verify_and_have_no_project_imports():
    identity = prediction._source_identity()
    assert len(identity['vendor_sha256']) == len(identity['adapter_sha256']) == 64
    root = Path(prediction.__file__).parent
    for path in (root / 'prediction.py', root / 'model_assets.py'):
        code = path.read_text(encoding="utf-8")
        assert 'from my_strategy' not in code and 'import my_strategy' not in code
        assert 'KHQUANT_' not in code
