DeckPad
=======

DeckPad turns a Steam Deck into a wireless game controller for any PC on
the same local network. The PC sees a standard Xbox 360 controller plus
a mouse, driven by the Deck's trackpad and (optionally) its gyro; no
game-side support is required. Inspired by now broken repo of [Deckpad](https://github.com/HelloThisIsFlo/Deckpad)
by [HelloThisIsFlo](https://github.com/HelloThisIsFlo).

There is no pairing, no account, and nothing to install on the Deck. The
sender uses the SDL2 shipped with SteamOS; the receiver ships as a
standalone binary. Sender and receiver discover each other automatically
via UDP broadcast on port 30666.

[Post I made about it on slop.com](https://x.com/ThatOneKrish/status/2075829957276946452)

WHAT DO I NEED?
---------------

  - A Steam Deck and a PC (Windows or Linux) on the same network.
  - This directory placed at /home/deck/DeckPad on the Deck.
  - Nothing else. Python is NOT required on the PC when using the
    prebuilt binaries in dist/.

QUICK START
-----------

On the PC:

  Windows:
    1. Install the ViGEmBus driver (once):
         https://github.com/nefarius/ViGEmBus/releases
       The receiver will tell you if it is missing.
    2. Run dist/windows/DeckPadReceiver.exe.
    3. Allow it through the Windows Firewall when prompted (UDP 30666).

  Linux:
    $ sudo ./dist/linux/deckpad-receiver-linux

On the Deck:

    Launch DeckPad from the library (Game Mode). Within a couple of
    seconds the status line turns green:

        Receiver: <pc-name>

    A virtual Xbox 360 pad now exists on the PC. Start any game.

If the connection drops, the virtual pad resets to neutral after 0.5 s;
buttons never stay stuck.

To copy the prebuilt receivers off the Deck, run on the PC:

    $ scp -r deck@<deck-ip>:/home/deck/DeckPad/dist .

INSTALLING ON THE DECK (ONE TIME)
---------------------------------

  1. Place this directory at /home/deck/DeckPad.

  2. In Desktop Mode, open Steam -> Games -> Add a Non-Steam Game to My
     Library -> Browse... and select /home/deck/DeckPad/DeckPad.sh
     (set the file type filter to "All Files").

  3. Optionally rename the entry to "DeckPad" in its Steam properties.

  4. In Game Mode, open DeckPad's controller settings (controller icon)
     and select the "Gamepad" template so all Deck inputs reach the app.
     Back grips (L4/L5/R4/R5) and trackpads may be mapped freely in
     Steam Input; whatever they emit is what gets streamed.

LAYOUT
-------------
<img src="https://github.com/KrishGaur1354/Deckpad/blob/main/deckpad-layout.jpg" width=500>

CONFIGURATION
-------------

Hold View + Menu inside the app to open the settings menu:

    Send rate                               30-250 Hz
    Stick deadzones (left/right)            0-60 %
    Trigger deadzone                        0-60 %
    Stick sensitivity (left/right)          0.1x-2.5x
    Invert left/right Y                     on/off
    Swap A/B, Swap X/Y (Nintendo layout)    on/off
    Gyro                                    off / mouse / r-stick
    Gyro sensitivity                        0.1x-5.0x
    Touch mouse                             on/off
    Mouse sensitivity                       0.1x-5.0x
    Rumble                                  on/off

Settings are persisted in config.json next to the scripts. The file also
supports options the menu does not expose:

  "target_ip": "192.168.1.20"
        Skip auto-discovery and always send to a fixed address. Needed
        when broadcast does not cross the network (some VPNs, routers
        with client isolation).

  "port": 30666
        Change the UDP port. Pass --port to the receiver as well.

  "button_map"
        Arbitrary remapping, deck button -> output button. Valid output
        names:

            a b x y back guide start ls rs lb rb
            dpad_up dpad_down dpad_left dpad_right
            mouse_left mouse_right mouse_middle

        ls/rs are the stick clicks (L3/R3). The mouse_* outputs become
        clicks of the PC mouse, not gamepad buttons.

        Deck-only inputs paddle1..paddle4 (back grips L4/L5/R4/R5, if
        Steam Input passes them through) and misc1 may be mapped to any
        output, e.g. back grips as A/B or as mouse clicks:

            "button_map": { "paddle1": "a", "paddle2": "mouse_left" }

GYRO AND TOUCH MOUSE
--------------------

The Deck can also drive the PC's mouse pointer:

  Touch mouse ("Touch mouse" setting, default on)
        In Steam Input, map a trackpad "As Mouse" (the right trackpad
        already is in the standard Gamepad template). Its motion,
        clicks and scrolling are streamed to the PC as real mouse
        movement, left/right/middle clicks and wheel events. Great for
        desktop use or games that want a mouse.

  Mouse mode (toggle with View + Y, any time outside the menu)
        A quick-switch mode for using the PC like a desktop: the
        trackpad drives the cursor (even if "Touch mouse" is off), the
        right stick glides it, and the triggers click: RT = left
        click, LT = right click. While active those inputs are hidden
        from the gamepad, and a banner on the status screen reminds
        you how to leave. Cursor speed follows "Mouse sensitivity".

  Gyro ("Gyro" setting, default off)
        mouse    tilting the Deck moves the PC pointer (gyro aiming
                 for mouse-driven shooters).
        r-stick  gyro is added to the right stick instead, for
                 controller-native games.

        If the status screen says "sensor not available", Steam Input
        is holding the gyro: in the game's controller settings set
        Gyro Behavior to "None" (or disable Steam Input for DeckPad)
        so the raw sensor reaches the app.

  Rumble ("Rumble" setting, default on)
        Game rumble on the PC is sent back over the network and played
        on the Deck's own motors. Windows receiver only (the Linux
        uinput backend cannot report force feedback).

The Deck's status screen also shows the network round-trip time
("Ping") and the Deck's battery level next to the packet counter.

Note: both ends speak protocol DKP2 now. If the Deck cannot find a
receiver after updating, the PC is still running an old build update
both sides together (rebuild the binaries in dist/ if you use them).

RUNNING THE RECEIVER FROM SOURCE
--------------------------------

  Windows:
    $ pip install vgamepad
    $ python deckpad_receiver.py

  Linux (no packages needed):
    $ sudo python3 deckpad_receiver.py

  To run without sudo on Linux, grant uinput access once:

    $ echo 'KERNEL=="uinput", MODE="0660", TAG+="uaccess"' | \
        sudo tee /etc/udev/rules.d/70-deckpad-uinput.rules
    $ sudo udevadm control --reload && sudo udevadm trigger

BUILDING THE WINDOWS BINARY
---------------------------

Done on the Deck; requires podman:

    $ podman run --rm -v /home/deck/DeckPad:/src docker.io/tobix/pywine:3.13 bash -c \
        "cd /src && wine pip install -q pyinstaller vgamepad && \
         wine pyinstaller --onefile --noupx --clean --noconfirm --name DeckPadReceiver \
         --collect-all vgamepad --distpath /src/dist/windows \
         --workpath /tmp/build --specpath /tmp/build deckpad_receiver.py"

Note: --noupx matters. UPX-packed DLLs break the bundled Python.

IF SOMETHING GOES WRONG
-----------------------

  - Deck stuck on "searching": verify both machines are on the same
    network and the receiver is running. If the router isolates
    clients, set "target_ip" in config.json to the PC's IP.

  - L3/R3 or back grips "not working": watch the status screen while
    pressing them. L3/R3 light up as L3/R3 in the button grid; back
    grips appear in the "grips (raw)" row only if your Steam Input
    template passes them through (map L4/L5/R4/R5 in Steam Input, and
    give them an output in config.json's "button_map"). If nothing
    lights up, Steam Input is swallowing the input — fix the template,
    not DeckPad.

  - Latency: typically a few ms on the same Wi-Fi network check the
    "Ping" readout on the Deck's status screen. A 5 GHz network helps
    most; raising the send rate helps marginally.

  - Desktop-mode test on the Deck:
        $ ./DeckPad.sh --windowed

  - Headless self-check (no window):
        $ python3 deckpad_sender.py --smoke-test

HOW IT WORKS
------------

  deckpad_sender.py    Runs on the Deck. Uses the SDL2 shipped with
                       SteamOS; nothing to install. Shows a status
                       screen with live input display and a settings
                       menu navigated with the controller.

  deckpad_receiver.py  Runs on the PC. Creates a virtual Xbox 360
                       controller (ViGEmBus on Windows, uinput on
                       Linux) that any game recognizes, plus a virtual
                       mouse for the trackpad and gyro-aim features.

  Discovery is UDP broadcast on port 30666. On connection loss the
  virtual pad resets to neutral after 0.5 s.
