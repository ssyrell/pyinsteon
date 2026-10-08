"""Manage Insteon Scenes."""

import asyncio
import json
import logging
from os import path
from typing import Dict, Union

import aiofiles
import voluptuous as vol

from . import scene_controller_manager as controller_manager
from .. import devices
from ..address import Address
from ..constants import ResponseStatus
from ..handlers.send_all_link_off import SendAllLinkOffCommandHandler
from ..handlers.send_all_link_on import SendAllLinkOnCommandHandler
from ..utils import multiple_status
from .device_link_manager import LinkInfo
from .scene_controller_manager import SceneControllerError

SCENE_FILE = "insteon_scenes.json"
_LOGGER = logging.getLogger(__name__)
ControllerAddress = Address
ResponderAddress = Address
Group = int
_scene_names = {}
_scene_lock = asyncio.Lock()

DeviceLinkSchema = vol.Schema(
    [
        {
            vol.Required("address"): str,
            vol.Required("data1"): int,
            vol.Required("data2"): int,
            vol.Required("data3"): int,
        }
    ]
)


def _json_key_to_int(key):
    """Ensure the Json key is an int."""
    if isinstance(key, dict):
        return {int(k): v for k, v in key.items()}
    return key


async def _get_scene_device_status(group: int):
    """Get the status of the devices in a scene."""
    scene = await async_get_scene(group)
    for addr in scene["devices"]:
        device = devices[addr]
        if device:
            await device.async_status()


async def async_trigger_scene_on(group):
    """Trigger an Insteon scene ON."""
    await SendAllLinkOnCommandHandler().async_send(group=group)
    await asyncio.sleep(2)
    await _get_scene_device_status(group)


async def async_trigger_scene_off(group):
    """Trigger an Insteon scene OFF."""
    await SendAllLinkOffCommandHandler().async_send(group=group)
    await asyncio.sleep(2)
    await _get_scene_device_status(group)


async def async_get_scenes(work_dir=None):
    """Return a list of scenes."""
    scenes: Dict[Group, Dict[str, Union[Dict[ResponderAddress, LinkInfo], str]]] = {}
    if work_dir:
        await async_load_scene_names(work_dir=work_dir)
        await controller_manager.async_load(work_dir)
    for addr in devices:
        device = devices[addr]
        if device == devices.modem:
            continue
        for rec in device.aldb.find(
            target=devices.modem.address, is_controller=False, in_use=True
        ):
            if rec.group == 0:
                continue
            if not scenes.get(rec.group):
                scenes[rec.group] = {}
                scenes[rec.group]["name"] = _scene_names.get(
                    rec.group, f"Insteon Scene {rec.group}"
                )
                scenes[rec.group]["group"] = rec.group
                scenes[rec.group]["devices"] = {}
            scene = scenes.get(rec.group)
            if not scene["devices"].get(device.address):
                scene["devices"][device.address] = []
            has_controller = False
            for _ in devices.modem.aldb.find(
                target=device.address, group=rec.group, is_controller=True, in_use=True
            ):
                has_controller = True
                break
            scene["devices"][device.address].append(
                LinkInfo(rec.data1, rec.data2, rec.data3, has_controller, True)
            )
    for scene_num in controller_manager.scene_numbers():
        if scene_num not in scenes:
            scenes[scene_num] = {
                "name": _scene_names.get(scene_num, f"Insteon Scene {scene_num}"),
                "group": scene_num,
                "devices": {},
            }
    for scene_num, scene in scenes.items():
        _add_controller_metadata(scene_num, scene)
    return scenes


async def async_get_scene(scene_num: int, work_dir: str = None):
    """Return a scenes."""
    scene: Dict[str, Union[Dict[ResponderAddress, LinkInfo], str]] = {}
    if work_dir:
        await async_load_scene_names(work_dir=work_dir)
        await controller_manager.async_load(work_dir)
    scene["name"] = _scene_names.get(scene_num, f"Insteon Scene {scene_num}")
    scene["group"] = scene_num
    scene["devices"] = {}
    for addr in devices:
        device = devices[addr]
        if device == devices.modem:
            continue
        for rec in device.aldb.find(
            target=devices.modem.address,
            is_controller=False,
            in_use=True,
            group=scene_num,
        ):
            if rec.group == 0:
                continue
            if not scene["devices"].get(device.address):
                scene["devices"][device.address] = []
            has_controller = False
            for _ in devices.modem.aldb.find(
                target=device.address, group=rec.group, is_controller=True, in_use=True
            ):
                has_controller = True
                break
            scene["devices"][device.address].append(
                LinkInfo(rec.data1, rec.data2, rec.data3, has_controller, True)
            )
    _add_controller_metadata(scene_num, scene)
    return scene


def _add_controller_metadata(scene_num, scene):
    metadata = controller_manager.scene_metadata(scene_num)
    scene["controllers"] = (
        [dict(item) for item in metadata["controllers"]] if metadata else []
    )
    scene["pending"] = bool(metadata and metadata["pending"])
    if metadata and metadata["pending"]:
        scene["name"] = metadata["name"]
        scene["devices"] = {}
        for link in metadata["links"]:
            scene["devices"].setdefault(Address(link["address"]), []).append(
                LinkInfo(link["data1"], link["data2"], link["data3"], False, False)
            )


def _scene_links(scene):
    return [
        {
            "address": Address(address).id,
            "data1": link.data1,
            "data2": link.data2,
            "data3": link.data3,
        }
        for address, links in scene["devices"].items()
        for link in links
    ]


def set_scene_name(scene_num: int, name: str):
    """Set the friendly name of a scene."""
    _scene_names[scene_num] = name


async def async_load_scene_names(work_dir: str):
    """Load the scene names from the saved location."""
    json_file = {}
    scene_file = path.join(work_dir, SCENE_FILE)
    try:
        async with aiofiles.open(scene_file, "r") as afp:
            json_file = await afp.read()
        try:
            saved_devices = json.loads(json_file, object_hook=_json_key_to_int)
            _scene_names.clear()
            for scene_num, name in saved_devices.items():
                _scene_names[scene_num] = name
        except json.decoder.JSONDecodeError:
            _LOGGER.debug("Loading saved device file failed")
    except FileNotFoundError:
        _LOGGER.debug("Saved device file not found")


async def async_save_scene_names(work_dir: str):
    """Write the scenes to a file."""
    out_json = json.dumps(_scene_names, indent=2)
    scene_file = path.join(work_dir, SCENE_FILE)
    async with aiofiles.open(scene_file, "w") as afp:
        await afp.write(out_json)
        await afp.flush()


async def async_add_device_to_scene(
    address: Address,
    scene_num: int,
    data1: int = 255,
    data2: int = 0,
    data3: int = 1,
    delay_write: bool = False,
    work_dir: str = None,
):
    """Add a device to a scene."""
    scene = await async_get_scene(scene_num, work_dir=work_dir)
    if controller_manager.scene_metadata(scene_num):
        if delay_write:
            raise SceneControllerError(
                "Delayed writes are not supported for device-controlled scenes"
            )
        links = [
            link
            for link in _scene_links(scene)
            if not (
                Address(link["address"]) == Address(address) and link["data3"] == data3
            )
        ]
        links.append(
            {
                "address": Address(address).id,
                "data1": data1,
                "data2": data2,
                "data3": data3,
            }
        )
        return await async_add_or_update_scene(scene_num, links, work_dir=work_dir)
    device = devices[address]
    modem = devices.modem
    device.aldb.add(
        group=scene_num,
        target=modem.address,
        controller=False,
        data1=data1,
        data2=data2,
        data3=data3,
    )
    modem.aldb.add(
        group=scene_num,
        target=device.address,
        controller=True,
        data1=int(device.cat),
        data2=device.subcat,
        data3=device.firmware if device.firmware is not None else 0,
    )
    if not delay_write:
        await device.aldb.async_write()
        await modem.aldb.async_write()


async def async_remove_device_from_scene(
    address: Address,
    scene_num: int,
    delay_write: bool = False,
    work_dir: str = None,
):
    """Remove a device from a scene."""
    scene = await async_get_scene(scene_num, work_dir=work_dir)
    if controller_manager.scene_metadata(scene_num):
        if delay_write:
            raise SceneControllerError(
                "Delayed writes are not supported for device-controlled scenes"
            )
        links = [
            link
            for link in _scene_links(scene)
            if Address(link["address"]) != Address(address)
        ]
        return await async_add_or_update_scene(scene_num, links, work_dir=work_dir)

    device = devices[address]
    modem = devices.modem
    for rec in device.aldb.find(
        group=scene_num, target=modem.address, is_controller=False, in_use=True
    ):
        device.aldb.remove(rec.mem_addr)
    for rec in modem.aldb.find(
        group=scene_num, target=device.address, is_controller=True, in_use=True
    ):
        modem.aldb.remove(rec.mem_addr)

    if not delay_write:
        await device.aldb.async_write()
        await modem.aldb.async_write()


async def async_add_or_update_scene(
    scene_num: int,
    links: DeviceLinkSchema,
    name: str = None,
    work_dir: str = None,
    controllers=None,
) -> int:
    """Create or update a scene with a list of devices and device link data."""
    async with _scene_lock:
        return await _async_add_or_update_scene(
            scene_num, links, name, work_dir, controllers
        )


async def _async_add_or_update_scene(scene_num, links, name, work_dir, controllers):
    if scene_num > 255:
        raise SceneControllerError("Scene groups must be between 1 and 255")
    try:
        DeviceLinkSchema(links)
    except vol.Error as exc:
        _LOGGER.error("Invalid DeviceLinkSchema format for links parameter.")
        raise exc

    updated_devices = []
    curr_scene = await async_get_scene(scene_num, work_dir=work_dir)

    if scene_num <= 0:
        scene_num = await _find_next_scene()
        curr_scene["name"] = f"Insteon Scene {scene_num}"

    metadata = controller_manager.scene_metadata(scene_num)
    if controllers == [] and not metadata:
        controllers = None
    if controllers is not None or metadata:
        if controllers is None:
            controllers = metadata["controllers"]
        for link in links:
            if any(not 0 <= link[key] <= 255 for key in ("data1", "data2", "data3")):
                raise SceneControllerError("Responder data must be between 0 and 255")
            if (
                devices[link["address"]] is None
                or devices[link["address"]] == devices.modem
            ):
                raise SceneControllerError(f"Invalid scene responder {link['address']}")
            responder = devices[link["address"]]
            if (
                responder.groups
                and link["data3"] not in responder.groups
                and not (len(responder.groups) == 1 and link["data3"] == 0)
            ):
                raise SceneControllerError(
                    f"Invalid responder output {link['data3']} for {responder.address}"
                )
        identities = [(Address(link["address"]).id, link["data3"]) for link in links]
        if len(identities) != len(set(identities)):
            raise SceneControllerError("A responder output was selected more than once")
        saved_name = name if name is not None else curr_scene["name"]
        result = await controller_manager.async_save(
            scene_num,
            links,
            controllers,
            _scene_links(curr_scene),
            work_dir,
            saved_name,
        )
        if result == ResponseStatus.SUCCESS:
            set_scene_name(scene_num, saved_name)
            if work_dir:
                await async_save_scene_names(work_dir)
        return scene_num, result

    # Remove all records from the current scene
    # Not a big deal since when we add them back they will not need to be writen again if they did not change
    updated_devices = _remove_scene_links(scene_num, curr_scene)

    # Add the devices from device_info param
    for link in links:
        device = devices[link["address"]]
        device.aldb.add(
            group=scene_num,
            target=devices.modem.address,
            controller=False,
            data1=link["data1"],
            data2=link["data2"],
            data3=link["data3"],
        )
        if device not in updated_devices:
            updated_devices.append(device)
        devices.modem.aldb.add(
            group=scene_num,
            target=device.address,
            controller=True,
            data1=int(device.cat),
            data2=device.subcat,
            data3=device.firmware if device.firmware is not None else 0,
        )

    result = await _async_write_scene_link_changes(updated_devices)
    if name:
        set_scene_name(scene_num=scene_num, name=name)
        if work_dir:
            await async_save_scene_names(work_dir)
    return scene_num, result


async def async_delete_scene(scene_num: int, work_dir: Union[str, None]):
    """Delete a scene."""
    async with _scene_lock:
        return await _async_delete_scene(scene_num, work_dir)


async def _async_delete_scene(scene_num, work_dir):
    curr_scene = await async_get_scene(scene_num, work_dir=work_dir)
    if controller_manager.scene_metadata(scene_num):
        result = await controller_manager.async_delete(
            scene_num, _scene_links(curr_scene), work_dir
        )
        if result == ResponseStatus.SUCCESS:
            _scene_names.pop(scene_num, None)
            if work_dir:
                await async_save_scene_names(work_dir)
        return result
    updated_devices = _remove_scene_links(scene_num, curr_scene)
    result = await _async_write_scene_link_changes(updated_devices)
    if result == ResponseStatus.SUCCESS:
        try:
            _scene_names.pop(scene_num)
        except KeyError:
            pass
        if work_dir:
            await async_save_scene_names(work_dir=work_dir)
    return result


def _remove_scene_links(scene_num: int, scene_data):
    """Mark all current scene links as unused.

    Does not write to the database.
    """
    updated_devices = []
    if scene_data:
        curr_device_info = scene_data["devices"]
        for addr, info_list in curr_device_info.items():
            device = devices[addr]
            for info in info_list:
                for rec in device.aldb.find(
                    is_controller=False,
                    group=scene_num,
                    target=devices.modem.address,
                    data3=info.data3,
                    in_use=True,
                ):
                    device.aldb.remove(rec.mem_addr)
                    if device not in updated_devices:
                        updated_devices.append(device)
                for modem_rec in devices.modem.aldb.find(
                    is_controller=True,
                    target=device.address,
                    group=scene_num,
                    in_use=True,
                ):
                    devices.modem.aldb.remove(modem_rec.mem_addr)
    return updated_devices


async def _async_write_scene_link_changes(device_list):
    """Write the ALDB for the devices changed."""
    results = []
    for device in device_list:
        _, failed = await device.aldb.async_write()
        result = ResponseStatus.FAILURE if failed else ResponseStatus.SUCCESS
        results.append(result)
    _, failed = await devices.modem.aldb.async_write()
    result = ResponseStatus.FAILURE if failed else ResponseStatus.SUCCESS
    results.append(result)
    return multiple_status(*results)


async def _find_next_scene():
    """Return the next available scene number."""
    next_scene = 20
    scenes = await async_get_scenes()
    occupied = set(scenes) | controller_manager.scene_numbers()
    if devices.modem:
        occupied.update(
            rec.group for _, rec in devices.modem.aldb.items() if rec.is_in_use
        )
    while next_scene in occupied:
        next_scene += 1
    if next_scene > 255:
        raise SceneControllerError("No modem scene groups are available")
    return next_scene
