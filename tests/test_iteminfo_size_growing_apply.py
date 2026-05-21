"""Regression tests for size-growing whole-buffer Format 3 apply (#105).

The Format 3 whole-table writer emits a single change of shape
``{offset: 0, original: vanilla.hex(), patched: new.hex()}``. When a
mod's intents add bytes (e.g. appending entries to a list-of-dict
field), ``len(patched) > len(original)`` and the apply path's bounds
check used to reject the change outright with no in-game effect. The
companion ``.pabgh`` also needs regenerating when serialization
shifts entry offsets, otherwise the game's hash→offset lookup reads
stale bytes past the growth point.

The three tests here cover:

  1. ``_apply_byte_patches`` accepts size-growing replaces when the
     change carries an ``original`` field whose length fits the
     buffer.
  2. Without an ``original`` field, the same shape still gets
     rejected (the new bound falls back to len(patched_bytes) which
     is the old behaviour — no regression for v2 mods that ship
     absolute offsets past EOF).
  3. ``build_iteminfo_intent_changes`` emits a sibling
     ``iteminfo.pabgh`` change with shifted offsets when an intent
     grows an item, mirroring multichangeinfo_writer's pattern.

All three are runnable without a live game fixture.
"""
from __future__ import annotations

import logging
import struct

from cdumm.engine.json_patch_handler import _apply_byte_patches


def test_bounds_check_passes_for_size_growing_replace():
    """offset=0 + original=full vanilla + patched=vanilla+'X' is the
    shape the Format 3 whole-table writer emits when intents grow
    the table. The bounds check at json_patch_handler.py:1078 used
    to reject this because len(patched) > len(data); the new check
    bounds against len(original) when present, letting the bytearray
    slice assignment grow the buffer naturally."""
    vanilla = b"VANILLA-FIXTURE"
    patched = vanilla + b"X"
    change = {
        "offset": 0,
        "original": vanilla.hex(),
        "patched": patched.hex(),
    }
    data = bytearray(vanilla)
    applied, mismatched, relocated = _apply_byte_patches(
        data, [change], vanilla_data=vanilla)

    assert applied == 1, f"expected applied=1, got {applied}"
    assert mismatched == 0
    assert relocated == 0
    assert bytes(data) == patched
    assert len(data) == len(vanilla) + 1


def test_bounds_check_still_rejects_oversize_without_original(caplog):
    """Without an ``original`` field, the bounds check falls back to
    len(patched_bytes) — the historical behaviour. A v2 mod that
    ships absolute offsets pointing past EOF must still be rejected
    cleanly so the all-or-nothing taint filter trips."""
    vanilla = b"SHORT"
    too_big = vanilla + b"OVERFLOW"
    change = {
        "offset": 0,
        # NO 'original' field
        "patched": too_big.hex(),
    }
    data = bytearray(vanilla)

    with caplog.at_level(
            logging.WARNING, logger="cdumm.engine.json_patch_handler"):
        applied, mismatched, relocated = _apply_byte_patches(
            data, [change], vanilla_data=vanilla)

    assert applied == 0
    assert bytes(data) == vanilla  # untouched
    # The bounds-check warning must surface so users see why their
    # change was dropped; the silent skip from before #105 set
    # pitonpp's debugging back days.
    bounds_warns = [
        r for r in caplog.records
        if "exceeds file size" in r.getMessage()
    ]
    assert bounds_warns, (
        f"expected bounds-check warning, got: "
        f"{[r.getMessage() for r in caplog.records]}")


def test_rebuild_iteminfo_pabgh_substitutes_offsets():
    """``_rebuild_iteminfo_pabgh`` keeps every vanilla key, in
    vanilla order, but rewrites each entry's offset to the value
    supplied by ``serialize_iteminfo_with_offsets``. Sanity check
    that doesn't need a live iteminfo fixture."""
    from cdumm.engine.iteminfo_writer import _rebuild_iteminfo_pabgh

    vanilla_offsets = [(101, 0), (202, 100), (303, 250)]
    vanilla_header = bytearray(struct.pack("<H", len(vanilla_offsets)))
    for key, off in vanilla_offsets:
        vanilla_header += struct.pack("<II", key, off)
    vanilla_header = bytes(vanilla_header)

    # Mod grew the second item: third item shifts by +50.
    new_offsets = [(101, 0), (202, 100), (303, 300)]

    new_header = _rebuild_iteminfo_pabgh(vanilla_header, new_offsets)
    assert new_header is not None
    count = struct.unpack_from("<H", new_header, 0)[0]
    assert count == 3

    rebuilt: list[tuple[int, int]] = []
    for i in range(count):
        pos = 2 + i * 8
        k, off = struct.unpack_from("<II", new_header, pos)
        rebuilt.append((k, off))
    assert rebuilt == new_offsets


def test_rebuild_iteminfo_pabgh_returns_none_when_key_missing():
    """If serialization dropped a key the vanilla header expected,
    abort the rebuild — better to ship vanilla .pabgh than a partial
    index that misses entries."""
    from cdumm.engine.iteminfo_writer import _rebuild_iteminfo_pabgh

    vanilla_offsets = [(101, 0), (202, 100)]
    vanilla_header = bytearray(struct.pack("<H", len(vanilla_offsets)))
    for key, off in vanilla_offsets:
        vanilla_header += struct.pack("<II", key, off)
    vanilla_header = bytes(vanilla_header)

    # Key 202 is missing from the serialised offsets.
    new_offsets = [(101, 0)]

    assert _rebuild_iteminfo_pabgh(vanilla_header, new_offsets) is None


def test_iteminfo_writer_refuses_grown_pabgb_when_pabgh_rebuild_fails(caplog):
    """Fail-closed contract: when serialization grew the body but the
    .pabgh rebuild can't produce a complete index, refuse to ship the
    .pabgb at all. Without this guard the apply would write the grown
    .pabgb but keep the vanilla .pabgh — items past the growth point
    would resolve to wrong bytes at runtime and the game would crash
    on load (pitonpp My_ItemBuffs_Mod, 2026-05-21).

    Drives the contract through the public writer entry point instead
    of the helper so it covers the call-site decision, not just
    _rebuild_iteminfo_pabgh's None return.
    """
    import json
    import logging
    from pathlib import Path

    import pytest

    from cdumm.engine.iteminfo_writer import (
        build_iteminfo_intent_changes,
    )
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes,
        serialize_iteminfo_with_offsets,
    )
    from cdumm.engine.format3_handler import Format3Intent

    # Need a real vanilla body — the same live-fixture gate as the
    # other size-growing test.
    live = Path(
        "C:/Users/faisa/AppData/Local/Temp/iteminfo_postpatch.pabgb")
    if not live.exists():
        pytest.skip("iteminfo_postpatch.pabgb fixture not present")

    body = live.read_bytes()
    items = parse_iteminfo_from_bytes(body)
    _, real_offsets = serialize_iteminfo_with_offsets(items)

    # Build a synthetic vanilla_header that references a SHADOW KEY
    # the parser doesn't expose. Mimics the production failure mode:
    # the native parser's salvage path dropped a record, so a key
    # present in vanilla.pabgh has no corresponding entry in the
    # parsed items list.
    shadow_key = max(k for k, _ in real_offsets) + 9_999_999
    fake_header = bytearray(
        struct.pack("<H", len(real_offsets) + 1))
    for key, off in real_offsets:
        fake_header += struct.pack("<II", key, off)
    fake_header += struct.pack("<II", shadow_key, len(body))
    fake_header = bytes(fake_header)

    # Pick any intent that actually grows the body so the writer
    # reaches the rebuild branch.
    target = next(
        it for it in items
        if it.get("equip_passive_skill_list") is not None
    )
    new_list = list(target.get("equip_passive_skill_list") or [])
    new_list.append({"skill_key": 9_999_999, "level": 1})
    intent = Format3Intent(
        entry=target.get("string_key", str(target["key"])),
        key=target["key"],
        field="equip_passive_skill_list",
        op="set",
        new=new_list,
    )

    with caplog.at_level(
            logging.ERROR, logger="cdumm.engine.iteminfo_writer"):
        changes = build_iteminfo_intent_changes(
            body, [intent], vanilla_header=fake_header)

    # If this intent didn't actually grow the buffer on this game
    # version, the writer takes the same-size branch and returns
    # `[pabgb_change]` with no .pabgh — that's the correct path
    # for that case; skip the assertion.
    if changes and len(bytes.fromhex(changes[0]["patched"])) == len(body):
        pytest.skip("intent didn't grow the buffer; can't test fail-closed")

    assert changes == [], (
        f"expected [] when .pabgh rebuild fails on a grown .pabgb, "
        f"got {len(changes)} change(s)")
    assert any(
        "Refusing to apply" in r.getMessage()
        for r in caplog.records), (
        "expected an ERROR log explaining the refusal so the user "
        "can act on the failure")


def test_iteminfo_writer_emits_pabgh_change_on_growth():
    """When an intent grows an item past the vanilla's serialized
    size, ``build_iteminfo_intent_changes`` must emit a sibling
    ``iteminfo.pabgh`` change with offsets re-anchored to the new
    .pabgb layout. Without that, the game's hash→offset lookup keeps
    pointing at vanilla offsets and items past the growth point come
    back as adjacent items' bytes."""
    from cdumm.engine.iteminfo_writer import (
        build_iteminfo_intent_changes,
    )
    from cdumm.engine.iteminfo_native_parser import (
        parse_iteminfo_from_bytes,
    )

    # Round-trip a tiny synthetic two-item iteminfo from the live
    # vanilla extract used by the existing E2E tests. If the live
    # fixture is absent we skip — the writer paths exercised here
    # all need a parseable real iteminfo body, which can't be
    # synthesised without committing 5 MB of game data.
    from pathlib import Path
    import pytest
    live = Path(
        "C:/Users/faisa/AppData/Local/Temp/iteminfo_postpatch.pabgb")
    if not live.exists():
        pytest.skip("iteminfo_postpatch.pabgb fixture not present")

    body = live.read_bytes()
    items = parse_iteminfo_from_bytes(body)

    # Build a matching vanilla .pabgh: u16 count + count*(u32 key,
    # u32 offset). Compute offsets by serialising each item in
    # isolation — same byte layout the writer will produce on the
    # unmodified items.
    from cdumm.engine.iteminfo_native_parser import (
        serialize_iteminfo_with_offsets,
    )
    _, vanilla_offsets = serialize_iteminfo_with_offsets(items)
    vanilla_header = bytearray(struct.pack("<H", len(vanilla_offsets)))
    for key, off in vanilla_offsets:
        vanilla_header += struct.pack("<II", key, off)
    vanilla_header = bytes(vanilla_header)

    # Pick an item that has equip_passive_skill_list — adding an
    # element grows the record by at least the element's marshaled
    # size, which shifts every subsequent item.
    from cdumm.engine.format3_handler import Format3Intent
    target = next(
        it for it in items
        if it.get("equip_passive_skill_list") is not None
    )
    target_key = target["key"]
    new_passives = list(target.get("equip_passive_skill_list") or [])
    new_passives.append({
        "skill_key": 9_999_999,
        "level": 1,
    })

    intent = Format3Intent(
        entry=target.get("string_key", str(target_key)),
        key=target_key,
        field="equip_passive_skill_list",
        op="set",
        new=new_passives,
    )

    changes = build_iteminfo_intent_changes(
        body, [intent], vanilla_header=vanilla_header)
    assert changes, "expected at least one change"

    pabgb = next(c for c in changes
                 if c.get("_target_file") == "iteminfo.pabgb")
    new_body = bytes.fromhex(pabgb["patched"])

    if len(new_body) == len(body):
        # Append didn't grow the buffer (the writer doesn't expose
        # equip_passive_skill_list growth on every game version). Skip
        # the .pabgh shape assertion — the writer's contract is only
        # to emit .pabgh when serialization shifted bytes.
        pytest.skip(
            "writer produced same-size output for this intent; "
            ".pabgh emission only fires on growth")

    pabgh = next(
        (c for c in changes
         if c.get("_target_file") == "iteminfo.pabgh"),
        None)
    assert pabgh is not None, (
        "expected sibling iteminfo.pabgh change for size-growing "
        "apply; without it items past the growth point would resolve "
        "to wrong bytes at runtime")

    new_header = bytes.fromhex(pabgh["patched"])
    new_count = struct.unpack_from("<H", new_header, 0)[0]
    assert new_count == struct.unpack_from("<H", vanilla_header, 0)[0]

    # Every key in the vanilla header still resolves to a new offset
    # in the rebuilt header — same key set, just shifted offsets.
    new_by_key: dict[int, int] = {}
    for i in range(new_count):
        pos = 2 + i * 8
        k, off = struct.unpack_from("<II", new_header, pos)
        new_by_key[k] = off
    for k, _van_off in vanilla_offsets:
        assert k in new_by_key, f"key {k} dropped from .pabgh rebuild"
