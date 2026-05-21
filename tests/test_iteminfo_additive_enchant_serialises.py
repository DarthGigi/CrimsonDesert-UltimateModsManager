"""Hotfix regression test for GitHub #79 (UnLuckyLust, hhkbble).

Background: v3.2.16 added an "additive write" branch to
``build_iteminfo_intent_changes`` that, when ``_resolve_field_name``
returned None, fell through with ``target_field = intent.field`` if
the field name was in ``SUPPORTED_FIELDS``. The intent was a
mod that adds enchants to records like Axiom Bracelet which have
no enchants in vanilla.

The fix was a half-fix: the writer accepted the new key on the
parsed dict, but the native parser's serializer ``_write_item``
walks a fixed ``_ITEM_FIELDS`` schema that does NOT include
``enchant_data_list`` (the live binary's EnchantData layout has
not been reverse-engineered). The added key was silently dropped
at serialise time, ``new_bytes == vanilla_body`` was True, and
``build_iteminfo_intent_changes`` returned None, which the apply
pipeline reported to the user as "0 byte changes".

UnLuckyLust on GitHub #79 confirmed v3.2.16 still produced "0
byte changes" against a mod that adds buffs to Axiom Bracelet
(key=1001129).

Honest hotfix: drop ``enchant_data_list`` from ``SUPPORTED_FIELDS``
(adding it back is gated on real schema work) and have the writer
log a clear, user-actionable skip reason that names the
unsupported field, so the user knows why their intent was dropped
instead of being told "0 byte changes" with no context.

These tests pin that contract.
"""
from __future__ import annotations

import logging

from cdumm.engine.iteminfo_writer import (
    SUPPORTED_FIELDS,
    build_iteminfo_intent_changes,
)
from cdumm.engine.format3_handler import Format3Intent


def test_enchant_data_list_no_longer_in_supported_fields():
    """SUPPORTED_FIELDS must not claim a field the serializer
    cannot actually emit. ``enchant_data_list`` is absent from the
    native parser's ``_ITEM_FIELDS`` (live binary layout TBD), so
    listing it as supported produces silent zero-byte writes.

    Re-add this entry only when ``_ITEM_FIELDS`` gains a
    matching schema entry plus a paired
    ``_read_EnchantData`` / ``_write_EnchantData`` round-trip
    that survives the 6235-record vanilla walk.
    """
    assert "enchant_data_list" not in SUPPORTED_FIELDS


def test_enchant_data_list_intent_logs_clear_skip_reason(caplog):
    """When a mod targets ``enchant_data_list`` on iteminfo, the
    writer must log a skip message that names the field and
    explains it isn't writeable yet, so the user understands why
    the intent was dropped.
    """
    intent = Format3Intent(
        entry="Daeil_Band",
        key=1001129,
        field="enchant_data_list",
        op="set",
        new=[{
            "level": 0,
            "enchant_stat_data": {
                "max_stat_list": [],
                "regen_stat_list": [],
                "stat_list_static": [],
                "stat_list_static_level": [],
            },
            "buy_price_list": [],
            "equip_buffs": [{"buff": 1000099, "level": 10}],
        }],
    )

    # Drive the writer with vanilla bytes that the parser will
    # reject (or accept as a zero-record body); we only care that
    # the skip message lands when the writer reaches the
    # field-resolution step. Use empty bytes; the writer should
    # short-circuit cleanly without crashing.
    with caplog.at_level(logging.WARNING, logger="cdumm.engine.iteminfo_writer"):
        changes = build_iteminfo_intent_changes(b"", [intent])

    # Cannot produce a change for an unsupported field.
    assert changes == []

    # The skip message must explicitly name enchant_data_list AND
    # say it is not currently writeable, so the user sees an
    # actionable reason instead of a generic "field not in dict".
    msgs = [r.getMessage() for r in caplog.records]
    joined = "\n".join(msgs).lower()
    assert "enchant_data_list" in joined, (
        f"writer skip log should name enchant_data_list; got:\n"
        + "\n".join(msgs))
    assert (
        "not currently writeable" in joined
        or "not supported by the iteminfo serializer" in joined
        or "needs schema support" in joined), (
        f"writer skip log should explain WHY (not just 'unknown "
        f"field'); got:\n" + "\n".join(msgs))
