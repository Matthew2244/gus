#!/usr/bin/env python3
"""Tests for wing-autogain. Run:  python3 -m unittest -v test_wing_autogain

Three levels:
  1. pure logic (analysis, decision, wording, codecs) against the RP examples
  2. the whole loop against SimConsole (no network)
  3. the REAL WingConsole class against FakeWing, a localhost server that
     speaks OSC on UDP and the native protocol on TCP + UDP meter packets,
     written from the protocol document. This only proves the code matches
     our reading of the document; the real console is the final word.
"""

import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wing_autogain as W  # noqa: E402

SHOW = os.path.join(W.SHOWS_DIR, "The Woodshed.snap")
HAVE_SHOW = os.path.isfile(SHOW)


def frames_at(peak, n=160, active=0.75, floor=-80.0):
    out = []
    for i in range(n):
        if (i % 100) < active * 100:
            out.append(peak if i % 10 == 0 else peak - 3)
        else:
            out.append(floor)
    return out


def quick_opts(**kw):
    o = {"listen": 1.0, "target": None, "max_step": 12.0, "max_gain": None,
         "tolerance": W.TOLERANCE_DB, "dry_run": False, "confirm": True,
         "max_passes": 3, "each": False, "plain": True}
    o.update(kw)
    return o


def make_sim(levels=None, link=True, follow=True, sources=None):
    if sources is None:
        sources = {("B", 22): {"name": "Matt Vox", "mode": "M", "g": 0.0, "vph": False},
                   ("A", 1): {"name": "Kick In", "mode": "M", "g": 0.0, "vph": False},
                   ("A", 13): {"name": "Overheads", "mode": "ST", "g": 0.0, "vph": True},
                   ("A", 14): {"name": "Overheads", "mode": "ST", "g": 0.0, "vph": True},
                   ("B", 11): {"name": "Montage", "mode": "ST", "g": 20.0, "vph": False},
                   ("B", 12): {"name": "Montage", "mode": "ST", "g": 20.0, "vph": False},
                   ("B", 25): {"name": "Vox 4", "mode": "M", "g": 30.0, "vph": True}}
    chans = [{"kind": "ch", "num": 32, "name": "Matt Vox", "tags": "#D7", "grp": "B", "n": 22, "altsrc": False},
             {"kind": "ch", "num": 1, "name": "Kick In", "tags": "#D1", "grp": "A", "n": 1, "altsrc": False},
             {"kind": "ch", "num": 13, "name": "Overheads", "tags": "#D1", "grp": "A", "n": 13, "altsrc": True},
             {"kind": "ch", "num": 25, "name": "Montage", "tags": "#D4", "grp": "B", "n": 11, "altsrc": False},
             {"kind": "ch", "num": 35, "name": "Vox 4", "tags": "#D8", "grp": "B", "n": 25, "altsrc": False},
             {"kind": "aux", "num": 4, "name": "Phone", "tags": "#D11", "grp": "AUX", "n": 1, "altsrc": False}]
    dcas = {1: "DRUMS", 4: "KEYS", 7: "MATT VOX", 8: "VOCALS", 11: "PLAYBACK"}
    model = W.SimModel(sources, levels or {}, link_stereo=link, follow=follow)
    return W.SimConsole(model, chans, dcas)


class TmpLog:
    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = W.Log(os.path.join(self.dir.name, "log.jsonl"))
        return self.log

    def __exit__(self, *a):
        self.dir.cleanup()


def run(console, targets, **kw):
    with TmpLog() as log:
        lines = []
        rid, results = W.run_autogain(console, targets, quick_opts(**kw), log,
                                      lines.append, None, W.Voice(kw.get("plain", True)))
        return results, lines, log.records()


# ---------------------------------------------------------------------------
class TestCodecs(unittest.TestCase):
    def test_osc_roundtrip_and_padding(self):
        pkt = W.osc_encode("/ch/2/fdr", -3.0)
        self.assertEqual(len(pkt) % 4, 0)
        # RP "Writing (Set) Parameter": '/ch/2/fdr~~~,f~~[-3.0000]' = 20 bytes
        self.assertEqual(len(pkt), 20)
        self.assertEqual(W.osc_decode(pkt), ("/ch/2/fdr", [-3.0]))

    def test_get_reply_sff(self):
        # RP example: /ch/1/fdr~~~,sff~~~~-oo~[0.0000][-144.0000]
        raw = bytes.fromhex("2f63682f312f6664720000002c736666000000002d6f6f0000000000c3100000")
        addr, args = W.osc_decode(raw)
        self.assertEqual(addr, "/ch/1/fdr")
        self.assertEqual(args[0], "-oo")
        self.assertEqual(W.osc_value(args), -144.0)

    def test_get_reply_sfi(self):
        raw = bytes.fromhex("2f63682f312f6d75746500002c73666900000000310000003f80000000000001")
        self.assertEqual(W.osc_value(W.osc_decode(raw)[1]), 1)

    def test_parse_gain_range(self):
        desc = " mode  list [M, ST, M/S]~ g   lin [-3.0 .. 45.5 dB], 98 steps~ vph int [0 .. 1]~"
        self.assertEqual(W.parse_gain_range(desc), (-3.0, 45.5, 0.5))
        self.assertIsNone(W.parse_gain_range("fxmix lin [0 .. 100 %], 101 steps"))

    def test_nrp_tx_examples_from_rp(self):
        # RP: "sending d702dfaf0e02 to channel 1 will transfer dfd0d702dfaf0e02"
        self.assertEqual(W.nrp_frame(0, bytes.fromhex("d702dfaf0e02")).hex(), "dfd0d702dfaf0e02")
        # "sending d702dfdf0e02 to channel 2 will transfer dfd1d702dfdf0e02"
        self.assertEqual(W.nrp_frame(1, bytes.fromhex("d702dfdf0e02")).hex(), "dfd1d702dfdf0e02")
        # "sending d702dfd10e02 to channel 1 ... will transfer dfd0d702dfded10e02"
        self.assertEqual(W.nrp_frame(0, bytes.fromhex("d702dfd10e02")).hex(), "dfd0d702dfded10e02")

    def test_nrp_rx_example_from_rp(self):
        # RP: "The sequence D702DFDEAF0E02 will in fact represent D702DFAF0E02"
        dec = W.NrpDecoder()
        dec.ch = 1
        got = bytes(b for _, b in dec.feed(bytes.fromhex("d702dfdeaf0e02")))
        self.assertEqual(got.hex(), "d702dfaf0e02")

    def test_nrp_roundtrip_tricky(self):
        for payload in (b"\xdf\xd3\xdf", b"\xdf", b"\x01\xdf\xde\xdf\xdf\xd0", bytes(range(0xd0, 0xe0))):
            dec = W.NrpDecoder()
            got = bytes(b for ch, b in dec.feed(W.nrp_frame(3, payload)) if ch == 3)
            self.assertEqual(got, payload, payload.hex())

    def test_meter_request_matches_rp_example(self):
        # RP: dfd3d33737 (port 0x3737) and dfd3d400000002dca001de (id 2, channel 2)
        self.assertEqual(W.nrp_frame(3, W.meter_port_payload(0x3737)).hex(), "dfd3d33737")
        self.assertEqual(W.nrp_frame(3, W.meter_request_payload(2, W.MTR_CHANNEL, [2])).hex(),
                         "dfd3d400000002dca001de")
        self.assertEqual(W.nrp_frame(3, W.meter_renew_payload(2)).hex(), "dfd3d400000002")
        # RP example spec "0xdc 0xa0 0x00 0x01 0x08 0xa6 0x04 0xde" = ch 1,2,9 ...
        self.assertEqual(W.meter_request_payload(9, W.MTR_CHANNEL, [1, 2, 9])[5:].hex(), "dca0000108de")

    def test_decode_meter_packet(self):
        pkt = struct.pack(">I", 7) + struct.pack(">3h", -12 * 256, 0, -128 * 256)
        rid, vals = W.decode_meter_packet(pkt)
        self.assertEqual(rid, 7)
        self.assertEqual(vals, [-12.0, 0.0, -128.0])


class TestAnalysisAndDecision(unittest.TestCase):
    R = (-3.0, 45.5, 0.5)

    def test_measurement(self):
        m = W.analyze(frames_at(-22))
        self.assertEqual(m.peak, -22)
        self.assertTrue(m.enough)
        self.assertFalse(m.clipped)
        self.assertLess(m.avg, -22)

    def test_raise_to_target_floor_quantized(self):
        d = W.decide(W.analyze(frames_at(-21.3)), 24.0, self.R, -12.0)
        self.assertEqual(d.action, "raise")
        self.assertEqual(d.new, 33.0)          # 24 + 9.3 floored to 0.5 grid
        self.assertAlmostEqual(d.expected_peak, -12.3, places=3)

    def test_hold_within_tolerance(self):
        d = W.decide(W.analyze(frames_at(-13.5)), 30.0, self.R, -12.0)
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.change, 0)

    def test_cap_per_pass(self):
        d = W.decide(W.analyze(frames_at(-45)), 0.0, self.R, -12.0, max_step=12)
        self.assertTrue(d.capped)
        self.assertEqual(d.new, 12.0)

    def test_hard_step_cap(self):
        d = W.decide(W.analyze(frames_at(-45)), 0.0, self.R, -12.0, max_step=40)
        self.assertEqual(d.new, 18.0)

    def test_silence_never_raises(self):
        d = W.decide(W.analyze([-90.0] * 160), 10.0, self.R, -12.0)
        self.assertEqual(d.action, "silent")
        self.assertEqual(d.new, 10.0)

    def test_blip_is_not_enough(self):
        fr = [-90.0] * 160
        fr[50] = fr[51] = -20.0
        d = W.decide(W.analyze(fr), 10.0, self.R, -12.0)
        self.assertEqual(d.action, "weak")
        self.assertEqual(d.new, 10.0)

    def test_no_data(self):
        self.assertEqual(W.decide(W.analyze([]), 10.0, self.R, -12.0).action, "nodata")

    def test_clipping_forces_at_least_6_down(self):
        d = W.decide(W.analyze(frames_at(0.0)), 30.0, self.R, -12.0)
        self.assertEqual(d.action, "lower")
        self.assertLessEqual(d.new, 24.0)
        self.assertIsNone(d.expected_peak)    # unknown above clip

    def test_at_max(self):
        d = W.decide(W.analyze(frames_at(-30)), 45.5, self.R, -12.0)
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.limit, "max")

    def test_reaches_max(self):
        d = W.decide(W.analyze(frames_at(-30)), 40.0, self.R, -12.0)
        self.assertEqual(d.new, 45.5)
        self.assertEqual(d.limit, "max")

    def test_at_min(self):
        d = W.decide(W.analyze(frames_at(-3)), -3.0, self.R, -10.0)
        self.assertEqual(d.limit, "min")
        self.assertEqual(d.new, -3.0)

    def test_user_max_gain(self):
        d = W.decide(W.analyze(frames_at(-40)), 25.0, self.R, -12.0, max_gain=30)
        self.assertEqual(d.new, 30.0)
        self.assertEqual(d.limit, "user-max")

    def test_target_ceiling(self):
        d = W.decide(W.analyze(frames_at(-20)), 10.0, self.R, 0.0)
        self.assertLessEqual(d.expected_peak, W.HARD_TARGET_CEILING)


class TestWording(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(W.say_num(-22.4), "minus 22")
        self.assertEqual(W.say_num(32.5, True), "32.5")
        self.assertEqual(W.say_num(-3.0, True), "minus 3")
        self.assertEqual(W.say_num(0.2), "0")

    def test_plain_has_no_flavour_and_fun_does(self):
        c = make_sim({("B", 22): -34.0})
        t = W.Target("B", 22, "Matt Vox")
        res, lines, _ = run(c, [t], plain=True)
        self.assertTrue(lines[-1].startswith("Matt Vox: peaks were minus 34. Raised gain"))
        self.assertTrue(lines[-1].endswith("."))
        for opts in W.FLAVOUR.values():
            for f in opts:
                self.assertNotIn(f, lines[-1])
        c = make_sim({("B", 22): -34.0})
        res, lines2, _ = run(c, [W.Target("B", 22, "Matt Vox")], plain=False)
        self.assertTrue(any(f in lines2[-1] for f in W.FLAVOUR["raised"]))

    def test_guess_preset(self):
        self.assertEqual(W.guess_preset("Matt Vox"), "vocal")
        self.assertEqual(W.guess_preset("Kick In"), "drums")
        self.assertEqual(W.guess_preset("Tom 3"), "drums")
        self.assertEqual(W.guess_preset("Montage"), "line")
        self.assertEqual(W.guess_preset("Horn 2"), "horns")
        self.assertEqual(W.guess_preset("Talkback"), "speech")
        self.assertEqual(W.guess_preset("Room Mic"), "room")
        self.assertEqual(W.guess_preset("Conga"), "perc")
        self.assertEqual(W.guess_preset("Bass"), "instrument")
        self.assertEqual(W.guess_preset("Thing"), "default")


class TestSpeech(unittest.TestCase):
    def test_one_line_per_input_then_summary_and_failures_never_break(self):
        spoken = []

        class FakeSpeaker:
            def say(self, text):
                spoken.append(text)

        c = make_sim({("B", 22): -34.0, ("B", 25): None})
        with TmpLog() as log:
            W.run_autogain(c, [W.Target("B", 22, "Matt Vox"), W.Target("B", 25, "Vox 4")],
                           quick_opts(), log, lambda s: None, FakeSpeaker())
        self.assertTrue(spoken[0].startswith("Listening to 2 inputs"))
        self.assertTrue(spoken[1].startswith("Matt Vox:"))
        self.assertTrue(spoken[2].startswith("Vox 4: WARNING"))

    def test_speaker_survives_errors(self):
        sp = W.Speaker(True)
        if not sp.enabled:
            self.skipTest("speech only on macOS")
        got = []

        def boom(text):
            got.append(text)
            raise OSError("no speech here")
        sp._speak = boom
        sp.say("one")
        sp.say("two")
        sp.finish(timeout=5)
        self.assertEqual(got, ["one", "two"])

    def test_cli_options_all_have_help(self):
        for act in W.build_parser()._actions:
            if act.dest not in ("help", "version"):
                self.assertTrue(act.help, act.dest)


class TestSimLoop(unittest.TestCase):
    def test_vocal_raised_and_confirmed(self):
        c = make_sim({("B", 22): -34.0})
        res, lines, log = run(c, [W.Target("B", 22, "Matt Vox")])
        r = res[0]
        self.assertEqual(r.outcome, "raised")
        self.assertEqual(r.gain, 22.0)                       # -34 -> -12
        self.assertTrue(r.final_measured)
        self.assertLessEqual(abs(r.last.peak - (-12)), W.TOLERANCE_DB)
        self.assertIn("Now peaking around minus 12", lines[-1])
        ch = [(x["old"], x["new"]) for x in log if x["kind"] == "change"]
        self.assertEqual(ch, [(0.0, 12.0), (12.0, 22.0)])   # 12 dB cap, then the rest

    def test_big_raise_takes_several_capped_passes(self):
        c = make_sim({("B", 22): -48.0})
        res, lines, log = run(c, [W.Target("B", 22, "Matt Vox")])
        steps = [x["new"] - x["old"] for x in log if x["kind"] == "change"]
        self.assertTrue(all(s <= 12.0 for s in steps), steps)
        self.assertEqual(res[0].gain, 36.0)

    def test_silent_input_untouched(self):
        c = make_sim({("B", 25): None})
        res, lines, log = run(c, [W.Target("B", 25, "Vox 4")])
        self.assertEqual(res[0].outcome, "silent")
        self.assertEqual(c.model.writes, [])
        self.assertIn("WARNING, no signal heard. Gain left at 30.", lines[-1])

    def test_clipping_input_lowered_then_settles(self):
        c = make_sim({("A", 1): -14.0}, sources={("A", 1): {"name": "Kick In", "mode": "M", "g": 20.0, "vph": False}})
        res, lines, log = run(c, [W.Target("A", 1, "Kick In")])
        r = res[0]
        self.assertTrue(r.first.clipped)
        self.assertEqual(r.outcome, "lowered")
        self.assertIn("CLIPPING", lines[-1])
        self.assertLessEqual(abs(r.last.peak - (-10)), W.TOLERANCE_DB)

    def test_gain_at_limit_reported(self):
        c = make_sim({("B", 22): -70.0}, sources={("B", 22): {"name": "Matt Vox", "mode": "M", "g": 40.0, "vph": False}})
        res, lines, _ = run(c, [W.Target("B", 22, "Matt Vox")])
        self.assertEqual(res[0].gain, 45.5)
        self.assertEqual(res[0].limit, "max")
        self.assertIn("WARNING, at maximum gain, 45.5", lines[-1])

    def test_too_hot_at_min(self):
        c = make_sim({("B", 11): 2.0, ("B", 12): 0.0})
        t = W.Target("B", 11, "Montage", stereo=True)
        res, lines, _ = run(c, [t])
        self.assertEqual(res[0].gain, -3.0)
        self.assertIn("minimum gain", lines[-1])

    def test_stereo_linked_both_follow(self):
        c = make_sim({("A", 13): -30.0, ("A", 14): -26.0}, link=True)
        res, _, _ = run(c, [W.Target("A", 13, "Overheads", stereo=True)])
        # judged by the louder side (-26) -> drums target -10 -> +16
        self.assertEqual(c.model.gain(("A", 13)), 16.0)
        self.assertEqual(c.model.gain(("A", 14)), 16.0)
        self.assertNotIn(("A", 14), [k for k, _ in c.model.writes])

    def test_stereo_unlinked_partner_set_too(self):
        c = make_sim({("A", 13): -30.0, ("A", 14): -26.0}, link=False)
        run(c, [W.Target("A", 13, "Overheads", stereo=True)])
        self.assertEqual(c.model.gain(("A", 14)), c.model.gain(("A", 13)))
        self.assertIn(("A", 14), [k for k, _ in c.model.writes])

    def test_dry_run_changes_nothing(self):
        c = make_sim({("B", 22): -34.0})
        res, lines, log = run(c, [W.Target("B", 22, "Matt Vox")], dry_run=True)
        self.assertEqual(c.model.writes, [])
        self.assertIn("Would raise gain 12 dB, from 0 to 12. That should peak around minus 22. "
                      "That's capped at 12 dB", lines[-1])
        self.assertFalse([x for x in log if x["kind"] == "change"])

    def test_stage_box_not_following_is_flagged(self):
        c = make_sim({("B", 22): -34.0}, follow=False)
        res, lines, _ = run(c, [W.Target("B", 22, "Matt Vox")])
        self.assertTrue(res[0].not_following)
        self.assertIn("didn't move with the gain", lines[-1])

    def test_no_meter_data(self):
        c = make_sim({("B", 22): -34.0})
        c.measure = lambda keys, seconds, on_frame=None: {k: [] for k in keys}
        res, lines, _ = run(c, [W.Target("B", 22, "Matt Vox")])
        self.assertEqual(res[0].outcome, "nodata")
        self.assertEqual(c.model.writes, [])

    def test_only_gain_paths_can_be_written(self):
        c = make_sim()
        with self.assertRaises(W.ConsoleError):
            c.set_gain("AUX", 1, 10)

    def test_undo_restores_and_respects_manual_changes(self):
        c = make_sim({("B", 22): -34.0, ("A", 1): -24.0})
        with TmpLog() as log:
            W.run_autogain(c, [W.Target("B", 22, "Matt Vox"), W.Target("A", 1, "Kick In")],
                           quick_opts(), log, lambda s: None)
            self.assertNotEqual(c.model.gain(("B", 22)), 0.0)
            c.model.sources[("A", 1)]["g"] = 5.0           # someone touched it by hand
            out = []
            rc = W.do_undo(c, log, "sim", out.append)
            self.assertEqual(c.model.gain(("B", 22)), 0.0)
            self.assertEqual(c.model.gain(("A", 1)), 5.0)  # left alone
            self.assertEqual(rc, 1)
            self.assertTrue(any("someone changed it" in o for o in out))
            out = []
            W.do_undo(c, log, "sim", out.append)
            self.assertIn("Nothing to undo", out[-1])      # undo does not redo

    def test_each_mode(self):
        c = make_sim({("B", 22): -34.0, ("A", 1): -24.0})
        res, lines, _ = run(c, [W.Target("B", 22, "Matt Vox"), W.Target("A", 1, "Kick In")], each=True)
        self.assertTrue(lines[0].startswith("Next: Matt Vox."))
        self.assertTrue(any(l.startswith("Next: Kick In.") for l in lines))


class TestResolution(unittest.TestCase):
    def setUp(self):
        self.c = make_sim()
        self.r = W.Resolver(self.c)

    def test_parse_input(self):
        self.assertEqual(W.parse_input("a1"), ("A", 1))
        self.assertEqual(W.parse_input("B 25"), ("B", 25))
        self.assertEqual(W.parse_input("local 3"), ("LCL", 3))
        with self.assertRaises(ValueError):
            W.parse_input("ch1")

    def test_channel_by_name_prefix_and_number(self):
        t, _ = self.r.resolve(channels=["matt"])
        self.assertEqual(t[0].key, ("B", 22))
        t, _ = self.r.resolve(channels=["32"])
        self.assertEqual(t[0].name, "Matt Vox")

    def test_dca_by_name_and_number(self):
        t, _ = self.r.resolve(dcas=["vocals"])
        self.assertEqual([x.name for x in t], ["Vox 4"])
        t, _ = self.r.resolve(dcas=["1"])
        self.assertEqual(sorted(x.name for x in t), ["Kick In", "Overheads"])

    def test_stereo_even_input_snaps_to_pair(self):
        t, _ = self.r.resolve(inputs=["A14"])
        self.assertEqual(t[0].key, ("A", 13))
        self.assertTrue(t[0].stereo)

    def test_aux_input_skipped(self):
        t, skipped = self.r.resolve(dcas=["playback"])
        self.assertEqual(t, [])
        self.assertIn("no preamp gain", skipped[0])

    def test_alt_warning(self):
        t, _ = self.r.resolve(channels=["Overheads"])
        self.assertTrue(any("ALT" in w for w in t[0].warnings))

    def test_ambiguous(self):
        with self.assertRaises(SystemExit):
            self.r.resolve(channels=["ox"])

    @unittest.skipUnless(HAVE_SHOW, "The Woodshed.snap not present")
    def test_real_show_file(self):
        src, chans, dcas = W.load_show(SHOW)
        c = W.SimConsole(W.SimModel(src), chans, dcas)
        t, skipped = W.Resolver(c, (src, chans, dcas)).resolve(dcas=["VOCALS"], channels=["Matt Vox"])
        self.assertEqual([x.name for x in t], ["Matt Vox", "Vox 2", "Vox 3", "Vox 4", "Vox 5"])
        self.assertEqual(t[0].key, ("B", 22))
        t, _ = W.Resolver(c, (src, chans, dcas)).resolve(dcas=["drums"])
        oh = [x for x in t if x.name == "Overheads"][0]
        self.assertTrue(oh.stereo)
        self.assertEqual(oh.key, ("A", 13))


@unittest.skipUnless(HAVE_SHOW, "The Woodshed.snap not present")
class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WING_AUTOGAIN_SIM_STATE"] = os.path.join(self.tmp.name, "sim.json")
        self.log = os.path.join(self.tmp.name, "log.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        out = []
        rc = W.main(list(args) + ["--simulate", "--log", self.log, "-q", "--listen", "2"], out=out.append)
        return rc, out

    def test_full_cli_run_status_undo(self):
        rc, out = self.cli("--dca", "VOCALS", "--plain")
        self.assertEqual(rc, 0, out)
        self.assertTrue(out[0].startswith("Listening to 4 inputs"))
        self.assertTrue(out[-1].startswith("Done: 4 raised."))
        rc, out = self.cli("--status", "-c", "Vox 2")
        self.assertNotIn("gain 0,", out[0])
        rc, out = self.cli("--undo")
        self.assertTrue(out[-1].startswith("Undo done: 4 restored"))
        rc, out = self.cli("--status", "-c", "Vox 2")
        self.assertIn("gain 0,", out[0])

    def test_sim_signal_edge_cases(self):
        rc, out = self.cli("-c", "Kick In,Tom 1,Snare 1 Top", "--sim-signal", "A1=-30,A7=silent,A3=clip")
        self.assertEqual(rc, 1)
        text = "\n".join(out)
        self.assertIn("Tom 1: WARNING, no signal heard", text)
        self.assertIn("Snare 1 Top: WARNING, CLIPPING", text)

    def test_json(self):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc, out = self.cli("-c", "Matt Vox", "--json")
        finally:
            sys.stdout = old
        data = json.loads(buf.getvalue())
        self.assertEqual(data["inputs"][0]["source"], "B22")
        self.assertEqual(data["inputs"][0]["outcome"], "raised")


# ---------------------------------------------------------------------------
# FakeWing: OSC + native protocol on localhost, driven by a SimModel
# ---------------------------------------------------------------------------
class FakeWing:
    def __init__(self, model, channels=()):
        self.model = model
        self.channels = {(c["kind"], c["num"]): c for c in channels}
        self.osc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.osc.bind(("127.0.0.1", 0))
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("127.0.0.1", 0))
        self.tcp.listen(4)
        self.osc_port = self.osc.getsockname()[1]
        self.native_port = self.tcp.getsockname()[1]
        self.stop = False
        self.requests = {}      # rid -> (kind, [idx], last_renew)
        self.client = None      # (ip, port)
        self.tcp_bytes = bytearray()
        self.osc_sets = []
        self.lock = threading.Lock()
        for fn in (self._osc_loop, self._tcp_loop, self._meter_loop):
            threading.Thread(target=fn, daemon=True).start()

    def close(self):
        self.stop = True
        self.osc.close()
        self.tcp.close()

    def _reply(self, addr, peer, *args):
        self.osc.sendto(W.osc_encode(addr, *args), peer)

    def _osc_loop(self):
        while not self.stop:
            try:
                data, peer = self.osc.recvfrom(65536)
            except OSError:
                return
            addr, args = W.osc_decode(data)
            parts = addr.strip("/").split("/")
            if addr == "/?":
                self._reply(addr, peer, "WING,127.0.0.1,FAKE,ngc-full,X,3.1.1")
            elif len(parts) == 4 and parts[0] == "io" and parts[1] == "in" and args == ["?"]:
                self._reply(addr, peer, " mode   list [M, ST, M/S]~ g    lin [-3.0 .. 45.5 dB], 98 steps~"
                                        " vph   int [0 .. 1]~ mute int [0 .. 1]~")
            elif len(parts) == 5 and parts[:2] == ["io", "in"]:
                key = (parts[2], int(parts[3]))
                src = self.model.sources.get(key)
                if src is None:
                    self._reply("/*", peer, "NODE NOT FOUND")
                    continue
                field = parts[4]
                if args and field == "g":
                    with self.lock:
                        self.osc_sets.append((addr, args[0]))
                        self.model.set_gain(key, float(args[0]))
                elif args:
                    self.osc_sets.append((addr, args[0]))   # recorded so tests can prove it never happens
                elif field == "g":
                    g = self.model.gain(key)
                    self._reply(addr, peer, "%.1f" % g, (g + 3) / 48.5, float(g))
                elif field == "vph":
                    v = int(bool(src.get("vph")))
                    self._reply(addr, peer, str(v), float(v), v)
                elif field in ("name", "mode"):
                    self._reply(addr, peer, src.get(field, ""))
            elif len(parts) >= 3 and parts[0] in ("ch", "aux", "dca"):
                c = self.channels.get((parts[0], int(parts[1])), {})
                rest = "/".join(parts[2:])
                if rest == "name":
                    self._reply(addr, peer, c.get("name", ""))
                elif rest == "tags":
                    self._reply(addr, peer, c.get("tags", ""))
                elif rest == "in/conn/grp":
                    self._reply(addr, peer, c.get("grp", "OFF"))
                elif rest == "in/conn/in":
                    n = c.get("n", 1)
                    self._reply(addr, peer, str(n), 0.0, n)
                elif rest == "in/set/altsrc":
                    self._reply(addr, peer, "0", 0.0, 0)
            else:
                self._reply("/*", peer, "NODE NOT FOUND")

    def _tcp_loop(self):
        while not self.stop:
            try:
                conn, peer = self.tcp.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn, peer), daemon=True).start()

    def _serve(self, conn, peer):
        dec = W.NrpDecoder()
        buf = []
        with conn:
            while not self.stop:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                self.tcp_bytes += data
                buf += [b for ch, b in dec.feed(data) if ch == W.METER_CHID]
                buf = self._parse(buf, peer[0])

    def _parse(self, b, ip):
        i = 0
        while i < len(b):
            t = b[i]
            if t == W.MTR_PORT and i + 3 <= len(b):
                self.client = (ip, (b[i + 1] << 8) | b[i + 2])
                i += 3
            elif t == W.MTR_ID and i + 5 <= len(b):
                rid = struct.unpack(">I", bytes(b[i + 1:i + 5]))[0]
                i += 5
                if i < len(b) and b[i] == W.MTR_START:
                    try:
                        end = b.index(W.MTR_END, i)
                    except ValueError:
                        return b[i - 5:]
                    kind, idx = b[i + 1], [x + 1 for x in b[i + 2:end]]
                    with self.lock:
                        self.requests[rid] = (kind, idx, time.time())
                    i = end + 1
                else:
                    with self.lock:
                        if rid in self.requests:
                            k, idx, _ = self.requests[rid]
                            self.requests[rid] = (k, idx, time.time())
            else:
                return b[i:] if t in (W.MTR_PORT, W.MTR_ID) else b[i + 1:]
        return []

    def _meter_loop(self):
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dev_to_grp = {v: k for k, v in W.SOURCE_DEVICE.items()}
        next_t = time.time()
        while not self.stop:
            next_t += W.METER_FRAME_S
            time.sleep(max(0.0, next_t - time.time()))
            now = time.time()
            with self.lock:
                reqs = dict(self.requests)
                for rid, (kind, idx, t0) in reqs.items():
                    if now - t0 > 5.0 or not self.client:     # RP: data times out after 5 s
                        continue
                    words = []
                    if kind == W.MTR_SOURCE:
                        for dev in idx:
                            grp = dev_to_grp[dev]
                            for n in range(1, W.GROUP_SIZE.get(grp, 8) + 1):
                                k = (grp, n)
                                words.append(self.model.frame(k) if k in self.model.sources else -128.0)
                    elif kind == W.MTR_CHANNEL:
                        for num in idx:
                            c = self.channels.get(("ch", num))
                            k = (c["grp"], c["n"]) if c else None
                            v = self.model.frame(k) if k in self.model.sources else -128.0
                            words += [v, v, v, v, -128.0, 0.0, -128.0, 0.0]
                    pkt = struct.pack(">I", rid) + b"".join(
                        struct.pack(">h", int(round(v * 256))) for v in words)
                    with contextlib_suppress():
                        out.sendto(pkt, self.client)
        out.close()


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return True


class TestRealProtocolAgainstFakeWing(unittest.TestCase):
    def setUp(self):
        self.sim = make_sim({("B", 22): -34.0, ("A", 13): -30.0, ("A", 14): -27.0})
        self.fake = FakeWing(self.sim.model, self.sim._channels)
        self.wing = W.WingConsole("127.0.0.1", osc_port=self.fake.osc_port,
                                  native_port=self.fake.native_port, timeout=0.5)

    def tearDown(self):
        self.wing.close()
        self.fake.close()

    def test_ping_get_set_range(self):
        self.assertTrue(self.wing.ping().startswith("WING,"))
        self.assertEqual(self.wing.gain("B", 25), 30.0)
        self.assertEqual(self.wing.gain_range("B", 25), ((-3.0, 45.5, 0.5), "console"))
        self.assertEqual(self.wing.set_gain("B", 25, 27.5), 27.5)
        self.assertEqual(self.wing.source_info("A", 13)["mode"], "ST")
        self.assertTrue(self.wing.source_info("A", 13)["vph"])

    def test_meters_arrive_and_decode(self):
        frames = self.wing.measure([("B", 22), ("A", 13)], 1.5)
        self.assertGreater(len(frames[("B", 22)]), 3)
        self.assertLessEqual(abs(len(frames[("A", 13)]) - len(frames[("B", 22)])), 2)
        self.assertAlmostEqual(max(frames[("B", 22)]), -34.0, delta=0.01)
        # the request on the wire starts by selecting channel 3 [RP]
        self.assertEqual(bytes(self.fake.tcp_bytes[:2]).hex(), "dfd3")

    def test_renewal_keeps_data_flowing_past_5s(self):
        # the fake stops sending 5 s after the last request/renewal, like the RP says
        times = []
        t0 = time.time()
        self.wing.measure([("B", 22)], 7.0, on_frame=lambda: times.append(time.time() - t0))
        self.assertGreater(max(times), 6.0, "data stopped at 5 s, renewal not working")
        self.assertLess(W.METER_RENEW_S, 5.0)

    def test_channel_meter_mode(self):
        self.wing.meter_mode = "channel"
        r = W.Resolver(self.wing, (self.sim.model.sources, self.sim._channels, self.sim._dcas))
        t, _ = r.resolve(channels=["Matt Vox"])
        frames = self.wing.measure([t[0].key], 1.0)
        self.assertAlmostEqual(max(frames[("B", 22)]), -34.0, delta=0.01)

    def test_full_autogain_over_the_network(self):
        r = W.Resolver(self.wing, (self.sim.model.sources, self.sim._channels, self.sim._dcas))
        targets, _ = r.resolve(channels=["Matt Vox", "Overheads"])
        with TmpLog() as log:
            lines = []
            _, res = W.run_autogain(self.wing, targets, quick_opts(listen=3.0), log, lines.append)
        self.assertEqual(self.sim.model.gain(("B", 22)), 22.0)
        self.assertEqual(self.sim.model.gain(("A", 13)), 17.0)       # louder side -27 -> -10
        self.assertEqual(self.sim.model.gain(("A", 14)), 17.0)
        # only ever wrote /g paths; never phantom
        self.assertTrue(all(a.endswith("/g") for a, _ in self.fake.osc_sets), self.fake.osc_sets)
        self.assertIn("Matt Vox: peaks were minus 34. Raised gain 22 dB to 22. Now peaking around minus 12.", lines)

    def test_unreachable_console_is_a_clear_error(self):
        dead = W.WingConsole("127.0.0.1", osc_port=1, native_port=1, timeout=0.1, retries=1)
        with self.assertRaises(W.ConsoleError) as cm:
            dead.gain("A", 1)
        self.assertIn("No answer from the WING", str(cm.exception))
        dead.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
