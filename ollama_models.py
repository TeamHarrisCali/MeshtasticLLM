"""Talks to the local Ollama server about models: what is installed, what can chat / use tools,
and downloading new ones with progress. Used by the web UI; holds no bridge logic."""
import json
import re
import threading
import time

import requests

# Conservative: letters, digits and . _ - / with an optional :tag. Covers "qwen2.5:7b",
# "library/llama3.2:3b" and "hf.co/user/repo:Q4_K_M" style names, nothing exotic.
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,100}(:[A-Za-z0-9._\-]{1,60})?$")


class OllamaError(Exception):
    """Something the operator should see, e.g. Ollama not running or a bad model name."""


def same_model(a, b):
    """'llama3.2' and 'llama3.2:latest' are the same model to Ollama."""
    def norm(n):
        return n if ":" in n else n + ":latest"
    return norm(a or "") == norm(b or "")


class ModelManager:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self._caps = {}            # digest -> capabilities list (they never change for a given digest)
        self._pulls = {}           # name -> progress dict
        self._cancel = {}          # name -> threading.Event
        self._resp = {}            # name -> the open download connection (closed to cancel)
        self._lock = threading.Lock()

    # ---- reading ---------------------------------------------------------
    def _get(self, path, timeout=5):
        try:
            r = requests.get(self.url + path, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise OllamaError(f"Can't reach Ollama at {self.url} ({e.__class__.__name__}). Is it running?") from e

    def capabilities(self, name, digest=None):
        """['completion', 'tools', ...] or None if this Ollama doesn't say."""
        key = digest or name
        if key in self._caps:
            return self._caps[key]
        try:
            r = requests.post(self.url + "/api/show", json={"model": name}, timeout=10)
            r.raise_for_status()
            caps = r.json().get("capabilities")
        except requests.RequestException:
            return None
        self._caps[key] = caps
        return caps

    def thinks(self, name):
        """True if this is a reasoning model that 'thinks' before answering (cached after the first look)."""
        return "thinking" in (self.capabilities(name) or [])

    def installed(self):
        out = []
        for m in self._get("/api/tags").get("models", []):
            d = m.get("details") or {}
            caps = self.capabilities(m["name"], m.get("digest"))
            out.append({
                "name": m["name"], "size": m.get("size", 0), "modified": m.get("modified_at"),
                "params": d.get("parameter_size"), "quant": d.get("quantization_level"), "family": d.get("family"),
                "capabilities": caps,
                # an older Ollama that doesn't report capabilities: assume it can chat
                "chat": True if caps is None else "completion" in caps,
                "tools": bool(caps and "tools" in caps),
            })
        return sorted(out, key=lambda m: m["name"])

    def loaded(self):
        try:
            return [m["name"] for m in self._get("/api/ps").get("models", [])]
        except OllamaError:
            return []

    def preload(self, name):
        """Load a model into memory now so the first real question isn't slow. Best effort."""
        try:
            requests.post(self.url + "/api/generate",
                          json={"model": name, "prompt": "", "stream": False, "keep_alive": "30m"}, timeout=300)
        except requests.RequestException:
            pass

    # ---- downloading -----------------------------------------------------
    def pulls(self):
        with self._lock:
            return [dict(p) for p in sorted(self._pulls.values(), key=lambda p: -p["started"])][:5]

    def start_pull(self, name):
        name = name.strip() if isinstance(name, str) else ""
        if not MODEL_NAME_RE.match(name):
            raise OllamaError("That doesn't look like a model name (try something like qwen2.5:7b).")
        with self._lock:
            if any(p["active"] for p in self._pulls.values()):
                raise OllamaError("Another download is already running. Wait for it or cancel it first.")
            self._pulls[name] = {"name": name, "status": "starting", "total": 0, "completed": 0, "active": True,
                                 "error": None, "done": False, "started": time.time()}
            self._cancel[name] = threading.Event()
        threading.Thread(target=self._pull, args=(name,), daemon=True, name="ollama-pull").start()

    def cancel_pull(self, name):
        if not isinstance(name, str):
            raise OllamaError("That download isn't running.")
        with self._lock:
            ev = self._cancel.get(name)
            active = name in self._pulls and self._pulls[name]["active"]
            resp = self._resp.get(name)
        if not (ev and active):
            raise OllamaError("That download isn't running.")
        ev.set()
        if resp is not None:  # wakes the download thread even if Ollama is between progress updates
            try:
                resp.close()
            except Exception:
                pass

    def _set(self, name, **kw):
        with self._lock:
            self._pulls[name].update(kw)

    def _pull(self, name):
        layers = {}  # digest -> [total, completed]; Ollama reports progress per layer
        resp = None
        try:
            resp = requests.post(self.url + "/api/pull", json={"model": name, "stream": True},
                                 stream=True, timeout=(5, 120))
            with self._lock:
                self._resp[name] = resp
            if resp.status_code >= 400:
                try:
                    msg = resp.json().get("error", resp.text)
                except ValueError:
                    msg = resp.text
                raise OllamaError(str(msg)[:200])
            # chunk_size=1: the default waits for 512 bytes, which stalls the progress display
            # whenever Ollama sends small updates slowly (start of a download, verification)
            for line in resp.iter_lines(chunk_size=1):
                if self._cancel[name].is_set():
                    self._set(name, status="cancelled", active=False)
                    return
                if not line:
                    continue
                ev = json.loads(line)
                if "error" in ev:
                    raise OllamaError(str(ev["error"])[:200])
                status = ev.get("status", "")
                if ev.get("digest") and ev.get("total"):
                    layers[ev["digest"]] = [ev["total"], ev.get("completed", 0)]
                self._set(name, status=status,
                          total=sum(t for t, _ in layers.values()), completed=sum(c for _, c in layers.values()))
                if status == "success":
                    self._set(name, status="success", done=True, active=False)
                    return
            self._set(name, status="stopped early", error="The download ended before Ollama said it was done.",
                      active=False)
        except OllamaError as e:
            self._set(name, status="failed", error=str(e), active=False)
        except requests.RequestException as e:
            if self._cancel[name].is_set():
                self._set(name, status="cancelled", active=False)
            else:
                self._set(name, status="failed", active=False,
                          error=f"Lost contact with Ollama during the download ({e.__class__.__name__}).")
        except Exception as e:  # malformed stream, or the connection was closed under us by a cancel
            if self._cancel[name].is_set():
                self._set(name, status="cancelled", active=False)
            else:
                self._set(name, status="failed", error=f"Unexpected error: {e}", active=False)
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            with self._lock:
                self._resp.pop(name, None)
