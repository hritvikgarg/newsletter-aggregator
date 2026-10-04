"""Load pipeline/config.yaml + data/category_feeds.csv into one Config object."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PIPELINE_DIR = Path(__file__).resolve().parents[2]   # .../pipeline
REPO_ROOT = PIPELINE_DIR.parent


@dataclass
class Config:
    repo_root: Path
    archive_dir: Path
    db_path: Path
    inbox_address: str
    query: str
    overlap_days: int
    ignore_senders: list[str]
    tag_to_segment: dict[str, str]                # "techai" -> "1-TechAI"
    sender_overrides: list[tuple[str, str]]       # (substring, segment), checked in order
    active_segments: list[str]
    llm: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); never overrides real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_tag_map(category_feeds_csv: Path) -> dict[str, str]:
    """Map '+tag' -> segment id from data/category_feeds.csv (subscribe_email column)."""
    out: dict[str, str] = {}
    with open(category_feeds_csv, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            addr = row["subscribe_email"].strip().lower()
            if "+" in addr:
                tag = addr.split("+", 1)[1].split("@", 1)[0]
                out[tag] = row["segment"].strip()
    return out


def load_config(path: Path | None = None, repo_root: Path | None = None) -> Config:
    repo_root = repo_root or REPO_ROOT
    path = path or (PIPELINE_DIR / "config.yaml")
    _load_dotenv(PIPELINE_DIR / ".env")
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    p = raw["paths"]
    archive_dir = Path(os.environ.get("NLAGG_ARCHIVE_DIR", repo_root / p["archive_dir"]))
    db_path = Path(os.environ.get("NLAGG_DB_PATH", repo_root / p["db_path"]))
    seg = raw.get("segments", {})
    inbox = raw["inbox"]

    return Config(
        repo_root=repo_root,
        archive_dir=archive_dir,
        db_path=db_path,
        inbox_address=inbox["address"].lower(),
        query=inbox.get("query", "in:anywhere"),
        overlap_days=int(inbox.get("overlap_days", 3)),
        ignore_senders=[s.lower() for s in inbox.get("ignore_senders", [])],
        tag_to_segment=load_tag_map(repo_root / p["category_feeds"]),
        sender_overrides=[(o["match"].lower(), o["segment"]) for o in seg.get("sender_overrides", [])],
        active_segments=list(seg.get("active", [])),
        llm=raw.get("llm", {}),
        raw=raw,
    )
