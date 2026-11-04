"""Package source using an explicit allowlist; never include credentials/data."""
from pathlib import Path
import zipfile

FILES = ["main.py", "accounts.py", "delivery.py", "storage.py", "configure.py",
         "setup_key.py", "requirements.txt", "README.md", "bothost.json", "Dockerfile",
         ".dockerignore", ".gitignore", ".env.example", "test_bot.py", "build_release.py"]

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    target = root.parent / "telegram_bot.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(root / name, name)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(FILES)
    print(target)
    print(f"Files: {len(FILES)}; bytes: {target.stat().st_size}")
