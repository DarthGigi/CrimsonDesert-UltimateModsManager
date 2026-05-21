"""Skill Format 3 list-of-dict field writer.

Uses the vendored skill parser at
`src/cdumm/_vendor/skillinfo_parser.py`. That parser file is
distributed under MPL-2.0 (see
`src/cdumm/_vendor/skillinfo_parser_LICENSE_MPL2`).

Whole-table approach (mirrors iteminfo_writer): the parser is
already verified byte-roundtrip on vanilla 1.0.0.4 skill.pabgb,
so we parse vanilla, mutate target entries' list fields, serialize,
and emit a single offset=0 change.

Bug from timuela on GitHub #41 (focus_aerial_roll skill mod):
Format 3 mods targeting skill.pabgb with `_useResourceStatList`
were skipped at validation time.
"""
from __future__ import annotations
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cdumm.engine.format3_handler import Format3Intent

logger = logging.getLogger(__name__)

_DEV_VENDOR_DIR = Path(__file__).resolve().parent.parent / "_vendor"
_cached_module: Any | None = None
_load_attempted = False


# Skill list-of-dict fields the vendored parser exposes. Mirrors
# the keys produced by `parse_skill_entry`.
SUPPORTED_FIELDS = {
    "_useResourceStatList",
    "_buffLevelList",
}


def _candidate_dirs() -> list[Path]:
    out: list[Path] = [_DEV_VENDOR_DIR]
    if hasattr(sys, "_MEIPASS"):
        meipass = Path(sys._MEIPASS)
        out.append(meipass / "cdumm" / "_vendor")
        out.append(meipass / "_vendor")
    return out


def _get_parser():
    """Load skillinfo_parser from the vendor dir, dev or frozen."""
    global _cached_module, _load_attempted
    if _load_attempted:
        return _cached_module
    _load_attempted = True
    for candidate in _candidate_dirs():
        if not candidate.exists():
            continue
        try:
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            import skillinfo_parser as _mod
            _cached_module = _mod
            logger.info("skillinfo_parser loaded from %s", candidate)
            return _cached_module
        except Exception as e:
            logger.debug(
                "skill parser load attempt at %s failed: %s", candidate, e)
            continue
    logger.warning("skill parser not loadable; skill list writer unavailable")
    return None


def build_skill_intent_changes(
    vanilla_body: bytes,
    vanilla_header: bytes,
    intents: "list[Format3Intent]",
) -> list[dict]:
    """Apply Format 3 intents to skill.pabgb and emit v2 changes.

    Needs both .pabgb body AND .pabgh header (the parser requires
    the index to walk records). Returns ``[]`` when no intents
    applied.

    Returns ``[skill.pabgb change]`` for same-size mutations, or
    ``[skill.pabgb change, skill.pabgh change]`` when serialization
    shifts entry offsets — the vendored parser's ``serialize_all``
    already returns both halves of the table; we just capture the
    regenerated index alongside the body so the game's hash→offset
    lookup keeps tracking the right entry starts (#105 pitonpp).
    """
    parser = _get_parser()
    if parser is None:
        return []
    try:
        entries = parser.parse_all(vanilla_header, vanilla_body)
    except Exception as e:
        logger.error("skill parse failed: %s", e, exc_info=True)
        return []

    by_key = {e["key"]: e for e in entries}
    applied = 0
    skipped_op = 0
    skipped_key = 0
    skipped_field = 0
    for intent in intents:
        if intent.key not in by_key:
            skipped_key += 1
            logger.debug(
                "skill writer: key %d not in table, skipping", intent.key)
            continue
        if intent.field not in SUPPORTED_FIELDS:
            skipped_field += 1
            logger.warning(
                "skill writer: field %r not supported (only %s); "
                "intent on key=%d dropped",
                intent.field, ", ".join(sorted(SUPPORTED_FIELDS)),
                intent.key)
            continue
        if intent.op != "set":
            skipped_op += 1
            logger.warning(
                "skill writer: op %r not supported (only 'set'); "
                "intent on key=%d field=%r dropped",
                intent.op, intent.key, intent.field)
            continue
        try:
            by_key[intent.key][intent.field] = intent.new
            applied += 1
        except Exception as e:
            logger.warning(
                "skill writer: applying intent on key=%d field=%r "
                "failed: %s", intent.key, intent.field, e)

    if applied == 0:
        skip_total = skipped_op + skipped_key + skipped_field
        if skip_total:
            logger.warning(
                "skill writer: 0 of %d intent(s) applied "
                "(%d non-'set' op, %d unknown key, %d unknown field). "
                "No change emitted.",
                skip_total, skipped_op, skipped_key, skipped_field)
        return []

    try:
        new_pabgh, new_pabgb = parser.serialize_all(entries)
    except Exception as e:
        logger.error("skill serialize failed: %s", e, exc_info=True)
        return []

    if new_pabgb == vanilla_body:
        return []

    skip_total = skipped_op + skipped_key + skipped_field
    if skip_total:
        skip_summary_parts = []
        if skipped_op:
            skip_summary_parts.append(f"{skipped_op} non-'set' op")
        if skipped_key:
            skip_summary_parts.append(f"{skipped_key} unknown key")
        if skipped_field:
            skip_summary_parts.append(f"{skipped_field} unknown field")
        skip_summary = ", ".join(skip_summary_parts)
        label = (
            f"skill Format 3 intents ({applied} applied, "
            f"{skip_total} skipped: {skip_summary})"
        )
    else:
        label = f"skill Format 3 intents ({applied} applied)"

    changes: list[dict] = [{
        "offset": 0,
        "original": vanilla_body.hex(),
        "patched": new_pabgb.hex(),
        "label": label,
        "_target_file": "skill.pabgb",
    }]

    # Regenerate the companion .pabgh when serialization shifted entry
    # offsets — vendored parser's serialize_all gives us the rebuilt
    # index for free, we just need to ship it alongside the .pabgb so
    # the game's hash→offset lookup doesn't read stale vanilla offsets
    # past the growth point (#105 pitonpp, mirroring the iteminfo fix).
    if (len(new_pabgb) != len(vanilla_body)
            and new_pabgh != vanilla_header):
        changes.append({
            "offset": 0,
            "original": vanilla_header.hex(),
            "patched": new_pabgh.hex(),
            "label": f"skill .pabgh rebuild ({applied} applied)",
            "_target_file": "skill.pabgh",
        })

    return changes
