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
from ..config import ON_LEVEL, RAMP_RATE
from ..constants import DeviceCategory, ResponseStatus

SCENE_CONTROLLERS_FILE = "insteon_scene_controllers.json"
# Lighting devices whose buttons respond to scenes (loads or keypad indicators).
RESPONDING_CONTROLLER_CATEGORIES = {
    DeviceCategory.DIMMABLE_LIGHTING_CONTROL,
    DeviceCategory.SWITCHED_LIGHTING_CONTROL,
}
DEFAULT_ON_LEVEL = 255
DEFAULT_RAMP_RATE = 28
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


def _local_setting(device, name, default):
    prop = device.properties.get(name)
    if prop is None or not prop.is_loaded or prop.value is None:
        return default
    return int(prop.value)


def _responds(device):
    return not device.is_battery and device.cat in RESPONDING_CONTROLLER_CATEGORIES


def valid_output(device, data3):
    """Return whether data3 selects an output of the responder."""
    return (
        not device.groups
        or data3 in device.groups
        or (len(device.groups) == 1 and data3 == 0)
    )


def _controller_link(controller):
    """Return the responder link that keeps a controller button in the scene."""
    device = _device(controller["address"])
    if not _responds(device):
        return None
    on_level, ramp_rate = DEFAULT_ON_LEVEL, DEFAULT_RAMP_RATE
    if controller["group"] == 1:
        # Match a button press, which uses the load's local settings.
        on_level = _local_setting(device, ON_LEVEL, on_level) or DEFAULT_ON_LEVEL
        ramp_rate = _local_setting(device, RAMP_RATE, ramp_rate)
    return {
        "address": device.address.id,
        "data1": on_level,
        "data2": ramp_rate,
        "data3": controller["group"],
    }


def _scene_links(links, controllers):
    """Add controller buttons as responders unless explicitly configured."""
    explicit = {(Address(link["address"]).id, link["data3"]) for link in links}
    implied = []
    for controller in controllers:
        link = _controller_link(controller)
        if link and (link["address"], link["data3"]) not in explicit:
            implied.append(link)
    return [*links, *implied]


def _records(scene_num, links, controllers):
    records = {}
    sources = [{"address": devices.modem.address.id, "group": scene_num}, *controllers]
    for source in sources:
        for link in _scene_links(links, controllers):
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


def _validate_controllers(scene_num, controllers, previous, *, adopt=False):
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
        # Imports adopt the hand-made links that were found for these buttons.
        if pair not in previous_pairs and not adopt:
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


async def async_save(
    scene_num, links, controllers, current_links, work_dir, name, *, adopt=False
):
    """Journal a desired scene, then write only its owned ALDB records."""
    async with _state_lock:
        return await _async_save_scene(
            scene_num, links, controllers, current_links, work_dir, name, adopt=adopt
        )


async def _async_save_scene(
    scene_num, links, controllers, current_links, work_dir, name, *, adopt=False
):
    if not work_dir:
        raise SceneControllerError(
            "A working directory is required for device-controlled scenes"
        )
    previous = _scenes.get(scene_num, {})
    controllers = _validate_controllers(scene_num, controllers, previous, adopt=adopt)
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


def _managed_pairs():
    pairs = set()
    for scene in _scenes.values():
        pairs.update(
            (Address(item["address"]).id, item["group"])
            for item in scene["controllers"]
        )
        pairs.update(
            (record["address"], record["group"])
            for record in scene["records"]
            if record["controller"]
            and Address(record["address"]) != devices.modem.address
        )
    return pairs


def _scan_buttons():
    """Collect hand-made links by controller button from loaded databases."""
    buttons = {}

    def button(address, group):
        return buttons.setdefault(
            (Address(address).id, group), {"outputs": {}, "targets": set()}
        )

    for address in devices:
        device = devices[address]
        if device == devices.modem or not device.aldb.is_loaded:
            continue
        for rec in device.aldb.find(in_use=True):
            if rec.group == 0 or rec.target in (devices.modem.address, device.address):
                continue
            if rec.is_controller:
                button(device.address, rec.group)["targets"].add(rec.target.id)
            else:
                outputs = button(rec.target, rec.group)["outputs"]
                outputs.setdefault((device.address.id, rec.data3), set()).add(
                    (rec.data1, rec.data2)
                )
    return buttons


def _button_issues(address, group, info):
    device = devices[address]
    if device is None:
        return [f"{Address(address)} is not a known Insteon device"]
    issues = []
    if group not in controller_groups(device):
        issues.append(f"{device.address} group {group} is not a controller button")
    if not device.aldb.is_loaded:
        issues.append(f"Read the complete All-Link Database for {device.address}")
    responders = {output[0] for output in info["outputs"]}
    for target in sorted(info["targets"] - responders):
        responder = devices[target]
        if responder is None or not responder.aldb.is_loaded:
            issues.append(f"Read the complete All-Link Database for {Address(target)}")
    for (target, data3), levels in sorted(info["outputs"].items()):
        if len(levels) > 1:
            issues.append(f"{Address(target)} has conflicting links to this button")
        if not valid_output(devices[target], data3):
            issues.append(f"{Address(target)} has no output {data3}")
    return issues


def _candidate(members, buttons, issues):
    outputs = {}
    for member in members:
        for output, levels in buttons[member]["outputs"].items():
            outputs.setdefault(output, set()).update(levels)
    if not issues and any(len(levels) > 1 for levels in outputs.values()):
        return None
    links = [
        {
            "address": address,
            "data1": data1,
            "data2": data2,
            "data3": data3,
        }
        for (address, data3), levels in sorted(outputs.items())
        for data1, data2 in levels
    ]
    if not links:
        issues = [*issues, "The button has no responders"]
    return {
        "controllers": [
            {"address": address, "group": group} for address, group in sorted(members)
        ],
        "links": links,
        "issues": issues,
        "importable": not issues,
    }


def find_device_scenes():
    """Return hand-made device-controlled scenes which can be imported.

    Buttons are grouped when they control the same set of outputs (including each
    other) with the same settings, as created by Insteon Director.
    """
    managed = _managed_pairs()
    buttons = {
        pair: info for pair, info in _scan_buttons().items() if pair not in managed
    }
    candidates = []
    groups = {}
    for (address, group), info in sorted(buttons.items()):
        issues = _button_issues(address, group, info)
        if issues:
            candidates.append(_candidate([(address, group)], buttons, issues))
            continue
        outputs = set(info["outputs"])
        if _responds(devices[address]):
            outputs.add((address, group))
        groups.setdefault(frozenset(outputs), []).append((address, group))
    for members in groups.values():
        combined = _candidate(members, buttons, [])
        if combined is not None:
            candidates.append(combined)
            continue
        candidates.extend(_candidate([member], buttons, []) for member in members)
    return sorted(
        candidates,
        key=lambda item: [(c["address"], c["group"]) for c in item["controllers"]],
    )
