"""Locate project state without depending on the conversation host."""
import os


def runtime_dir(root):
    return os.path.join(root, ".claude", "solo-lab")


def project_root():
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        return os.path.abspath(os.environ["CLAUDE_PROJECT_DIR"])
    here = os.path.abspath(os.getcwd())
    while True:
        if os.path.isfile(os.path.join(here, ".claude", "solo-lab", "manifest.json")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return os.getcwd()
        here = parent
