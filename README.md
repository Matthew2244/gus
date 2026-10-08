# Gus

Gus is an auto gain for the Behringer WING. He listens to your inputs through the WING's meters while someone plays, sings or talks, sets each preamp to a good level, and tells you out loud what he changed.

The WING doesn't have auto gain built in. Gus does it from a Mac on the same network.

It works with VoiceOver from the ground up, and it looks good too.

## What's here

- `wing_autogain.py`: the command-line tool, `wing-autogain`. Python 3, no extra packages.
- `GusApp/`: a native Mac app (SwiftUI) on top of it.
- `test_wing_autogain.py`: tests, run against a built-in pretend WING.

## What Gus does

- Sets gain for everything, one DCA, one channel or one input.
- Listens for 8 seconds, moves gain in small steps, then listens again to check.
- Speaks each result as it goes. In the app, through VoiceOver if it's running.
- Practice mode, with a pretend WING, so you can try it with no console.
- A "just tell me" mode that measures and changes nothing.
- Undo for the last run.

## Safety

- He only ever changes preamp gain. Never phantom power, never faders.
- He never raises a silent input, and always brings a clipping one down.
- Every change is logged, with the old and new value.

## Status

Tested against a simulator and a fake WING built from the official WING remote protocol document. It has not been tested on a real console yet. Check the meter mapping first with `wing-autogain --probe A`.

## Install

Command line: put `wing_autogain.py` somewhere on your PATH as `wing-autogain`.

App: run `GusApp/build.sh`. It needs Xcode, and it installs Gus.app in Applications.

## License

MIT
