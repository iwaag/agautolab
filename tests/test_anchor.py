"""What a work conversation says about itself."""

from agautolab import anchor

BOT_ID = 11


def message(sender_id=BOT_ID, content="", id=1):
    return {"id": id, "sender_id": sender_id, "content": content}


def test_the_notes_round_trip():
    assert anchor.mission_note("demo") == "[selfnote][mission] demo"
    assert anchor.parse_mission("[selfnote][mission] demo") == "demo"
    assert anchor.task_note(5512, 3) == "[selfnote][task] 5512#3"
    assert anchor.parse_task("[selfnote][task] 5512#3") == (5512, 3)
    assert anchor.doc_note(77) == "[selfnote][doc] 77"
    assert anchor.parse_doc("[selfnote][doc] 77") == 77
    assert anchor.state_note("completed") == "[selfnote][state] completed"
    assert anchor.parse_state("[selfnote][state] COMPLETED") == "completed"


def test_a_note_of_another_kind_is_not_this_kind():
    assert anchor.parse_task("[selfnote][rootchat] pj-x/workplan-y") is None
    assert anchor.parse_mission("the mission is demo") is None
    assert anchor.parse_task("[selfnote][task]") is None
    assert anchor.parse_task("[selfnote][task] not-a-pair") is None
    assert anchor.parse_doc("[selfnote][doc] later") is None
    assert anchor.parse_state("[selfnote][state]") is None


def test_identity_is_the_notes_own_message_id():
    """Nothing is keyed on a name: the note *is* the mission, or the task."""
    history = [
        message(13, "[selfnote][mission] somebody-elses", id=1),
        message(content="[selfnote][mission] demo", id=5512),
    ]
    assert anchor.own_mission(history, BOT_ID) == (5512, "demo")
    assert anchor.own_task(
        [message(content="[selfnote][task] 5512#3", id=5601)], BOT_ID
    ) == (5601, 5512, 3)


def test_identity_is_written_once_so_the_earliest_note_wins():
    """A topic is anchored by the run that opened it; a later note is a repeat."""
    history = [
        message(content="[selfnote][task] 5512#3", id=5601),
        message(content="[selfnote][task] 5512#9", id=5999),
    ]
    assert anchor.own_task(history, BOT_ID) == (5601, 5512, 3)


def test_state_and_the_current_document_change_so_the_newest_note_wins():
    history = [
        message(content="[selfnote][doc] 10", id=11),
        message(content="[selfnote][state] open", id=12),
        message(content="[selfnote][doc] 20", id=21),
        message(content="[selfnote][state] completed", id=22),
        message(13, "[selfnote][state] cancelled", id=23),  # not ours
    ]
    assert anchor.own_doc(history, BOT_ID) == 20
    assert anchor.own_state(history, BOT_ID) == "completed"


def test_an_unanchored_topic_says_so():
    plain = [message(content="just talking")]
    assert anchor.own_mission(plain, BOT_ID) is None
    assert anchor.own_task(plain, BOT_ID) is None
    assert anchor.own_doc(plain, BOT_ID) is None
    assert anchor.own_state(plain, BOT_ID) is None
    assert anchor.own_rootchat(plain, BOT_ID) is None
