"""Phase 3 intentionally deletes the agautolab legacy selection vocabulary."""

from pathlib import Path

LEGACY = (
    "AUTOLAB_WINDOW_BACKEND", "AUTOLAB_WINDOW_MODEL", "AUTOLAB_OLLAMA_URL",
    "AUTOLAB_SUMMARY_MODEL", "AUTOLAB_AGENT_MODEL", "AUTOLAB_CLAUDE_BIN",
    "run_ollama", "claude_output.json", ".local/agent/claude_bin",
    ".local/direction/",
)


def test_legacy_names_are_absent_from_runtime_and_docs():
    root = Path(__file__).resolve().parents[1]
    files = [root / "README.md", root / "AGENT_GUIDE.md", *(
        path for area in (root / "agent", root / "src")
        for path in area.rglob("*") if path.is_file() and "__pycache__" not in path.parts
    )]
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in files)
    assert not [name for name in LEGACY if name in text]
