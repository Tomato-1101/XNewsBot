"""切替と壊れた出力の拒否を、実LLM・LINEなしで検証する。"""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('curate_fallback', Path(__file__).parents[1] / 'scripts/curate_fallback.py')
cf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cf)


@pytest.fixture
def raw():
    return {'genres': {'AI': [
        {'text': '新しい日本語AIモデルを公開', 'summary': '原文の概要です。', 'source': 'news', 'url': 'https://example.com/a'},
        {'text': '新しい日本語AIモデルを公開', 'source': 'news', 'url': 'https://example.com/b'},
        {'text': '個人のAIへの感想', 'url': 'https://example.com/c'},
        {'text': 'English title', 'source': 'news', 'url': 'https://example.com/d'},
    ], '話題': []}}


def test_original_preserves_source_and_omits_duplicates_x_and_english(raw):
    result = cf.original_news(raw)
    assert len(result['genres']['AI']) == 1
    item = result['genres']['AI'][0]
    assert item['title'] == raw['genres']['AI'][0]['text']
    assert item['summary'] == '原文の概要です。'
    assert item['source_idxs'] == [0]
    assert result['genres']['話題'] == []


def test_valid_claude_never_calls_codex(raw, monkeypatch, tmp_path):
    result = cf.original_news(raw)
    output = tmp_path / 'curated.json'
    output.write_text(json.dumps(result))
    monkeypatch.setattr(cf, 'codex_curate', lambda r: pytest.fail('Codex must not run'))
    assert cf.recover(raw, output, tmp_path / 'reason')[1] == 'claude'


def test_claude_failure_uses_codex(raw, monkeypatch, tmp_path):
    result = cf.original_news(raw)
    monkeypatch.setattr(cf, 'codex_curate', lambda r: result)
    output = tmp_path / 'curated.json'
    output.write_text('stale')
    reason = tmp_path / 'reason'
    assert cf.recover(raw, output, reason, True, 'Claude週間上限') == (result, 'codex')
    assert 'Claude週間上限' in reason.read_text()


def test_both_llms_fail_uses_original(raw, monkeypatch, tmp_path):
    def fail(r):
        raise RuntimeError('Codex limit')
    monkeypatch.setattr(cf, 'codex_curate', fail)
    reason = tmp_path / 'reason'
    result, route = cf.recover(raw, tmp_path / 'missing', reason)
    assert route == 'original'
    assert len(result['genres']['AI']) == 1
    assert 'Codex失敗' in reason.read_text()


@pytest.mark.parametrize('mutation', ['missing', 'idx', 'boolidx', 'score', 'empty', 'big'])
def test_rejects_invalid_outputs(raw, mutation):
    result = cf.original_news(raw)
    item = result['genres']['AI'][0]
    if mutation == 'missing': del result['genres']['話題']
    elif mutation == 'idx': item['source_idxs'] = [999]
    elif mutation == 'boolidx': item['source_idxs'] = [True]
    elif mutation == 'score': item['score'] = '30'
    elif mutation == 'empty': result['genres']['AI'] = []
    elif mutation == 'big':
        item['importance'] = 'big'
        result['genres']['AI'] = [copy.deepcopy(item) for _ in range(4)]
    with pytest.raises(ValueError): cf.validate(raw, result)


def test_all_empty_original_fails(raw):
    raw['genres']['AI'] = []
    with pytest.raises(ValueError, match='全ジャンル空'):
        cf.original_news(raw)


def test_codex_invocation_is_isolated_and_validated(raw, monkeypatch):
    monkeypatch.setattr(cf.shutil, 'which', lambda name: '/usr/local/bin/codex')
    def run(command, **kwargs):
        assert command[:2] == ['/usr/local/bin/codex', 'exec']
        assert '--ignore-user-config' in command and '--ignore-rules' in command
        assert command[command.index('--sandbox') + 1] == 'read-only'
        assert 'shell_tool' in command and 'apps' in command and 'multi_agent' in command
        assert command[-1] == '-'
        assert '新しい日本語AIモデルを公開' in kwargs['input']
        Path(command[command.index('-o') + 1]).write_text(json.dumps(cf.original_news(raw)))
        return type('Result', (), {'returncode': 0})()
    monkeypatch.setattr(cf.subprocess, 'run', run)
    assert cf.codex_curate(raw) == cf.original_news(raw)


def test_original_deduplicates_urls_across_genres(raw):
    raw['genres']['話題'] = [dict(raw['genres']['AI'][0], text='同じURLの別見出し')]
    assert cf.original_news(raw)['genres']['話題'] == []


def test_original_ingest_does_not_advance_watch_state(raw, monkeypatch, tmp_path, session):
    import contextlib
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('pipeline_fallback_test', Path(__file__).parents[1] / 'scripts/pipeline.py')
    pl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pl)
    state = tmp_path / 'watch_state.json'
    before = '{"date":"2026-10-20","last_until":"2026-10-20T22:15:00Z"}'
    state.write_text(before)
    monkeypatch.setattr(pl.watch, 'state_path', lambda: str(state))
    raw.update(date='2026-10-22', slot='morning', watch={'since': '2026-10-20T22:15:00Z', 'until': '2026-10-21T22:15:00Z'})
    raw['genres']['監視アカウント'] = [{'text': 'An important release', 'url': 'https://x.com/test/status/1'}]
    curated = cf.original_news(raw)
    assert curated['curation_route'] == 'original'
    assert curated['genres']['監視アカウント'] == []
    raw_path, cur_path = tmp_path / 'raw.json', tmp_path / 'cur.json'
    raw_path.write_text(json.dumps(raw))
    cur_path.write_text(json.dumps(curated))
    @contextlib.contextmanager
    def fake_session():
        yield session
    monkeypatch.setattr(pl, 'get_settings', lambda: SimpleNamespace())
    monkeypatch.setattr(pl, 'init_db', lambda: None)
    monkeypatch.setattr(pl, 'get_session', fake_session)
    pl.cmd_ingest(SimpleNamespace(raw=str(raw_path), curated=str(cur_path), date=None, slot=None))
    assert state.read_text() == before
