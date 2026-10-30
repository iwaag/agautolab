"""Delivery routing and reference availability from an unrelated run directory."""
import json
from pathlib import Path

from agautolab.wfexec import Executor, Settings, execution_contract


def claim(tmp_path, source=None, kind='braindump'):
    folder = tmp_path / 'devdocs/runs/freework/run-001'
    folder.mkdir(parents=True)
    (folder / 'braindump.md').write_text('一覧を列挙してください。')
    (folder / 'run.json').write_text(json.dumps({'input': {'kind': kind, 'file': 'braindump.md', 'source': source}}))
    return {'cwd': str(tmp_path), 'ref': {'workflow': 'freework', 'run': 'run-001'}, 'attempt': {'id': 'a1'}, 'brief': 'Current run facts', 'execution': {}}


def test_browser_question_delivered_through_records(tmp_path):
    prompt = Executor(Settings(wfe=['wfe'])).prompt(claim(tmp_path))
    assert prompt.startswith('# This request\n\n一覧を列挙してください。')
    assert '--artifact' in prompt.split('# Your work')[0]
    assert 'wfe conversation reply --re' not in prompt
    directory = Path(prompt.split('Guide directory: ')[1].splitlines()[0])
    assert (directory / 'reference/delivery.md').is_file()
    assert '## Results the person needs to try' not in prompt


def test_conversation_uses_original_message(tmp_path):
    prompt = Executor(Settings(wfe=['wfe'])).prompt(claim(tmp_path, {'kind': 'conversation', 'conversation': 'room-a', 'message': 42}))
    assert 'Conversation room-a, message #42' in prompt
    assert 'wfe conversation reply --re 42' in prompt


def test_delegated_request_identifies_receiver(tmp_path):
    prompt = Executor(Settings(wfe=['wfe'])).prompt(claim(tmp_path, {'kind': 'conversation', 'conversation': 'original', 'message': 10}, kind='request'))
    assert 'requesting agent receives' in prompt


def test_reference_edit_changes_session_contract(tmp_path, monkeypatch):
    import agautolab.wfexec as module
    guide = tmp_path / 'guide.md'
    guide.write_text('Entry')
    notes = tmp_path / 'reference'
    notes.mkdir()
    note = notes / 'delivery.md'
    note.write_text('First')
    monkeypatch.setattr(module, 'GUIDE', guide)
    before = execution_contract()
    note.write_text('Revised')
    assert execution_contract() != before
