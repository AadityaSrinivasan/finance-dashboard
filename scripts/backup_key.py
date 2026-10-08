"""Save a copy of the token encryption key to a file you choose (e.g. a USB stick
or password manager attachment). Without this key, the saved Plaid access tokens
can't be used, and on the Trial plan you can't get those Item slots back.

Run:  .venv\\Scripts\\python scripts\\backup_key.py E:\\finance-key.txt
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.crypto import export_key  # noqa: E402

if len(sys.argv) != 2:
    print("Usage: python scripts\\backup_key.py <path-to-save-key-file>")
    sys.exit(1)
dest = Path(sys.argv[1])
if dest.exists():
    print(f"{dest} already exists; choose another file name.")
    sys.exit(1)
dest.write_text(export_key())
print(f"Key saved to {dest}. Keep it somewhere safe and private (not in this folder).")
