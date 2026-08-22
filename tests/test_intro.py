"""`python -m agautolab.intro` is the skeleton's `intro_main` over autolab's SPEC."""

from agag import agent as skeleton

from agautolab import instance


def test_the_intro_is_posted_for_this_instance(monkeypatch):
    sent = []

    class Client:
        def send_to_channel(self, channel, topic, text):
            sent.append((channel, topic, text))

    monkeypatch.setattr(skeleton.ZulipClient, "from_env", lambda path: Client())
    monkeypatch.setenv(instance.SPEC.instance_env_var, "autolab-agstudio1")

    skeleton.intro_main(instance.SPEC)

    (channel, topic, text) = sent[0]
    assert (channel, topic) == ("agents", "intro-autolab-agstudio1")
    body = instance.SPEC.intro_path.read_text(encoding="utf-8").rstrip()
    assert text.startswith(body.replace("{instance}", "autolab-agstudio1"))
    assert "{instance}" not in text
    assert "\nPosted: " in text and "\nRevision: `" in text


def test_the_spec_names_autolab_s_vocabulary():
    assert instance.SPEC.agent == "autolab"
    assert instance.SPEC.instance_env_var == "AUTOLAB_INSTANCE_NAME"
    assert instance.SPEC.sweep_prefixes == ("workrun-", "workplan-", "bmining-")
