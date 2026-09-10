"""autolab's own path does not go through Plane, and this is how we know.

`refactor` p1's verification is "prepare and serve a multi-task request with
Plane access unavailable to autolab". The live half of that is step 4's
demonstration; this is the half a test can hold: the import graph. An agent
that never imports the Plane client cannot call it, whatever the credentials
on the node say — and *not importing* is a property that quietly decays, so
it is asserted rather than remembered.

`agag.plane` itself is gone since `refactor` p3 — forge, cagent and this
agent all keep their records in Zulip now — so this assertion is no longer
about one agent's restraint. It is what stops the module coming back.
"""

import importlib
import pkgutil
import subprocess
import sys

import pytest

import agautolab


def autolab_modules():
    return [
        f"agautolab.{info.name}"
        for info in pkgutil.iter_modules(agautolab.__path__)
    ]


@pytest.mark.parametrize("name", autolab_modules())
def test_no_module_of_this_agent_reaches_the_plane_client(name):
    module = importlib.import_module(name)
    reached = {
        attribute
        for attribute, value in vars(module).items()
        if getattr(value, "__module__", "") == "agag.plane"
        or getattr(value, "__name__", "") == "agag.plane"
    }
    assert reached == set(), f"{name} imports {sorted(reached)} from agag.plane"


def test_importing_the_whole_agent_never_loads_the_plane_client():
    """The transitive check the per-module one cannot make: nothing autolab
    imports imports it either.

    In a subprocess, because the only other way to ask is to empty
    `sys.modules` — and re-importing this agent mid-suite gives every later
    test a second copy of every class it compares by identity.
    """
    probe = (
        "import sys, importlib, pkgutil, agautolab\n"
        "for info in pkgutil.iter_modules(agautolab.__path__):\n"
        "    importlib.import_module('agautolab.' + info.name)\n"
        "print('agag.plane' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False", result.stdout


def test_no_plane_credential_is_named_anywhere_in_the_agent():
    import agautolab.project_init as project_init

    assert not hasattr(project_init, "PLANE_ENV")
    assert not hasattr(project_init, "load_plane_config")
    assert not hasattr(project_init, "ensure_plane_project")
