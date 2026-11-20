import json

import pytest

from finchecker.cli import DEMO_CASES, main


def test_demo_runs_offline_and_exits_zero():
    assert main(['demo']) == 0


def test_demo_cases_are_nonempty():
    assert DEMO_CASES and all(text.strip() for _, text in DEMO_CASES)


@pytest.mark.parametrize(('text', 'code', 'status'), [
    ('600519 2025年报：营业收入同比增长1.21%。', 1, 'mismatch'),
    ('600519 2025年报：营业收入同比下降1.21%。', 0, 'verified'),
    ('暂不披露数据。', 3, 'no_claims'),
    ('000001 2025年报：营业收入130亿元。', 3, 'unverifiable'),
    ('600519 2025年报：营业收入同比下降1.21%；2025年报：净利润130亿元。', 3, 'incomplete'),
])
def test_cli_distinguishes_report_outcomes(capsys, text, code, status):
    assert main(['--text', text, '--json']) == code
    report = json.loads(capsys.readouterr().out)
    assert (report['status'], report['source'], report['simulated']) == (status, 'disclosure', False)


def test_cli_does_not_use_mock_for_user_text():
    with pytest.raises(SystemExit) as error:
        main(['--text', '600519 2025年三季报：营业收入130亿元。', '--source', 'mock'])
    assert error.value.code == 2


def test_cli_requires_input():
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2
