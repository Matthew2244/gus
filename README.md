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
- Practice mode, with a pretend WING, so you can try it with no console. The pretend WING can play real recordings too, with `--sim-audio "A1=kick.wav,B22=vox.wav@-30"` or `--sim-audio-dir` and a folder of files named after the channels.
- A "just tell me" mode that measures and changes nothing.
- Undo for the last run.

## Safety

- He only ever changes preamp gain. Never phantom power, never faders.
- He never raises a silent input, and always brings a clipping one down.
- Every change is logged, with the old and new value.

## How Gus was tested

Besides the tests, Gus was run over a real live multitrack: kick, two snares, three toms, hi-hat, overheads, organ, keys, guitar, vocal and backing tracks, plus bass and horn takes. The pretend WING played each track the way the real meter is expected to report it, a peak every 50 ms, with gain moving the level and clipping at full scale. Each track was started too low, about right and clipping, at 40 places in the set.

That turned up four things, now fixed:

- The "level didn't follow the gain" warning went off in up to half the runs, just because music gets louder and softer. Now it needs at least 6 dB of gain change and almost no level change, and it fires in 10 percent of runs or fewer.
- A softer bar on the check pass could make him raise, then lower again. Now each check counts everything he has heard so far.
- A snare at low gain, lots of short separate hits, was called "only a blip". Now separate hits count.
- Toms are mostly kit bleed while the band plays, and clipped in most runs. They now have their own target, minus 14, and in a whole-band run Gus suggests doing them one at a time.

The 8-second listen, the 12 dB step, the 2 dB window and the other targets held up, so they stayed. The regression tests make their own short drum, voice and keys signals, so no audio is stored here.

## Status

Tested against a simulator and a fake WING built from the official WING remote protocol document. It has not been tested on a real console yet. Check the meter mapping first with `wing-autogain --probe A`.

## Install

Command line: put `wing_autogain.py` somewhere on your PATH as `wing-autogain`.

App: run `GusApp/build.sh`. It needs Xcode, and it installs Gus.app in Applications.

## License

MIT
