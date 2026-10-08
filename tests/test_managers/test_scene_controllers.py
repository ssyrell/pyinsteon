"""Test logical scenes backed by distinct controller/group pairs."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pubsub.core import Publisher
import pytest

from pyinsteon import pub
from pyinsteon.address import Address
from pyinsteon.aldb.aldb_record import ALDBRecord
from pyinsteon.constants import ALDBStatus, ResponseStatus
from pyinsteon.device_types.dimmable_lighting_control import (
    DimmableLightingControl_KeypadLinc_6,
    DimmableLightingControl_SwitchLinc01,
)
from pyinsteon.device_types.general_controller import GeneralController_MiniRemote_8
from pyinsteon.device_types.hub import Hub
from pyinsteon.managers import (
    scene_controller_manager as controllers,
    scene_manager as scenes,
)
from pyinsteon.managers.device_link_manager import DeviceLinkManager
from pyinsteon.managers.device_manager import DeviceManager

from tests.utils import async_case

MODEM = "111111"
KEYPAD = "222222"
REMOTE = "333333"
LIGHT = "444444"
OTHER = "555555"
LINKS = [{"address": LIGHT, "data1": 128, "data2": 28, "data3": 1}]
CONTROLLERS = [{"address": KEYPAD, "group": 3}, {"address": REMOTE, "group": 1}]


@pytest.fixture
def scene_devices(monkeypatch):
    """Build isolated devices with real ALDBs and successful transport writes."""
    publisher = Publisher()
    monkeypatch.setattr(pub, "_publisher", publisher)
    monkeypatch.setattr(pub, "_topicMgr", publisher.getTopicMgr())
    for method in ("subscribe", "unsubscribe", "unsubAll", "sendMessage"):
        monkeypatch.setattr(pub, method, getattr(publisher, method))
    controllers._scenes.clear()
    scenes._scene_names.clear()
    monkeypatch.setattr(scenes, "_scene_lock", asyncio.Lock())
    monkeypatch.setattr(controllers, "_state_lock", asyncio.Lock())

    def build():
        devices = DeviceManager()
        devices.modem = Hub(MODEM, 3, 51, 165)
        for device in (
            DimmableLightingControl_KeypadLinc_6(KEYPAD, 1, 9),
            GeneralController_MiniRemote_8(REMOTE, 0, 0x1B),
            DimmableLightingControl_SwitchLinc01(LIGHT, 1, 1),
            DimmableLightingControl_KeypadLinc_6(OTHER, 1, 9),
        ):
            devices._devices[device.address] = device
        for address in devices:
            devices[address].async_keep_awake = AsyncMock(
                return_value=ResponseStatus.SUCCESS
            )
            aldb = devices[address].aldb
            hwm = ALDBRecord(
                aldb.first_mem_addr,
                controller=False,
                group=0,
                target="000000",
                data1=0,
                data2=0,
                data3=0,
                in_use=False,
                high_water_mark=True,
            )
            aldb.load_saved_records(ALDBStatus.LOADED, {hwm.mem_addr: hwm})
            aldb._write_manager.async_write = AsyncMock(
                return_value=ResponseStatus.SUCCESS
            )
        monkeypatch.setattr(controllers, "devices", devices)
        monkeypatch.setattr(scenes, "devices", devices)
        return devices

    yield build
    controllers._scenes.clear()
    scenes._scene_names.clear()


def active(device, **criteria):
    """Return active records matching the requested criteria."""
    return list(device.aldb.find(in_use=True, **criteria))


@async_case
async def test_distinct_groups_and_idempotency(scene_devices, tmp_path):
    """A single logical scene uses each hardware button's own group."""
    devices = scene_devices()
    scene_num, result = await scenes.async_add_or_update_scene(
        -1, LINKS, "Evening", str(tmp_path), CONTROLLERS
    )
    assert (scene_num, result) == (20, ResponseStatus.SUCCESS)
    for address, group in ((MODEM, 20), (KEYPAD, 3), (REMOTE, 1)):
        records = active(
            devices[LIGHT], target=address, group=group, is_controller=False
        )
        assert len(records) == 1
        assert (records[0].data1, records[0].data2, records[0].data3) == (128, 28, 1)
        assert (
            len(active(devices[address], target=LIGHT, group=group, is_controller=True))
            == 1
        )
    assert set(await scenes.async_get_scenes(str(tmp_path))) == {20}
    scene = await scenes.async_get_scene(20, str(tmp_path))
    assert scene["controllers"] == CONTROLLERS
    assert len(scene["devices"][Address(LIGHT)]) == 1
    assert not scene["pending"]
    counts = {addr: len(devices[addr].aldb) for addr in devices}
    writes = {
        addr: devices[addr].aldb._write_manager.async_write.call_count
        for addr in devices
    }
    await scenes.async_add_or_update_scene(20, LINKS, work_dir=str(tmp_path))
    assert {addr: len(devices[addr].aldb) for addr in devices} == counts
    assert {
        addr: devices[addr].aldb._write_manager.async_write.call_count
        for addr in devices
    } == writes


@async_case
async def test_synchronized_edits_and_targeted_delete(scene_devices, tmp_path):
    """Update all associated groups without touching another controller's group 3."""
    devices = scene_devices()
    await scenes.async_add_or_update_scene(
        20, LINKS, "Evening", str(tmp_path), CONTROLLERS
    )
    devices[OTHER].aldb.add(group=3, target=LIGHT, controller=True)
    devices[LIGHT].aldb.add(group=3, target=OTHER, data1=99, data2=27, data3=1)
    await devices[OTHER].aldb.async_write()
    await devices[LIGHT].aldb.async_write()
    changed = [{"address": LIGHT, "data1": 42, "data2": 15, "data3": 1}]
    await scenes.async_add_or_update_scene(20, changed, work_dir=str(tmp_path))
    assert [rec.data1 for rec in active(devices[LIGHT], target=KEYPAD)] == [42]
    assert [rec.data2 for rec in active(devices[LIGHT], target=REMOTE)] == [15]
    assert [rec.data1 for rec in active(devices[LIGHT], target=OTHER)] == [99]
    assert await scenes.async_delete_scene(20, str(tmp_path)) == ResponseStatus.SUCCESS
    assert not active(devices[KEYPAD], target=LIGHT)
    assert not active(devices[REMOTE], target=LIGHT)
    assert [rec.data1 for rec in active(devices[LIGHT], target=OTHER)] == [99]
    assert len(active(devices[OTHER], target=LIGHT)) == 1
    assert await scenes.async_get_scenes(str(tmp_path)) == {}


@async_case
async def test_restart_and_controller_removal(scene_devices, tmp_path):
    """Controller associations survive restart and explicitly empty means app-only."""
    devices = scene_devices()
    await scenes.async_add_or_update_scene(
        20, LINKS, "Evening", str(tmp_path), CONTROLLERS
    )
    controllers._scenes.clear()
    scene = await scenes.async_get_scene(20, str(tmp_path))
    assert scene["controllers"] == CONTROLLERS
    await scenes.async_add_or_update_scene(
        20, LINKS, work_dir=str(tmp_path), controllers=[]
    )
    assert not active(devices[LIGHT], target=KEYPAD)
    assert not active(devices[LIGHT], target=REMOTE)
    assert len(active(devices[LIGHT], target=MODEM, group=20)) == 1
    assert (await scenes.async_get_scene(20, str(tmp_path)))["controllers"] == []


@async_case
async def test_default_reporting_links_and_no_self_links(scene_devices, tmp_path):
    """Preserve controller reporting and don't write hardware self-links."""
    devices = scene_devices()
    devices[KEYPAD].aldb.add(group=3, target=MODEM, controller=True)
    devices.modem.aldb.add(group=3, target=KEYPAD, controller=False)
    await devices[KEYPAD].aldb.async_write()
    await devices.modem.aldb.async_write()
    links = [*LINKS, {"address": KEYPAD, "data1": 255, "data2": 28, "data3": 3}]
    await scenes.async_add_or_update_scene(
        20, links, "Evening", str(tmp_path), CONTROLLERS
    )
    assert not active(devices[KEYPAD], target=KEYPAD)
    assert len(active(devices[KEYPAD], target=MODEM, group=3, is_controller=True)) == 1
    assert (
        len(active(devices[KEYPAD], target=MODEM, group=20, is_controller=False)) == 1
    )
    await scenes.async_delete_scene(20, str(tmp_path))
    assert len(active(devices[KEYPAD], target=MODEM, group=3, is_controller=True)) == 1
    assert len(active(devices.modem, target=KEYPAD, group=3, is_controller=False)) == 1


@async_case
async def test_conflicting_managed_controller(scene_devices, tmp_path):
    """A button cannot own two distinct logical scenes."""
    scene_devices()
    await scenes.async_add_or_update_scene(
        20, LINKS, "Evening", str(tmp_path), CONTROLLERS
    )
    with pytest.raises(controllers.SceneControllerError, match="belongs to scene"):
        await scenes.async_add_or_update_scene(
            21, LINKS, work_dir=str(tmp_path), controllers=CONTROLLERS
        )
    assert controllers.scene_numbers() == {20}


@pytest.mark.parametrize("side", ["controller", "responder"])
@async_case
async def test_unmanaged_conflict(scene_devices, tmp_path, side):
    """Reject unmanaged hardware groups even when only one side of the link exists."""
    devices = scene_devices()
    if side == "controller":
        devices[KEYPAD].aldb.add(group=3, target=LIGHT, controller=True)
        await devices[KEYPAD].aldb.async_write()
    else:
        devices[LIGHT].aldb.add(group=3, target=KEYPAD, data3=1)
        await devices[LIGHT].aldb.async_write()
    with pytest.raises(
        controllers.SceneControllerError, match="already controls|existing responder"
    ):
        await scenes.async_add_or_update_scene(
            20, LINKS, work_dir=str(tmp_path), controllers=CONTROLLERS
        )
    assert not controllers.scene_numbers()


@pytest.mark.parametrize(
    "selection",
    [
        [{"address": KEYPAD, "group": 2}],
        [{"address": MODEM, "group": 20}],
        [{"address": "999999", "group": 1}],
        [CONTROLLERS[0], CONTROLLERS[0]],
    ],
)
@async_case
async def test_invalid_controllers_before_mutation(scene_devices, tmp_path, selection):
    """Reject unknown devices, unavailable buttons, modem selectors and duplicates."""
    devices = scene_devices()
    with pytest.raises(controllers.SceneControllerError):
        await scenes.async_add_or_update_scene(
            20, LINKS, work_dir=str(tmp_path), controllers=selection
        )
    assert all(not devices[addr].aldb.pending_changes for addr in devices)


@async_case
async def test_incomplete_and_unrelated_pending_databases(scene_devices, tmp_path):
    """Never silently write partial databases or pending edits owned by another editor."""
    devices = scene_devices()
    devices[LIGHT].aldb._status = ALDBStatus.PARTIAL
    with pytest.raises(
        controllers.SceneControllerError, match="complete All-Link Database"
    ):
        await scenes.async_add_or_update_scene(
            20, LINKS, work_dir=str(tmp_path), controllers=CONTROLLERS
        )
    devices[LIGHT].aldb._status = ALDBStatus.LOADED
    devices[LIGHT].aldb.add(group=99, target=OTHER, data3=1)
    with pytest.raises(controllers.SceneControllerError, match="unrelated pending"):
        await scenes.async_add_or_update_scene(
            20, LINKS, work_dir=str(tmp_path), controllers=CONTROLLERS
        )
    assert len(devices[LIGHT].aldb.pending_changes) == 1


@async_case
async def test_failed_creation_retry_after_restart(scene_devices, tmp_path):
    """Reserve the scene ID and preserve desired settings when a remote is asleep."""
    devices = scene_devices()
    writer = devices[REMOTE].aldb._write_manager.async_write
    writer.return_value = ResponseStatus.FAILURE
    assert await scenes.async_add_or_update_scene(
        -1, LINKS, "Evening", str(tmp_path), CONTROLLERS
    ) == (20, ResponseStatus.FAILURE)
    scene = await scenes.async_get_scene(20, str(tmp_path))
    assert scene["pending"]
    assert scene["name"] == "Evening"
    assert scene["devices"][Address(LIGHT)][0].data1 == 128
    assert 20 in await scenes.async_get_scenes(str(tmp_path))
    devices[REMOTE].aldb.clear_pending()
    controllers._scenes.clear()
    writer.return_value = ResponseStatus.SUCCESS
    assert await scenes.async_add_or_update_scene(
        20, LINKS, work_dir=str(tmp_path)
    ) == (20, ResponseStatus.SUCCESS)
    scene = await scenes.async_get_scene(20, str(tmp_path))
    assert not scene["pending"]
    assert scene["name"] == "Evening"
    assert len(active(devices[LIGHT], target=REMOTE)) == 1


@async_case
async def test_failed_removal_retains_ownership(scene_devices, tmp_path):
    """A failed deletion can be retried even after responders have been removed."""
    devices = scene_devices()
    await scenes.async_add_or_update_scene(
        20, LINKS, "Evening", str(tmp_path), CONTROLLERS
    )
    writer = devices[KEYPAD].aldb._write_manager.async_write
    writer.return_value = ResponseStatus.FAILURE
    assert await scenes.async_delete_scene(20, str(tmp_path)) == ResponseStatus.FAILURE
    assert (await scenes.async_get_scene(20, str(tmp_path)))["pending"]
    devices[KEYPAD].aldb.clear_pending()
    controllers._scenes.clear()
    writer.return_value = ResponseStatus.SUCCESS
    assert await scenes.async_delete_scene(20, str(tmp_path)) == ResponseStatus.SUCCESS
    assert not active(devices[KEYPAD], target=LIGHT)
    assert await scenes.async_get_scenes(str(tmp_path)) == {}


@async_case
async def test_helpers_update_both_underlying_groups(scene_devices, tmp_path):
    """Single-device scene helpers synchronize hardware and modem responders."""
    devices = scene_devices()
    await scenes.async_add_or_update_scene(
        20, LINKS, "Evening", str(tmp_path), CONTROLLERS
    )
    await scenes.async_add_device_to_scene(OTHER, 20, data1=70, work_dir=str(tmp_path))
    assert [rec.data1 for rec in active(devices[OTHER], target=KEYPAD, group=3)] == [70]
    await scenes.async_remove_device_from_scene(OTHER, 20, work_dir=str(tmp_path))
    assert not active(devices[OTHER], target=KEYPAD, group=3)
    assert not active(devices[OTHER], target=MODEM, group=20)


@async_case
async def test_corrupt_metadata_is_not_overwritten(scene_devices, tmp_path):
    """Refuse to mutate hardware when ownership metadata is unreadable."""
    devices = scene_devices()
    filename = tmp_path / controllers.SCENE_CONTROLLERS_FILE
    filename.write_text('{"version": 999, "scenes": {}}')
    with pytest.raises(controllers.SceneControllerError, match="Cannot load"):
        await scenes.async_add_or_update_scene(
            20, LINKS, work_dir=str(tmp_path), controllers=CONTROLLERS
        )
    assert json.loads(filename.read_text())["version"] == 999
    assert all(not devices[addr].aldb.pending_changes for addr in devices)


@async_case
async def test_simultaneous_creation_allocates_distinct_groups(scene_devices, tmp_path):
    """Concurrent creates serialize allocation and ownership checks."""
    scene_devices()
    results = await asyncio.gather(
        scenes.async_add_or_update_scene(
            -1, LINKS, "First", str(tmp_path), [CONTROLLERS[0]]
        ),
        scenes.async_add_or_update_scene(
            -1, LINKS, "Second", str(tmp_path), [CONTROLLERS[1]]
        ),
    )
    assert results == [(20, ResponseStatus.SUCCESS), (21, ResponseStatus.SUCCESS)]


@async_case
async def test_asleep_remote_is_not_reported_as_success(scene_devices, tmp_path):
    """A queued battery write is not a completed scene save."""
    devices = scene_devices()
    devices[REMOTE].async_keep_awake.return_value = ResponseStatus.FAILURE
    assert await scenes.async_add_or_update_scene(
        20, LINKS, "Evening", str(tmp_path), CONTROLLERS
    ) == (20, ResponseStatus.FAILURE)
    devices[REMOTE].aldb._write_manager.async_write.assert_not_called()
    assert (await scenes.async_get_scene(20, str(tmp_path)))["pending"]
    devices[REMOTE].async_keep_awake.return_value = ResponseStatus.SUCCESS
    assert await scenes.async_add_or_update_scene(
        20, LINKS, work_dir=str(tmp_path)
    ) == (20, ResponseStatus.SUCCESS)


@async_case
async def test_read_during_write_does_not_replace_journal(scene_devices, tmp_path):
    """Polling the editor while saving cannot restore stale pending metadata."""
    devices = scene_devices()
    reads = []

    async def write(record, force=False):
        reads.append(asyncio.create_task(scenes.async_get_scene(20, str(tmp_path))))
        return ResponseStatus.SUCCESS

    devices[LIGHT].aldb._write_manager.async_write.side_effect = write
    await scenes.async_add_or_update_scene(
        20, LINKS, "Evening", str(tmp_path), CONTROLLERS
    )
    assert all(not scene["pending"] for scene in await asyncio.gather(*reads))
    assert not (await scenes.async_get_scene(20, str(tmp_path)))["pending"]


@pytest.mark.parametrize("output", [0, 1])
@async_case
async def test_native_activation_updates_only_its_group(
    scene_devices, tmp_path, output
):
    """Hardware button broadcasts update Home Assistant using the right responder settings."""
    devices = scene_devices()
    await scenes.async_add_or_update_scene(
        20, [{**LINKS[0], "data3": output}], "First", str(tmp_path), [CONTROLLERS[0]]
    )
    other_links = [{"address": LIGHT, "data1": 200, "data2": 15, "data3": 1}]
    await scenes.async_add_or_update_scene(
        21, other_links, "Second", str(tmp_path), [{"address": KEYPAD, "group": 4}]
    )
    manager = DeviceLinkManager(devices)
    responders = manager.get_responders(Address(KEYPAD), 3)
    assert [link.data1 for link in responders[Address(LIGHT)]] == [128]
    devices[LIGHT].async_status = AsyncMock(return_value=ResponseStatus.SUCCESS)
    await manager._async_check_responders(
        topic=SimpleNamespace(name=f"{KEYPAD}.3.on.all_link_broadcast")
    )
    assert devices[LIGHT].groups[1].value == 128
    devices[LIGHT].async_status.assert_awaited_once()


@async_case
async def test_capacity_failure_before_mutation(scene_devices, tmp_path):
    """Refuse a scene which cannot leave space for a high-water-mark record."""
    devices = scene_devices()
    hwm = ALDBRecord(
        7,
        controller=False,
        group=0,
        target="000000",
        data1=0,
        data2=0,
        data3=0,
        in_use=False,
        high_water_mark=True,
    )
    devices[LIGHT].aldb.load_saved_records(
        ALDBStatus.LOADED, {7: hwm}, first_mem_addr=7
    )
    with pytest.raises(controllers.SceneControllerError, match="is full"):
        await scenes.async_add_or_update_scene(
            20, LINKS, work_dir=str(tmp_path), controllers=CONTROLLERS
        )
    assert all(not devices[addr].aldb.pending_changes for addr in devices)


@async_case
async def test_multiple_responder_outputs(scene_devices, tmp_path):
    """Different outputs share a controller record but have distinct responder records."""
    devices = scene_devices()
    links = [
        {"address": OTHER, "data1": 255, "data2": 28, "data3": 3},
        {"address": OTHER, "data1": 0, "data2": 0, "data3": 4},
    ]
    await scenes.async_add_or_update_scene(
        20, links, "Keypad indicators", str(tmp_path), CONTROLLERS
    )
    assert [
        (rec.data3, rec.data1) for rec in active(devices[OTHER], target=KEYPAD, group=3)
    ] == [(3, 255), (4, 0)]
    assert len(active(devices[KEYPAD], target=OTHER, group=3, is_controller=True)) == 1
    await scenes.async_add_or_update_scene(20, links[:1], work_dir=str(tmp_path))
    assert [rec.data3 for rec in active(devices[OTHER], target=KEYPAD, group=3)] == [3]
    assert len(active(devices[KEYPAD], target=OTHER, group=3, is_controller=True)) == 1


@async_case
async def test_default_name_uses_allocated_modem_group(scene_devices, tmp_path):
    """A nameless device scene uses the allocated scene number, not the sentinel."""
    scene_devices()
    assert await scenes.async_add_or_update_scene(
        -1, LINKS, work_dir=str(tmp_path), controllers=CONTROLLERS
    ) == (20, ResponseStatus.SUCCESS)
    assert (await scenes.async_get_scene(20, str(tmp_path)))[
        "name"
    ] == "Insteon Scene 20"
