from finchecker.cli import DEMO_CASES, main


def test_demo_runs_offline_and_exits_zero():
    assert main(['demo']) == 0


def test_demo_cases_are_nonempty():
    assert DEMO_CASES and all(text.strip() for _, text in DEMO_CASES)


def test_plain_text_mode_still_reports_failures():
    # inverted growth against MockSource truth must produce a non-zero exit code
    rc = main(['--text', '600519 2025年三季报：营业收入同比下降8.3%。'])
    assert rc == 1
