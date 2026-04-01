"""
Migrate JSONL data to SQLite (memory.db)
========================================

Reads existing JSONL exchange/reflection files, re-embeds them
(since embeddings were not persisted in the old system), and inserts
them into the new SQLite database.

Usage:
    python scripts/migrate_to_sqlite.py [--data-dir ./data] [--dry-run]
"""

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.memory.store import MemoryStore
from src.memory.models import Exchange, DailyReflection
from src.services.embedding_service import EmbeddingService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_jsonl(path: str) -> list:
    """Load records from a JSONL file."""
    records = []
    if not os.path.exists(path):
        return records
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning(f"Skipping corrupt line {i} in {path}: {e}")
    return records


def load_json(path: str) -> dict:
    """Load a single JSON file."""
    with open(path) as f:
        return json.load(f)


async def migrate(data_dir: str, dry_run: bool = False):
    data_path = Path(data_dir)
    db_path = data_path / "memory.db"

    if db_path.exists() and not dry_run:
        logger.error(f"Database already exists at {db_path}. Remove it first or use --dry-run.")
        return

    # ── Load existing JSONL data ──────────────────────────────────
    exchanges_path = data_path / "shared" / "memory" / "exchanges.jsonl"
    exchanges = load_jsonl(str(exchanges_path))
    logger.info(f"Loaded {len(exchanges)} exchanges from {exchanges_path}")

    # Load per-persona reflections
    reflections = []
    persona_dirs = [d for d in data_path.iterdir() if d.is_dir() and d.name not in ("shared", "models", "personas", "sessions", "state")]
    for persona_dir in persona_dirs:
        ref_path = persona_dir / "memory" / "reflections.jsonl"
        persona_refs = load_jsonl(str(ref_path))
        if persona_refs:
            logger.info(f"Loaded {len(persona_refs)} reflections for {persona_dir.name}")
            reflections.extend(persona_refs)

    # Load relationships
    relationships = []
    rel_dir = data_path / "shared" / "relationships"
    if rel_dir.exists():
        for f in rel_dir.glob("*.json"):
            try:
                relationships.append(load_json(str(f)))
            except Exception as e:
                logger.warning(f"Skipping {f}: {e}")
    if relationships:
        logger.info(f"Loaded {len(relationships)} relationships")

    # Load sessions
    sessions = []
    for persona_dir in persona_dirs:
        sess_dir = persona_dir / "sessions"
        if sess_dir.exists():
            for f in sess_dir.glob("*.json"):
                try:
                    sessions.append(load_json(str(f)))
                except Exception as e:
                    logger.warning(f"Skipping {f}: {e}")
    if sessions:
        logger.info(f"Loaded {len(sessions)} sessions")

    # Load existing npz embeddings if they exist
    npz_embeddings = {}
    for search_dir in [data_path / "shared" / "indexes", *(d / "indexes" for d in persona_dirs)]:
        for npz_file in search_dir.glob("*_vectors.npz") if search_dir.exists() else []:
            manifest_file = npz_file.with_name(npz_file.stem.replace("_vectors", "_manifest") + ".json")
            if manifest_file.exists():
                try:
                    import numpy as np
                    vectors = np.load(str(npz_file))["vectors"]
                    manifest = load_json(str(manifest_file))
                    ids = manifest.get("ids", [])
                    for i, rid in enumerate(ids):
                        if i < len(vectors):
                            npz_embeddings[rid] = vectors[i].tolist()
                    logger.info(f"Recovered {len(ids)} embeddings from {npz_file.name}")
                except Exception as e:
                    logger.warning(f"Failed to load {npz_file}: {e}")

    if npz_embeddings:
        logger.info(f"Total recovered embeddings: {len(npz_embeddings)}")

    if dry_run:
        logger.info("DRY RUN — would migrate:")
        logger.info(f"  {len(exchanges)} exchanges")
        logger.info(f"  {len(reflections)} reflections")
        logger.info(f"  {len(relationships)} relationships")
        logger.info(f"  {len(sessions)} sessions")
        logger.info(f"  {len(npz_embeddings)} recovered embeddings")
        need_embed = len(exchanges) + len(reflections) - len(npz_embeddings)
        logger.info(f"  ~{max(0, need_embed)} records need re-embedding")
        return

    # ── Initialize services ───────────────────────────────────────
    embed_service = await EmbeddingService.get_instance()
    store = MemoryStore(str(db_path))
    await store.initialize()

    # ── Migrate exchanges ─────────────────────────────────────────
    logger.info("Migrating exchanges...")
    for i, rec in enumerate(exchanges):
        ex = Exchange.from_dict(rec)

        # Try recovered embedding first, then re-embed
        if ex.id in npz_embeddings:
            ex.embedding = npz_embeddings[ex.id]
        else:
            try:
                ex.embedding = await embed_service.embed_document(ex.content_for_embedding)
            except Exception as e:
                logger.warning(f"Failed to embed exchange {ex.id}: {e}")

        await store.append_exchange(ex)

        if (i + 1) % 10 == 0:
            logger.info(f"  {i + 1}/{len(exchanges)} exchanges migrated")

    logger.info(f"Migrated {len(exchanges)} exchanges")

    # ── Migrate reflections ───────────────────────────────────────
    if reflections:
        logger.info("Migrating reflections...")
        for rec in reflections:
            ref = DailyReflection.from_dict(rec)

            if ref.id in npz_embeddings:
                ref.embedding = npz_embeddings[ref.id]
            elif ref.summary:
                try:
                    ref.embedding = await embed_service.embed_document(ref.summary)
                except Exception as e:
                    logger.warning(f"Failed to embed reflection {ref.id}: {e}")

            await store.append_reflection(ref)

        logger.info(f"Migrated {len(reflections)} reflections")

    # ── Migrate relationships ─────────────────────────────────────
    if relationships:
        logger.info("Migrating relationships...")
        for rec in relationships:
            user_id = rec.get("user_id", rec.get("id", ""))
            if user_id:
                await store.save_relationship(user_id, rec)
        logger.info(f"Migrated {len(relationships)} relationships")

    # ── Migrate sessions ──────────────────────────────────────────
    if sessions:
        logger.info("Migrating sessions...")
        for rec in sessions:
            session_id = rec.get("session_id", rec.get("id", ""))
            if session_id:
                await store.save_session(session_id, rec)
        logger.info(f"Migrated {len(sessions)} sessions")

    # ── Verify ────────────────────────────────────────────────────
    stats = store.stats()
    logger.info(f"Migration complete. DB stats: {stats}")

    expected = {
        "exchanges": len(exchanges),
        "reflections": len(reflections),
    }
    for table, expected_count in expected.items():
        actual = stats.get(table, 0)
        if actual != expected_count:
            logger.error(f"Count mismatch for {table}: expected {expected_count}, got {actual}")
        else:
            logger.info(f"  {table}: {actual} OK")

    await store.shutdown()

    # ── Archive old files ─────────────────────────────────────────
    legacy_dir = data_path / "legacy"
    os.makedirs(legacy_dir, exist_ok=True)

    if exchanges_path.exists():
        dest = legacy_dir / "exchanges.jsonl"
        shutil.move(str(exchanges_path), str(dest))
        logger.info(f"Archived {exchanges_path} → {dest}")

    for persona_dir in persona_dirs:
        for old_file in list((persona_dir / "memory").glob("*.jsonl")) + \
                         list((persona_dir / "indexes").glob("*")):
            dest = legacy_dir / persona_dir.name / old_file.name
            os.makedirs(dest.parent, exist_ok=True)
            shutil.move(str(old_file), str(dest))
            logger.info(f"Archived {old_file} → {dest}")

    for old_file in (data_path / "shared" / "indexes").glob("*"):
        dest = legacy_dir / "shared_indexes" / old_file.name
        os.makedirs(dest.parent, exist_ok=True)
        shutil.move(str(old_file), str(dest))
        logger.info(f"Archived {old_file} → {dest}")

    logger.info("Migration finished successfully!")


def main():
    parser = argparse.ArgumentParser(description="Migrate JSONL data to SQLite")
    parser.add_argument("--data-dir", default="./data", help="Data directory path")
    parser.add_argument("--dry-run", action="store_true", help="Preview without migrating")
    args = parser.parse_args()

    asyncio.run(migrate(args.data_dir, args.dry_run))


if __name__ == "__main__":
    main()
