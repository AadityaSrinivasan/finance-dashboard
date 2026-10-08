"""Put a backed-up token encryption key back into Windows Credential Manager
(e.g. after reinstalling Windows or moving to a new computer).

Run:  .venv\\Scripts\\python scripts\\restore_key.py E:\\finance-key.txt
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.crypto import TokenKeyMissing, export_key, import_key  # noqa: E402

args = [a for a in sys.argv[1:] if a != "--force"]
if len(args) != 1:
    print("Usage: python scripts\\restore_key.py <path-to-key-file> [--force]")
    sys.exit(1)
new_key = Path(args[0]).read_text().strip()
try:
    current = export_key()
except TokenKeyMissing:
    current = None
if current and current != new_key and "--force" not in sys.argv:
    print("A different key is already stored. Replacing it makes tokens saved with it unreadable.\n"
          "Back it up first (scripts\\backup_key.py), then re-run with --force.")
    sys.exit(1)
import_key(new_key)
print("Key restored to Windows Credential Manager.")
