"""Packet-id constants pinned against the reference enum.

Oracle: gs2lib IEnums.h as used by GServer-v2 origin/beta4. The id/name
pairs below are fixed by the declaration order of Player.cpp
FOR_OUTPUT_PACKETS / IPacketHandler.h FOR_INPUT_PACKETS plus the send sites
cited per row.
"""

import pytest

from reborn_protocol.constants import PLI, PLO

PLO_ROWS = [
    # (id, name, reference send site / anchor)
    (26, "NPCSETSCRIPT", "NPC.cpp:917"),
    (60, "UNHANDLED60", "Player.cpp output table"),
    (81, "UNHANDLED81", "Player.cpp output table"),
    (83, "UNHANDLED83", "Player.cpp output table"),
    (104, "FILEDECRYPTKEY", "Player.cpp output table"),
    (109, "HTTPDOWNLOAD", "Player.cpp output table"),
    (124, "RC_UNKNOWN124", "Player.cpp output table"),
    (132, "WEAPONBYTECODE", "Weapon.cpp:214"),
    (133, "CLASSBYTECODE", "ScriptClass.cpp:77"),
    (140, "WEAPONBYTECODE2", "Weapon.cpp:213"),
    (168, "ONLINEMODE", "Player.cpp:763"),
    (181, "PLAYERNOUDP", "Player.cpp output table"),
    (190, "SERVERLISTCONNECTED", "PlayerClient.cpp:489, ServerList.cpp:368"),
    (193, "WEAPONHASH", "Weapon.cpp:199"),
    (194, "RESETCLIENT", "PlayerClient.cpp:492"),
]


@pytest.mark.parametrize("pid,name,_site", PLO_ROWS)
def test_plo_name_matches_id(pid, name, _site):
    assert PLO[name] == pid
    assert PLO(pid).name == name


def test_serverlist_connected_is_190_not_196():
    # The enum has no 196 (LOADGANI 195, LOADSCRIPT 197).
    assert PLO.SERVERLISTCONNECTED == 190
    assert PLO(196).name == "UNKNOWN196"


@pytest.mark.parametrize("pid,name", [(25, "TOWEAPON"), (162, "PONG")])
def test_pli_names(pid, name):
    assert PLI[name] == pid
    assert PLI(pid).name == name
