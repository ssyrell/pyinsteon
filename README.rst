pyinsteon - Python Insteon Package
==================================

|Build status| |GitHub release| |PyPI|

This is a Python package to interface with an Insteon Modem. It has been
tested to work with most USB or RS-232 serial based devices such as the
`2413U <https://www.insteon.com/powerlinc-modem-usb>`__,
`2412S <https://www.insteon.com/powerlinc-modem-serial>`__,
`2448A7 <http://www.insteon.com/usb-wireless-adapter>`__ and Hub models
`2242 <https://www.insteon.com/support-knowledgebase/2014/9/26/insteon-hub-owners-manual>`__
and `2245 <https://www.insteon.com/insteon-hub/>`__. Other models have
not been tested but the underlying protocol has not changed much over
time so it would not be surprising if it worked with a number of other
models. If you find success with something, please let us know.

This **pyinsteon** package was created primarily to support an INSTEON
platform for the `Home Assistant <https://home-assistant.io/>`__
automation platform but it is structured to be general-purpose and
should be usable for other applications as well.

Requirements
------------

-  Python 3.9, 3.10, 3.11 or 3.12
-  Posix or Windows based system
-  Some form of Insteon PLM or Hub
-  At least one Insteon device

Installation
------------

You can, of course, just install the most recent release of this package
using ``pip``. This will download the more recent version from
`PyPI <https://pypi.python.org/pypi/pyinsteon>`__ and install it to
your host.

::

    pip install pyinsteon

If you want to grab the the development code, you can also clone this
git repository and install from local sources:

::

    cd pyinsteon
    pip install .

Device-controlled scenes
------------------------

``async_add_or_update_scene`` accepts an optional ``controllers`` list of
``{"address": "aa.bb.cc", "group": 3}`` objects. The scene number remains the
modem's group; each hardware controller uses its own button group. The modem
always controls the scene, including when hardware controllers are selected.
Omitting ``controllers`` preserves existing associations; passing ``[]``
removes hardware controllers without deleting the app-controlled scene.

Provide ``work_dir`` when editing device-controlled scenes. Associations and
an interrupted-write journal are stored in ``insteon_scene_controllers.json``
beside the existing scene-name file. Include both files and the saved device
databases in backups. Do not infer associations from equal group numbers.
Existing modem-only scenes are unchanged and are not automatically paired with
hardware scenes created by other applications.

Read complete All-Link Databases before editing. Unmanaged controller groups
with existing device links are rejected rather than overwritten. Battery
controllers must be awake; saves do not report queued writes as successful.
A failed save may have changed some devices. Retrieve the scene's ``pending``
status and retry the same scene number, or retry deletion. The journal retains
ownership of removed controllers until all required writes succeed.

Responder levels and ramp rates are synchronized across underlying groups.
Controllers can also be responders to the modem or other controllers. A
physical controller's own load uses its local button settings; no self-links
or local-load configuration changes are made. Controllers whose databases
cannot be written remotely are not supported.

.. |Build status| image:: https://dev.azure.com/pyinsteon/pyinsteon/_apis/build/status/pyinsteon.pyinsteon?branchName=main
   :target: https://dev.azure.com/pyinsteon/pyinsteon/_build/latest?definitionId=1&branchName=main
.. |GitHub release| image:: https://img.shields.io/github/release/pyinsteon/pyinsteon.svg
   :target: https://github.com/pyinsteon/pyinsteon/releases
.. |PyPI| image:: https://img.shields.io/pypi/v/pyinsteon.svg
   :target: https://pypi.python.org/pypi/pyinsteon
