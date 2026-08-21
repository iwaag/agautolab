"""What a `workrun-` topic says about itself."""

from agautolab import anchor

BOT_ID = 11


def message(sender_id=BOT_ID, content=""):
    return {"sender_id": sender_id, "content": content}


def test_the_work_note_round_trips():
    assert anchor.work_note("issue-3") == "[selfnote][work] issue-3"
    assert anchor.parse_work("[selfnote][work] issue-3") == "issue-3"


def test_a_note_of_another_kind_is_not_a_work_note():
    assert anchor.parse_work("[selfnote][rootchat] pj-x/workplan-y") is None
    assert anchor.parse_work("the work is issue-3") is None
    assert anchor.parse_work("[selfnote][work]") is None


def test_own_work_finds_this_bots_earliest_note():
    """A topic runs the task it was opened for; a later note is a repeat."""
    history = [
        message(13, "[selfnote][work] somebody-elses"),
        message(content="[selfnote][work] issue-3"),
        message(content="[selfnote][work] issue-9"),
    ]
    assert anchor.own_work(history, BOT_ID) == "issue-3"


def test_an_unanchored_topic_says_so():
    assert anchor.own_work([message(content="just talking")], BOT_ID) is None
    assert anchor.own_rootchat([message(content="just talking")], BOT_ID) is None
