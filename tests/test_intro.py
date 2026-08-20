from agautolab import intro


def test_topic_is_the_per_instance_intro_topic(monkeypatch):
    monkeypatch.setattr(intro, "instance_name", lambda: "autolab-agstudio1")
    assert intro.topic() == "intro-autolab-agstudio1"


def test_main_posts_the_committed_markdown_to_the_shared_board(monkeypatch):
    sent = []

    class Client:
        def send_to_channel(self, channel, topic, text):
            sent.append((channel, topic, text))

    monkeypatch.setattr(intro.ZulipClient, "from_env", lambda path: Client())
    monkeypatch.setattr(intro, "instance_name", lambda: "autolab-agstudio1")

    intro.main()

    (channel, topic, text) = sent[0]
    assert (channel, topic) == ("agents", "intro-autolab-agstudio1")
    body = intro.INTRO_PATH.read_text(encoding="utf-8").rstrip()
    assert text.startswith(body.replace("{instance}", "autolab-agstudio1"))
    assert "{instance}" not in text
    assert "\nPosted: " in text and "\nRevision: `" in text
