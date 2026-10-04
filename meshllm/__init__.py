"""Meshtastic LLM Bridge: a local AI you can reach over a LoRa radio mesh.

Run it with `python -m meshllm`. The modules are described in docs/files.md. This file is deliberately empty of imports so
that importing the package never needs the radio, Ollama or any third-party library. That is also why the version is only a string
here: it is the single source of truth (`python -m meshllm --version`, the dashboard, the release workflow and docs/CHANGELOG.md all
read it, and tests/test_release.py checks they agree), and setup scripts and CI can read it without installing anything.
"""
__version__ = "0.1.0"
