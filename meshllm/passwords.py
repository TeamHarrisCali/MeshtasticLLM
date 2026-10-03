"""Dashboard passwords: scrypt hashing, the hash file, and `python -m meshllm --set-password`.

A password is never read from the command line or an environment variable and no plaintext is ever stored, logged or put in a URL.
`--set-password` prompts without echo (getpass), hashes with scrypt and writes ONE line to a file only the owner can read. The bridge is
then pointed at that file with `--password-hash-file PATH` (or the MESHLLM_PASSWORD_HASH_FILE environment variable, which names a
file, not a password). The stored line carries its own parameters, so the defaults can be raised later without breaking old hashes:

    scrypt$32768$8$1$<salt, hex>$<key, hex>          (n, r, p, a 16-byte random salt, a 32-byte derived key)

There are two accounts: `admin` (everything) and an optional `viewer` (read-only, see websecurity.py), each with its own file.
"""
import getpass
import hashlib
import hmac
import os
import secrets
import stat
import sys
import tempfile
import unicodedata

ALGORITHM = "scrypt"
N, R, P = 2 ** 15, 8, 1          # cost parameters for NEW hashes (about 100 ms and 32 MiB per attempt)
SALT_BYTES, KEY_BYTES = 16, 32
MIN_N, MAX_N = 2 ** 15, 2 ** 20  # a stored hash outside these bounds is rejected, so a corrupted or hostile file cannot cost gigabytes
MAX_R, MAX_P = 16, 4
MIN_LENGTH = 12                  # characters; a phone keyboard can type this and a LAN login is only as strong as it is
MAX_LENGTH = 256                 # characters; bounds the work an attacker can make a login do
ROLES = ("admin", "viewer")


class PasswordError(ValueError):
    """A problem with a password or a hash file; the message is meant for the person at the keyboard."""


def _normalise(password):
    """The bytes that are hashed: NFKC-normalised UTF-8, so the same typed password works from any keyboard or device."""
    return unicodedata.normalize("NFKC", password).encode("utf-8")


def _maxmem(n, r, p):
    """A memory limit comfortably above what scrypt needs for these parameters (the library's default limit rejects n = 2**15)."""
    return 2 * 128 * r * (n + p + 2) + 1024 * 1024


def check_strength(password):
    """Raise PasswordError if the password is too short, too long or has no variety at all."""
    if not isinstance(password, str):
        raise PasswordError("The password must be text.")
    if len(password) < MIN_LENGTH:
        raise PasswordError(f"Use at least {MIN_LENGTH} characters (a few words in a row works well).")
    if len(password) > MAX_LENGTH:
        raise PasswordError(f"Use at most {MAX_LENGTH} characters.")
    if len(set(password)) < 4:
        raise PasswordError("That password is too repetitive.")


def hash_password(password, n=N, r=R, p=P, salt=None):
    """The storable line for a password (see the module docstring). A fresh random salt unless one is given (tests)."""
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    key = hashlib.scrypt(_normalise(password), salt=salt, n=n, r=r, p=p, maxmem=_maxmem(n, r, p), dklen=KEY_BYTES)
    return f"{ALGORITHM}${n}${r}${p}${salt.hex()}${key.hex()}"


def parse_hash(line):
    """(n, r, p, salt, key) from a stored line, or raise PasswordError if it is malformed or its parameters are out of bounds."""
    parts = (line or "").strip().split("$")
    if len(parts) != 6 or parts[0] != ALGORITHM:
        raise PasswordError("That is not a password hash written by --set-password.")
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, key = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        raise PasswordError("The password hash is damaged.")
    if not (MIN_N <= n <= MAX_N and n & (n - 1) == 0 and 1 <= r <= MAX_R and 1 <= p <= MAX_P and len(salt) >= 8 and len(key) >= 16):
        raise PasswordError("The password hash has unusable parameters.")
    return n, r, p, salt, key


def verify_password(password, line):
    """True if `password` matches the stored line. Constant-time comparison; any malformed input is simply False."""
    try:
        n, r, p, salt, key = parse_hash(line)
        if not isinstance(password, str) or len(password) > MAX_LENGTH:
            return False
        derived = hashlib.scrypt(_normalise(password), salt=salt, n=n, r=r, p=p, maxmem=_maxmem(n, r, p), dklen=len(key))
    except (PasswordError, ValueError, MemoryError):
        return False
    return hmac.compare_digest(derived, key)


def needs_rehash(line):
    """True if a stored hash uses weaker parameters than today's defaults (the owner can run --set-password again to raise them)."""
    n, r, p, _, _ = parse_hash(line)
    return (n, r, p) < (N, R, P)


def dummy_hash():
    """A valid hash of a random password nobody knows, verified against when an account does not exist so timing does not tell."""
    return hash_password(secrets.token_urlsafe(24))


def read_hash_file(path):
    """The one hash line in the file (validated), or raise PasswordError saying what is wrong."""
    try:
        with open(path, "r", encoding="ascii") as f:
            text = f.read(4096)
    except (OSError, UnicodeDecodeError) as e:
        raise PasswordError(f"Cannot read the password hash file {path}: {type(e).__name__}.")
    line = text.strip()
    parse_hash(line)
    return line


def loose_permissions(path):
    """True if the file can be read by anyone but its owner (POSIX only; Windows file modes say nothing useful)."""
    if os.name != "posix":
        return False
    try:
        return bool(os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    except OSError:
        return False


def write_hash_file(path, line):
    """Write the hash to `path` with mode 600 (owner only), atomically, in a folder only the owner can enter if it had to be created."""
    path = os.path.abspath(path)
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pw-", dir=folder)   # mkstemp creates the file with mode 600
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(line + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def default_path(role):
    """Where --set-password writes when no file is named: the user's own configuration folder (never inside the project)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "meshllm", f"{role}.hash")


def set_password_main(role="admin", path=None, other_path=None, prompt=getpass.getpass, interactive=None, out=print):
    """`--set-password`: ask twice without echo, hash, write the file. Returns the process exit code.

    `prompt` and `interactive` exist so tests can drive it; `other_path` is the other account's file, used to refuse giving both accounts
    the same password. Nothing typed is ever printed or kept beyond this function."""
    if role not in ROLES:
        out(f"error: --role must be one of {', '.join(ROLES)}")
        return 2
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        out("error: --set-password asks for the password at a terminal (it is never read from arguments, the environment or a pipe).")
        return 2
    path = path or default_path(role)
    out(f"Setting the dashboard {role} password. It is stored only as a scrypt hash, in {path}")
    try:
        first = prompt(f"New {role} password (not shown): ")
        check_strength(first)
        if prompt("Type it again: ") != first:
            out("error: the two entries did not match; nothing was changed.")
            return 1
        if other_path and os.path.exists(other_path):
            try:
                if verify_password(first, read_hash_file(other_path)):
                    out("error: the admin and viewer passwords must differ; nothing was changed.")
                    return 1
            except PasswordError:
                pass                         # the other file is unusable; that is reported when the bridge starts
        write_hash_file(path, hash_password(first))
    except PasswordError as e:
        out(f"error: {e}")
        return 1
    except (EOFError, KeyboardInterrupt):
        out("\nCancelled; nothing was changed.")
        return 1
    except OSError as e:
        out(f"error: could not write {path}: {type(e).__name__}")
        return 1
    flag = "--password-hash-file" if role == "admin" else "--viewer-password-hash-file"
    out(f"Saved. Start the bridge with:  python -m meshllm {flag} {path}")
    out("A bridge that is already running signs everyone out of this account within a few seconds.")
    return 0
