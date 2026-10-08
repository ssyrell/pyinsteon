"""Persist and reconcile device-controlled scenes."""

import asyncio
import json
import logging
from os import path

import aiofiles
import aiofiles.os
import voluptuous as vol

from .. import devices
from ..address import Address
from ..aldb.no_aldb import NoALDB
from ..constants import ResponseStatus

SCENE_CONTROLLERS_FILE = "insteon_scene_controllers.json"
ControllerSchema = vol.Schema(
    [
        {
            vol.Required("address"): str,
            vol.Required("group"): vol.All(int, vol.Range(min=1, max=255)),
        }
    ]
)
_LOGGER = logging.getLogger(__name__)
_scenes = {}
_state_lock = asyncio.Lock()
_byte = vol.All(int, vol.Range(min=0, max=255))
_link = {
    vol.Required("address"): str,
    vol.Required("data1"): _byte,
    vol.Required("data2"): _byte,
    vol.Required("data3"): _byte,
}
_metadata_schema = vol.Schema(
    {
        vol.Required("controllers"): ControllerSchema,
        vol.Required("links"): [_link],
        vol.Required("records"): [
            {
                **_link,
                vol.Required("controller"): bool,
                vol.Required("group"): vol.All(int, vol.Range(min=1, max=255)),
                vol.Required("target"): str,
            }
        ],
        vol.Required("name"): str,
        vol.Required("pending"): bool,
    }
)


class SceneControllerError(ValueError):
    """A scene cannot be safely written."""


def controller_groups(device):
    """Return groups which the device can transmit as a controller."""
    if isinstance(device.aldb, NoALDB):
        return []
    return sorted(
        {
            link.group
            for link in device.default_links
            if link.is_controller and link.group
        }
    )


async def async_load(work_dir):
    """Load associations and any interrupted write journal."""
    async with _state_lock:
        await _async_load(work_dir)


async def _async_load(work_dir):
    if not work_dir:
        return
    filename = path.join(work_dir, SCENE_CONTROLLERS_FILE)
    try:
        async with aiofiles.open(filename) as file:
            contents = await file.read()
    except FileNotFoundError:
        _scenes.clear()
        return
    try:
        data = json.loads(contents)
        if data["version"] != 1 or not isinstance(data["scenes"], dict):
            raise ValueError("Unsupported scene controller metadata")
        loaded = {}
        for group, scene in data["scenes"].items():
            _metadata_schema(scene)
            for record in scene["records"]:
                Address(record["address"])
                Address(record["target"])
            for link in scene["links"]:
                Address(link["address"])
            for controller in scene["controllers"]:
                Address(controller["address"])
            if not 1 <= int(group) <= 255:
                raise ValueError("Invalid scene group")
            loaded[int(group)] = scene
    except (ValueError, KeyError, TypeError, vol.Error) as exc:
        raise SceneControllerError(f"Cannot load {filename}: {exc}") from exc
    _scenes.clear()
    _scenes.update(loaded)


async def _async_save(work_dir):
    if not work_dir:
        raise SceneControllerError(
            "A working directory is required for device-controlled scenes"
        )
    filename = path.join(work_dir, SCENE_CONTROLLERS_FILE)
    temporary = filename + ".tmp"
    async with aiofiles.open(temporary, "w") as file:
        await file.write(json.dumps({"version": 1, "scenes": _scenes}, indent=2))
        await file.flush()
    await aiofiles.os.replace(temporary, filename)


def scene_metadata(scene_num):
    """Return the persisted state of a logical scene."""
    return _scenes.get(scene_num)


def scene_numbers():
    """Return managed modem scene groups, including interrupted creations."""
    return set(_scenes)


def _device(address):
    device = devices[address]
    if device is None:
        raise SceneControllerError(f"Insteon device {address} was not found")
    return device


def _key(record):
    return (
        Address(record["address"]).id,
        record["controller"],
        record["group"],
        Address(record["target"]).id,
        None if record["controller"] else record["data3"],
    )


def _record(address, controller, group, target, data1, data2, data3):
    return {
        "address": Address(address).id,
        "controller": controller,
        "group": group,
        "target": Address(target).id,
        "data1": data1,
        "data2": data2,
        "data3": data3,
    }


def _records(scene_num, links, controllers):
    records = {}
    sources = [{"address": devices.modem.address.id, "group": scene_num}, *controllers]
    for source in sources:
        for link in links:
            responder = _device(link["address"])
            if responder.address == Address(source["address"]):
                # A physical button controls its own load using its local settings.
                continue
            responder_record = _record(
                responder.address,
                False,
                source["group"],
                source["address"],
                link["data1"],
                link["data2"],
                link["data3"],
            )
            controller_record = _record(
                source["address"],
                True,
                source["group"],
                responder.address,
                int(responder.cat),
                responder.subcat,
                responder.firmware or 0,
            )
            records[_key(responder_record)] = responder_record
            records[_key(controller_record)] = controller_record
    return records


def _validate_controllers(scene_num, controllers, previous):
    try:
        ControllerSchema(controllers)
    except vol.Error as exc:
        raise SceneControllerError(f"Invalid scene controllers: {exc}") from exc
    seen = set()
    previous_pairs = {
        (Address(item["address"]).id, item["group"])
        for item in previous.get("controllers", [])
    }
    # Records retained after a failed controller removal still own that group.
    previous_pairs.update(
        (record["address"], record["group"])
        for record in previous.get("records", [])
        if record["controller"] and Address(record["address"]) != devices.modem.address
    )
    normalized = []
    for item in controllers:
        device = _device(item["address"])
        if not device.aldb.is_loaded:
            raise SceneControllerError(
                f"Read the complete All-Link Database for {device.address} before saving"
            )
        pair = (device.address.id, item["group"])
        if pair in seen:
            raise SceneControllerError(
                "A controller button was selected more than once"
            )
        seen.add(pair)
        if device == devices.modem or item["group"] not in controller_groups(device):
            raise SceneControllerError(
                f"{device.address} cannot control group {item['group']}"
            )
        for other_num, other in _scenes.items():
            if other_num == scene_num:
                continue
            other_pairs = {
                (Address(ctrl["address"]).id, ctrl["group"])
                for ctrl in other["controllers"]
            }
            other_pairs.update(
                (rec["address"], rec["group"])
                for rec in other["records"]
                if rec["controller"]
            )
            if pair in other_pairs:
                raise SceneControllerError(
                    f"{device.address} group {item['group']} belongs to scene {other_num}"
                )
        if pair not in previous_pairs:
            if any(
                rec.target != devices.modem.address
                for rec in device.aldb.find(
                    group=item["group"], is_controller=True, in_use=True
                )
            ):
                raise SceneControllerError(
                    f"{device.address} group {item['group']} already controls other devices"
                )
            for address in devices:
                if address == devices.modem.address:
                    continue
                if any(
                    devices[address].aldb.find(
                        target=device.address,
                        group=item["group"],
                        is_controller=False,
                        in_use=True,
                    )
                ):
                    raise SceneControllerError(
                        f"{device.address} group {item['group']} has existing responder links"
                    )
        normalized.append({"address": device.address.id, "group": item["group"]})
    return normalized


def _matching(device, record):
    return list(
        device.aldb.find(
            group=record["group"],
            target=record["target"],
            is_controller=record["controller"],
            in_use=True,
            data3=None if record["controller"] else record["data3"],
        )
    )


def _validate_databases(owned, desired):
    affected = {_key(record)[0] for record in owned.values()}
    for address in affected:
        device = _device(address)
        if not device.aldb.is_loaded:
            raise SceneControllerError(
                f"Read the complete All-Link Database for {device.address} before saving"
            )
        for pending in device.aldb.pending_changes.values():
            key = _key(
                _record(
                    address,
                    pending.is_controller,
                    pending.group,
                    pending.target,
                    pending.data1,
                    pending.data2,
                    pending.data3,
                )
            )
            if key not in owned:
                raise SceneControllerError(
                    f"{device.address} has unrelated pending All-Link Database changes"
                )
        hwm = device.aldb.high_water_mark_mem_addr
        if hwm is not None:
            free = sum(
                not rec.is_in_use and not rec.is_high_water_mark
                for _, rec in device.aldb.items()
            )
            required = 0
            for key, record in owned.items():
                if key[0] != address:
                    continue
                matches = _matching(device, record)
                if key in desired:
                    required += not matches
                    free += max(0, len(matches) - 1)
                else:
                    free += len(matches)
            # Leave room for a high-water-mark record below appended links.
            if required > free + hwm // 8:
                raise SceneControllerError(
                    f"The All-Link Database for {device.address} is full"
                )


def _stage(owned, desired):
    affected = {}
    for key, record in owned.items():
        device = _device(record["address"])
        affected[device.address.id] = device
        for mem_addr, pending in list(device.aldb.pending_changes.items()):
            pending_key = _key(
                _record(
                    device.address,
                    pending.is_controller,
                    pending.group,
                    pending.target,
                    pending.data1,
                    pending.data2,
                    pending.data3,
                )
            )
            if pending_key == key:
                device.aldb.pending_changes.pop(mem_addr)
        matches = _matching(device, record)
        wanted = desired.get(key)
        if wanted is None:
            for rec in matches:
                device.aldb.remove(rec.mem_addr)
            continue
        for duplicate in matches[1:]:
            device.aldb.remove(duplicate.mem_addr)
        if matches:
            rec = matches[0]
            if (rec.data1, rec.data2, rec.data3) != (
                wanted["data1"],
                wanted["data2"],
                wanted["data3"],
            ):
                device.aldb.modify(
                    rec.mem_addr,
                    data1=wanted["data1"],
                    data2=wanted["data2"],
                    data3=wanted["data3"],
                )
        else:
            device.aldb.add(
                group=wanted["group"],
                target=wanted["target"],
                controller=wanted["controller"],
                data1=wanted["data1"],
                data2=wanted["data2"],
                data3=wanted["data3"],
            )
    return affected


async def async_save(scene_num, links, controllers, current_links, work_dir, name):
    """Journal a desired scene, then write only its owned ALDB records."""
    async with _state_lock:
        return await _async_save_scene(
            scene_num, links, controllers, current_links, work_dir, name
        )


async def _async_save_scene(
    scene_num, links, controllers, current_links, work_dir, name
):
    if not work_dir:
        raise SceneControllerError(
            "A working directory is required for device-controlled scenes"
        )
    previous = _scenes.get(scene_num, {})
    controllers = _validate_controllers(scene_num, controllers, previous)
    desired = _records(scene_num, links, controllers)
    owned = _records(scene_num, current_links, previous.get("controllers", []))
    owned.update({_key(record): record for record in previous.get("records", [])})
    owned.update(desired)
    _validate_databases(owned, desired)
    state = {
        "controllers": controllers,
        "links": links,
        "records": list(owned.values()),
        "name": name,
        "pending": True,
    }
    _scenes[scene_num] = state
    await _async_save(work_dir)
    affected = _stage(owned, desired)
    failed = False
    for device in affected.values():
        if not device.aldb.pending_changes:
            continue
        if device.is_battery:
            awake = await device.async_keep_awake()
            if awake != ResponseStatus.SUCCESS:
                _LOGGER.error(
                    "Scene %s controller %s must be awake before writing",
                    scene_num,
                    device.address,
                )
                failed = True
                continue
            _, count = await device.aldb.async_write_on_wake()
        else:
            _, count = await device.aldb.async_write()
        if count:
            _LOGGER.error(
                "Scene %s could not be written to %s; wake battery devices and retry",
                scene_num,
                device.address,
            )
            failed = True
    if failed:
        return ResponseStatus.FAILURE
    state["records"] = list(desired.values())
    state["pending"] = False
    await _async_save(work_dir)
    return ResponseStatus.SUCCESS


async def async_delete(scene_num, current_links, work_dir):
    """Remove owned records, retaining the journal if any device fails."""
    async with _state_lock:
        previous = _scenes[scene_num]
        result = await _async_save_scene(
            scene_num, [], [], current_links, work_dir, previous["name"]
        )
        if result == ResponseStatus.SUCCESS:
            _scenes.pop(scene_num)
            await _async_save(work_dir)
        return result
