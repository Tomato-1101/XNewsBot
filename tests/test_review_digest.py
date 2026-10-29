"""配信前の Codex レビューの適用・安全装置を、偽の codex（XNEWS_REVIEW_CMD）だけで検証する。実 Codex・LINE は呼ばない。"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts/review_digest.py'
SPEC = importlib.util.spec_from_file_location('review_digest', SCRIPT)
rd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rd)

# 偽の codex: 受け取った引数と標準入力を記録し、FAKE_ANSWER のファイル内容を -o の先へ書く。
# FAKE_RC があればその終了コードで落ち、FAKE_SLEEP があれば眠る。
FAKE = r'''
import json, os, sys, time
args = sys.argv[1:]
Path = __import__("pathlib").Path
log = Path(os.environ["FAKE_LOG"])
log.write_text(json.dumps({"args": args, "stdin": sys.stdin.read()}, ensure_ascii=False))
if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))
if os.environ.get("FAKE_RC"):
    sys.exit(int(os.environ["FAKE_RC"]))
out = args[args.index("-o") + 1]
Path(out).write_text(Path(os.environ["FAKE_ANSWER"]).read_text())
'''


def item(title, imp='small', score=50, genres=None, idxs=(0,), detail=''):
    return {'title': title, 'summary': f'{title}の要約。', 'detail': detail if imp == 'big' else '',
            'importance': imp, 'score': score, 'genres': genres, 'source_idxs': list(idxs)}


@pytest.fixture
def raw():
    cand = [{'text': f'候補{i}', 'source': 'news', 'url': f'https://example.com/{i}'} for i in range(5)]
    return {'date': '2026-10-29', 'slot': 'morning',
            'genres': {'AI': list(cand), 'テクノロジー': list(cand), '話題': list(cand)},
            'recent_titles': {'AI': ['昨日の見出し']},
            'preferences': {'good': [{'genre': 'AI', 'title': '好きな記事'}], 'bad': []}}


@pytest.fixture
def curated():
    return {'curation_route': 'claude', 'genres': {
        'AI': [item('OpenAIが資金調達', 'big', 90, ['AI'], detail='詳細。'),
               item('AIの小ネタ', 'small', 40, ['AI'], (1,)),
               item('AIの続報', 'small', 45, ['AI'], (2,))],
        'テクノロジー': [item('OpenAI資金調達の別記事', 'small', 60, ['テクノロジー']),
                    item('新型スマホ発表', 'small', 55, ['テクノロジー'], (1,))],
        '話題': [item('大きな話題', 'small', 70, ['話題'])],
    }}


def run_cli(tmp_path, raw, curated, answer=None, args=(), **env_extra):
    raw_p, cur_p, rep_p = tmp_path / 'raw.json', tmp_path / 'cur.json', tmp_path / 'rep' / 'r.json'
    raw_p.write_text(json.dumps(raw, ensure_ascii=False))
    cur_p.write_text(json.dumps(curated, ensure_ascii=False))
    fake = tmp_path / 'fake_codex.py'
    fake.write_text(FAKE)
    ans = tmp_path / 'answer.txt'
    ans.write_text(answer if isinstance(answer, str) else json.dumps(answer or {'drop': [], 'merge': []},
                                                                    ensure_ascii=False))
    env = {'PATH': '/usr/bin:/bin', 'XNEWS_REVIEW_CMD': f'{sys.executable} {fake}',
           'FAKE_LOG': str(tmp_path / 'fake.log'), 'FAKE_ANSWER': str(ans), **env_extra}
    run = subprocess.run([sys.executable, str(SCRIPT), '--raw', str(raw_p), '--curated', str(cur_p),
                          '--report', str(rep_p), *args], env=env, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return (run.stdout.strip(), json.loads(cur_p.read_text()), json.loads(rep_p.read_text()),
            tmp_path / 'fake.log')


def titles(cur, g):
    return [it['title'] for it in cur['genres'][g]]


def test_drop_and_cross_genre_merge(tmp_path, raw, curated):
    answer = {'drop': [{'id': 'AI/1', 'reason': '中身が無い'}],
              'merge': [{'into': 'AI/0', 'from': ['テクノロジー/0', 'AI/2'], 'summary': '統合後の要約。',
                         'detail': '統合後の詳細。解説: 用語。'}]}
    line, cur, rep, log = run_cli(tmp_path, raw, curated, answer)
    assert line == 'Codex レビュー: 6→3件（除外1・統合2）'
    assert titles(cur, 'AI') == ['OpenAIが資金調達']
    assert titles(cur, 'テクノロジー') == ['新型スマホ発表']
    parent = cur['genres']['AI'][0]
    assert parent['genres'] == ['AI', 'テクノロジー']
    assert parent['summary'] == '統合後の要約。' and parent['detail'] == '統合後の詳細。解説: 用語。'
    # 同じジャンル内の統合だけ根拠を引き継ぐ（別ジャンルの番号は持ち込まない）
    assert parent['source_idxs'] == [0, 2]
    assert cur['curation_route'] == 'claude'
    assert rep['status'] == 'applied' and rep['before'] == 6 and rep['after'] == 3
    assert rep['dropped'] == [{'genre': 'AI', 'title': 'AIの小ネタ', 'reason': '中身が無い'}]
    assert rep['merged'][0]['into'] == {'genre': 'AI', 'title': 'OpenAIが資金調達'}
    # 呼び出しの作法と、Codex へ渡す入力
    called = json.loads(log.read_text())
    args = called['args']
    for flag in ('-m', 'gpt-6.1-sol', '-s', 'read-only', '--skip-git-repo-check', '--output-schema', '-o'):
        assert flag in args
    assert args[-1] == '-'
    assert '"id": "テクノロジー/0"' in called['stdin'] and '昨日の見出し' in called['stdin']
    assert '好きな記事' in called['stdin'] and '## ジャンルの範囲' in called['stdin']


def test_invalid_ops_are_ignored_individually(tmp_path, raw, curated):
    answer = {'drop': [{'id': 'AI/99', 'reason': 'x'}, {'id': '無いジャンル/0', 'reason': 'x'},
                       {'id': '話題/0', 'reason': '重複して出る'}, {'id': 'テクノロジー/1', 'reason': '有効'}],
              'merge': [{'into': 'AI/0', 'from': ['AI/0'], 'summary': '', 'detail': ''},
                        {'into': '話題/0', 'from': ['AI/2'], 'summary': '', 'detail': ''},
                        {'into': 'AI/1', 'from': [], 'summary': '', 'detail': ''}]}
    line, cur, rep, _ = run_cli(tmp_path, raw, curated, answer)
    assert line.startswith('Codex レビュー: 6→5件（除外1・統合0）')
    assert titles(cur, 'テクノロジー') == ['OpenAI資金調達の別記事']
    assert titles(cur, '話題') == ['大きな話題']
    assert len(titles(cur, 'AI')) == 3
    reasons = [i['reason'] for i in rep['ignored']]
    assert reasons.count('存在しない id') == 2
    assert sum('複数の操作' in r for r in reasons) == 3   # into=from と 話題/0 の drop・merge
    assert 'from が空' in reasons


def test_reject_over_60_percent(tmp_path, raw, curated):
    answer = {'drop': [{'id': i, 'reason': 'x'} for i in ('AI/1', 'AI/2', 'テクノロジー/0', 'テクノロジー/1')],
              'merge': []}
    line, cur, rep, _ = run_cli(tmp_path, raw, curated, answer)
    assert line.startswith('Codex レビュー失敗のため未レビューで配信')
    assert cur == curated
    assert rep['status'] == 'rejected'


def test_codex_failure_keeps_curated(tmp_path, raw, curated):
    line, cur, rep, _ = run_cli(tmp_path, raw, curated, FAKE_RC='3')
    assert line == 'Codex レビュー失敗のため未レビューで配信: Codex CLI終了コード3'
    assert cur == curated and rep['status'] == 'failed'


def test_broken_json_keeps_curated(tmp_path, raw, curated):
    line, cur, rep, _ = run_cli(tmp_path, raw, curated, '了解しました。JSONは出せません')
    assert line.startswith('Codex レビュー失敗のため未レビューで配信')
    assert cur == curated


def test_json_wrapped_in_text_is_extracted(tmp_path, raw, curated):
    text = '結果です\n```json\n{"drop":[{"id":"AI/1","reason":"x"}],"merge":[]}\n```'
    line, cur, _, _ = run_cli(tmp_path, raw, curated, text)
    assert line == 'Codex レビュー: 6→5件（除外1・統合0）'


def test_missing_codex_command_keeps_curated(tmp_path, raw, curated):
    line, cur, _, _ = run_cli(tmp_path, raw, curated, XNEWS_REVIEW_CMD='/nonexistent/codex')
    assert line.startswith('Codex レビュー失敗のため未レビューで配信')
    assert cur == curated


def test_timeout_keeps_curated(tmp_path, raw, curated, monkeypatch):
    fake = tmp_path / 'fake_codex.py'
    fake.write_text(FAKE)
    monkeypatch.setenv('XNEWS_REVIEW_CMD', f'{sys.executable} {fake}')
    monkeypatch.setenv('FAKE_LOG', str(tmp_path / 'fake.log'))
    monkeypatch.setenv('FAKE_SLEEP', '5')
    monkeypatch.setenv('FAKE_ANSWER', str(tmp_path / 'none.txt'))
    result, rep, line = rd.review(raw, curated, timeout=1)
    assert result is None and rep['status'] == 'failed' and 'Codex が1秒で終わらずタイムアウト' in line


def test_cli_timeout_argument(tmp_path, raw, curated):
    """定刻配信では deliver.sh が --timeout で定刻までの残りを渡す。"""
    line, cur, rep, _ = run_cli(tmp_path, raw, curated, args=('--timeout', '1'), FAKE_SLEEP='5')
    assert 'Codex が1秒で終わらずタイムアウト' in line
    assert cur == curated and rep['status'] == 'failed'


def test_big_limit_and_order(raw, curated):
    curated['genres']['AI'] = [item(f'big{i}', 'big', s, ['AI'], detail='詳細。')
                               for i, s in enumerate((80, 95, 60, 70))] + [item('small高', 'small', 99, ['AI'])]
    result, _, _ = rd.apply(curated, [], [], [])
    ai = result['genres']['AI']
    assert [it['title'] for it in ai] == ['big1', 'big0', 'big3', 'small高', 'big2']
    assert [it['importance'] for it in ai] == ['big', 'big', 'big', 'small', 'small']
    assert ai[-1]['detail'] == ''


@pytest.mark.parametrize('summary, detail, reason', [
    ('あ' * 101, '', 'small の要約が100字超'),
    ('統合後の要約。', '小さい記事の詳細', 'small に detail を書いた（詳細は big だけ）'),
])
def test_merge_failing_rules_is_ignored_whole(tmp_path, raw, curated, summary, detail, reason):
    """統合後の要約・詳細が決まりに合わない merge は丸ごと無視し、両方の記事を残す
    (from だけ消して、吸収した記事の事実が親に入らないまま落ちるのを防ぐ)。"""
    answer = {'drop': [], 'merge': [{'into': 'テクノロジー/0', 'from': ['AI/2'], 'summary': summary,
                                     'detail': detail}]}
    line, cur, rep, _ = run_cli(tmp_path, raw, curated, answer)
    assert line.startswith('Codex レビュー: 6→6件（除外0・統合0）')
    parent = cur['genres']['テクノロジー'][0]
    assert parent['summary'] == 'OpenAI資金調達の別記事の要約。' and parent['detail'] == ''
    assert parent['genres'] == ['テクノロジー']
    assert 'AIの続報' in titles(cur, 'AI')
    assert [(i['op'], i['reason']) for i in rep['ignored']] == [('merge', reason)]


def test_big_drop_is_rejected_but_big_merge_is_allowed(tmp_path, raw, curated):
    """big のただの除外は機械的に拒否して ignored に理由を残す。big を他の記事へ統合するのは認める。"""
    answer = {'drop': [{'id': 'AI/0', 'reason': '重複'}], 'merge': []}
    line, cur, rep, _ = run_cli(tmp_path, raw, curated, answer)
    assert sorted(titles(cur, 'AI')) == sorted(titles(curated, 'AI'))  # 並びは score 順に整えられる
    assert rep['ignored'] == [{'op': 'drop', 'detail': {'id': 'AI/0', 'reason': '重複'},
                               'reason': 'big の記事は除外しない（重複なら統合する）'}]

    curated['genres']['テクノロジー'][0] = item('別のbig', 'big', 80, ['テクノロジー'], detail='詳細。')
    answer = {'drop': [], 'merge': [{'into': 'テクノロジー/0', 'from': ['AI/0'], 'summary': '統合後の要約。',
                                     'detail': ''}]}
    line, cur, rep, _ = run_cli(tmp_path, raw, curated, answer)
    assert line == 'Codex レビュー: 6→5件（除外0・統合1）'
    assert 'OpenAIが資金調達' not in titles(cur, 'AI') and rep['ignored'] == []
    assert cur['genres']['テクノロジー'][0]['genres'] == ['テクノロジー', 'AI']


def test_big_detail_is_passed_in_full(tmp_path, raw, curated):
    """統合後の detail を書けるよう、big の詳細は途中で切らず全文を Codex に渡す。"""
    long_detail = '詳細の書き出し。' + 'い' * 600 + '詳細の最後の一文。'
    curated['genres']['AI'][0]['detail'] = long_detail
    _, _, _, log = run_cli(tmp_path, raw, curated)
    stdin = json.loads(log.read_text())['stdin']
    assert json.dumps(long_detail, ensure_ascii=False) in stdin
    assert 'detail_head' not in stdin


def test_review_emptied_genres_are_recorded(tmp_path, raw, curated):
    """レビューでジャンルが空になったら review_emptied に残す(ingest が前の回の記事を空で置き換えるため)。"""
    answer = {'drop': [{'id': '話題/0', 'reason': '範囲外'}], 'merge': []}
    line, cur, _, _ = run_cli(tmp_path, raw, curated, answer)
    assert line == 'Codex レビュー: 6→5件（除外1・統合0）'
    assert cur['genres']['話題'] == [] and cur['review_emptied'] == ['話題']
    _, cur, _, _ = run_cli(tmp_path, raw, curated, {'drop': [{'id': 'AI/1', 'reason': 'x'}], 'merge': []})
    assert 'review_emptied' not in cur


def test_big_merge_into_small_is_ignored(tmp_path, raw, curated):
    """big を small の親へ統合する merge は無視する(big の詳細と枠が消えるため)。big 同士は認める。"""
    answer = {'drop': [], 'merge': [{'into': 'テクノロジー/0', 'from': ['AI/0'], 'summary': '', 'detail': ''}]}
    line, cur, rep, _ = run_cli(tmp_path, raw, curated, answer)
    assert 'OpenAIが資金調達' in titles(cur, 'AI')
    assert [i['reason'] for i in rep['ignored']] == ['big を small へ統合しない（into も big にする）']

    curated['genres']['テクノロジー'][0] = item('別のbig', 'big', 80, ['テクノロジー'], detail='詳細。')
    answer = {'drop': [], 'merge': [{'into': 'テクノロジー/0', 'from': ['AI/0'], 'summary': '', 'detail': ''}]}
    line, cur, rep, _ = run_cli(tmp_path, raw, curated, answer)
    assert 'OpenAIが資金調達' not in titles(cur, 'AI') and rep['ignored'] == []
