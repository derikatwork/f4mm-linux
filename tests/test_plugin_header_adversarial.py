"""Adversarial cases for plugin_header found in review."""

from __future__ import annotations

import pytest

from helpers import make_plugin

from bethmm.plugin_header import (
    WEIGHT_DLC_MASTER,
    WEIGHT_MAIN_MASTER,
    candidate_games,
    parse_plugin_header,
)

LIGHT = 0x200


def votes_of(data: bytes, name: str = "plugin") -> list[tuple[str, int]]:
    return [(v.game_id, v.weight) for v in candidate_games(parse_plugin_header(data), name)]


# --------------------------------------------------------------------------
# Light plugins and the Skyrim LE/SE split. Skyrim LE cannot load light
# plugins, so a plugin that is one (ESL flag or .esl extension) and needs
# Skyrim.esm is for SE whatever its form version says. LE plugins ported to
# SE and ESL-flagged in xEdit keep form version 43.


@pytest.mark.parametrize("masters", [["Skyrim.esm"], ["Skyrim.esm", "Update.esm", "Dawnguard.esm"]])
def test_esl_flagged_skyrim_plugin_with_le_form_version_is_se(masters):
    data = make_plugin(masters, hedr_version=1.7, form_version=43, flags=LIGHT | 0x1)
    assert votes_of(data, "Ported.esp") == [("skyrimse", WEIGHT_MAIN_MASTER)]


@pytest.mark.parametrize("name", ["Ported.esl", "PORTED.ESL", "Ported.Esl"])
def test_esl_file_with_le_form_version_is_se(name):
    data = make_plugin(["Skyrim.esm"], hedr_version=0.94, form_version=43)
    votes = candidate_games(parse_plugin_header(data), name)
    assert [(v.game_id, v.weight) for v in votes] == [("skyrimse", WEIGHT_MAIN_MASTER)]
    assert "light" in votes[0].reason


def test_light_plugin_with_unknown_form_version_is_se_only():
    data = make_plugin(["Skyrim.esm"], hedr_version=1.7, form_version=0, flags=LIGHT)
    assert votes_of(data) == [("skyrimse", WEIGHT_MAIN_MASTER)]


def test_light_skyrim_dlc_master_alone_is_se():
    data = make_plugin(["Dawnguard.esm"], hedr_version=0.94, form_version=43, flags=LIGHT)
    assert votes_of(data) == [("skyrimse", WEIGHT_DLC_MASTER)]


def test_regular_le_plugin_is_still_le():
    # Same plugin without the light flag and with an .esp name: unchanged.
    data = make_plugin(["Skyrim.esm", "Update.esm"], hedr_version=0.94, form_version=43)
    assert votes_of(data, "Old.esp") == [("skyrim", WEIGHT_MAIN_MASTER)]


@pytest.mark.parametrize(
    "masters, hedr, form, expected",
    [
        # Bit 0x200 means something else (or nothing) in these games; it must
        # never remove the vote of a master only one game ships.
        (["FalloutNV.esm"], 1.34, 15, "falloutnv"),
        (["Fallout3.esm"], 0.94, 15, "fallout3"),
        (["Fallout4.esm"], 1.0, 131, "fallout4"),
        (["Starfield.esm"], 0.96, 555, "starfield"),
    ],
)
def test_light_flag_never_drops_single_game_masters(masters, hedr, form, expected):
    data = make_plugin(masters, hedr_version=hedr, form_version=form, flags=LIGHT)
    assert votes_of(data, "x.esl") == [(expected, WEIGHT_MAIN_MASTER)]


def test_oblivion_plugin_with_stray_light_bit_stays_oblivion():
    data = make_plugin(["Oblivion.esm"], hedr_version=1.0, header_size=20, flags=LIGHT)
    assert votes_of(data) == [("oblivion", WEIGHT_MAIN_MASTER)]
