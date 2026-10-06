"""Review tracked and new source files for size and common accidental secrets/artifacts."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BUDGET = 400
FORBIDDEN_SUFFIXES = {
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".xz",
    ".7z",
    ".whl",
    ".exe",
    ".msi",
    ".dll",
    ".so",
    ".iso",
    ".vhdx",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".key",
}
SECRET_FIELD = re.compile(r'^\s*secret_key:\s*["\']?([^"\'\s#]+)', re.MULTILINE)
SECRET_PLACEHOLDERS = {"local-development-only-change-before-deployment", "CHANGE_ME", "changeme"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
]


def main() -> None:
    result = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT
    )
    errors: list[str] = []
    for name in sorted(set(result.decode().split("\0")) - {""}):
        path = ROOT / name
        if not path.is_file():
            continue
        if name.startswith(("work/", ".venv/")) or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"{name}: generated/private artifact must stay outside Git")
            continue
        if path.name == ".env" or path.name.startswith(".env.") and path.name != ".env.example":
            errors.append(f"{name}: environment secrets must stay outside Git")
        if path.stat().st_size > 1_000_000:
            errors.append(f"{name}: file exceeds 1 MB source artifact budget")
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            lines = len(content.splitlines())
            limit = BUDGET
            if lines > limit:
                errors.append(
                    f"{name}: {lines} lines exceeds {limit}; split at a real owner boundary"
                )
        secret_fields = SECRET_FIELD.findall(content)
        if any(
            value not in SECRET_PLACEHOLDERS and not value.startswith("${")
            for value in secret_fields
        ):
            errors.append(
                f"{name}: secret_key must use an explicit development placeholder or environment reference"
            )
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            errors.append(f"{name}: potential credential detected; inspect locally")
    if errors:
        raise SystemExit("\n".join(errors))
    print("Source budgets and credential/artifact policy passed")


if __name__ == "__main__":
    main()
