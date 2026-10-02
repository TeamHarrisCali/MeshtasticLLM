#!/usr/bin/env sh
# One-step setup for Linux and macOS. Finds a Python (3.9 or newer), then runs setup_env.py, which builds the private
# environment, installs the dependencies and checks Ollama and the radio. Safe to run again any time.
#   ./setup.sh [options]        e.g. ./setup.sh --check      ./setup.sh --pull-model      ./setup.sh --recreate
# If no suitable Python is installed it says which command installs one (and offers to run it).
dir=$(cd "$(dirname "$0")" && pwd)
cd "$dir" || exit 1

find_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

py=$(find_python)
if [ -z "$py" ]; then
  echo "No Python 3.9 or newer was found."
  if [ "$(uname -s)" = "Darwin" ]; then
    cmd="brew install python"
    command -v brew >/dev/null 2>&1 || { echo "Install it from https://www.python.org/downloads/ (or get Homebrew first), then run ./setup.sh again."; exit 1; }
  elif command -v apt-get >/dev/null 2>&1; then cmd="sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip"
  elif command -v dnf >/dev/null 2>&1; then cmd="sudo dnf install -y python3 python3-pip"
  elif command -v yum >/dev/null 2>&1; then cmd="sudo yum install -y python3 python3-pip"
  elif command -v pacman >/dev/null 2>&1; then cmd="sudo pacman -S --needed python python-pip"
  elif command -v zypper >/dev/null 2>&1; then cmd="sudo zypper install -y python3 python3-pip"
  elif command -v apk >/dev/null 2>&1; then cmd="sudo apk add python3 py3-pip"
  else echo "Install Python from https://www.python.org/downloads/ and run ./setup.sh again."; exit 1
  fi
  echo "It can be installed with:  $cmd"
  if [ -t 0 ]; then
    printf "Run that now? [y/N] "
    read -r answer
    case "$answer" in y|Y|yes|YES) sh -c "$cmd" || exit 1; py=$(find_python) ;; esac
  fi
  [ -n "$py" ] || { echo "Run that command, then ./setup.sh again."; exit 1; }
fi

exec "$py" setup_env.py "$@"
