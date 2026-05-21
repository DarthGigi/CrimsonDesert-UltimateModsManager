"""DefaultSubItem must consume the 13-byte trailing block on populated records.

Bug 2026-05-08 (hhkbble's My_ItemBuffs_Mod on item 1001250):

Pre-fix parser stopped at the u32 value of default_sub_item and
misattributed the trailing 13 bytes to sharpness_data.p_prefix on
PW shape. That misattribution shifted cooltime / unk_post_cooltime_a /
unk_post_cooltime_b 13 bytes earlier on disk than where mod authors
target them via Format 3. Cooltime intents at the pre-fix offset
corrupted the trailing block (engine-validated bytes) and crashed
the game on launch.

Fix: _read_DefaultSubItem now reads u8 type_id + u32 value + i64 + u32
+ u8 (18 bytes total) when type_id < 14, and sharpness_data PW shape
no longer prepends a p_prefix. Byte-conservative: total bytes consumed
unchanged. Verified byte-perfect round-trip on all 6235 vanilla
records.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _vanilla_path():
    p = Path(
        r"C:/Users/faisa/AppData/Local/Temp/vanilla-iteminfo.pabgb"
    )
    if not p.exists():
        pytest.skip("vanilla iteminfo fixture not present")
    return p


def test_default_sub_item_populated_form_has_trailing_block():
    """When type_id < 14, default_sub_item must include unk_a/b/c."""
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes,
    )
    items = parse_iteminfo_from_bytes(_vanilla_path().read_bytes())
    for it in items:
        dsi = it.get("default_sub_item") or {}
        if dsi.get("type_id", 99) < 14:
            assert "unk_a" in dsi, f"key={it['key']} missing unk_a"
            assert "unk_b" in dsi, f"key={it['key']} missing unk_b"
            assert "unk_c" in dsi, f"key={it['key']} missing unk_c"
            return
    pytest.fail("no records with type_id < 14 found in vanilla")


def test_thief_gloves_cooltime_now_real():
    """Item 1001250 (thief gloves) has a real on-disk cooltime of
    1,800,000 (30-minute cooldown). Pre-fix parser reported 0."""
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes,
    )
    items = parse_iteminfo_from_bytes(_vanilla_path().read_bytes())
    thief = next((i for i in items if i["key"] == 1001250), None)
    if thief is None:
        pytest.skip("thief gloves (1001250) not in fixture")
    assert thief["cooltime"] == 1_800_000, (
        f"expected real cooltime 1_800_000 (30 min), got "
        f"{thief['cooltime']} — parser is still misaligned"
    )


def test_byte_perfect_roundtrip_on_full_vanilla():
    """Parse + serialize must reproduce vanilla bytes exactly."""
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes, serialize_iteminfo,
    )
    vanilla = _vanilla_path().read_bytes()
    items = parse_iteminfo_from_bytes(vanilla)
    rt = serialize_iteminfo(items)
    assert rt == vanilla, (
        f"round-trip diverged: vanilla={len(vanilla)}, "
        f"roundtrip={len(rt)}"
    )


def test_sharpness_data_no_longer_has_p_prefix():
    """PW shape removal: sharpness_data should always be shape='W'."""
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes,
    )
    items = parse_iteminfo_from_bytes(_vanilla_path().read_bytes())
    for it in items[:200]:
        sd = it.get("sharpness_data")
        if isinstance(sd, dict):
            assert sd.get("shape") == "W"
            assert sd.get("p_prefix") is None


def test_format3_cooltime_intent_writes_at_correct_offset():
    """End-to-end: a Format 3 intent setting cooltime on item 1001250
    must produce bytes matching the externally-known-good output."""
    import json
    from cdumm.engine.format3_handler import Format3Intent
    from cdumm.engine.iteminfo_writer import (
        build_iteminfo_intent_changes,
    )

    vanilla = _vanilla_path().read_bytes()
    intent = Format3Intent(
        entry="thief_gloves", key=1001250,
        field="cooltime", op="set", new=18000, old=None)
    # Need an enchant_data_list intent on a different key to force-batch
    # this intent through the iteminfo whole-table writer.
    intent2 = Format3Intent(
        entry="anything", key=2200,
        field="enchant_data_list", op="set", new=[], old=None)
    changes = build_iteminfo_intent_changes(vanilla, [intent, intent2])
    if not changes:
        pytest.fail("writer produced no change")
    # The first change is always the .pabgb body; a sibling .pabgh
    # may follow when serialization shifts entry offsets.
    pabgb_change = changes[0]
    assert pabgb_change.get("_target_file") == "iteminfo.pabgb"
    new_bytes = bytes.fromhex(pabgb_change["patched"])

    # The on-disk cooltime for item 1001250 sits at vanilla offset
    # 4166238 (record start 4165515 + record-relative 723). Verified
    # by independent black-box comparison against a known-good mod
    # (CrimsonGameMods packaging of hhkbble's My_ItemBuffs_Mod).
    import struct
    new_cooltime = struct.unpack_from("<q", new_bytes, 4166238)[0]
    assert new_cooltime == 18000, (
        f"cooltime intent landed at wrong byte offset; "
        f"value at 4166238 = {new_cooltime}, expected 18000"
    )
    # Vanilla bytes BEFORE the cooltime (which were corrupted by
    # pre-fix writes) should be unchanged.
    assert vanilla[4166225:4166238] == new_bytes[4166225:4166238]
