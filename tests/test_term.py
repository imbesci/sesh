import unittest

from sesh.core.format import bytes_, duration, number, relative_time, short_path, time_bucket
from sesh.tui.ansi import display_width, fit, paint, set_color_enabled, truncate
from sesh.tui.term import decode_keys

set_color_enabled(False)


def names(data: str) -> list[str]:
    keys, _rest = decode_keys(data)
    return [key.name for key in keys]


class DecodeKeys(unittest.TestCase):
    def test_printable_characters(self):
        self.assertEqual(names("abc"), ["a", "b", "c"])

    def test_control_characters(self):
        self.assertEqual(names("\x06"), ["ctrl+f"])
        self.assertEqual(names("\x18"), ["ctrl+x"])

    def test_enter_tab_backspace(self):
        self.assertEqual(names("\r"), ["enter"])
        self.assertEqual(names("\n"), ["enter"])
        self.assertEqual(names("\t"), ["tab"])
        self.assertEqual(names("\x7f"), ["backspace"])

    def test_arrows_and_navigation(self):
        self.assertEqual(names("\x1b[A\x1b[B\x1b[C\x1b[D"), ["up", "down", "right", "left"])
        self.assertEqual(names("\x1b[5~\x1b[6~"), ["pageup", "pagedown"])
        self.assertEqual(names("\x1b[H\x1b[F"), ["home", "end"])

    def test_shift_tab(self):
        self.assertEqual(names("\x1b[Z"), ["shift+tab"])

    def test_modified_arrows(self):
        self.assertEqual(names("\x1b[1;3A"), ["alt+up"])
        self.assertEqual(names("\x1b[1;5B"), ["ctrl+down"])

    def test_alt_letter(self):
        self.assertEqual(names("\x1bh"), ["alt+h"])

    def test_bracketed_paste_is_one_event(self):
        keys, rest = decode_keys("\x1b[200~3f9a1b2c-0000\x1b[201~")
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0].name, "paste")
        self.assertEqual(keys[0].pasted, "3f9a1b2c-0000")
        self.assertEqual(rest, "")

    def test_paste_with_newlines_stays_one_event(self):
        keys, _ = decode_keys("\x1b[200~line one\nline two\x1b[201~")
        self.assertEqual(len(keys), 1)
        self.assertIn("\n", keys[0].pasted)

    def test_mixed_input_decodes_in_order(self):
        self.assertEqual(names("a\x1b[Bb\r"), ["a", "down", "b", "enter"])

    def test_astral_characters_survive(self):
        keys, _ = decode_keys("😀")
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0].char, "😀")

    def test_incomplete_escape_is_held_for_the_next_read(self):
        keys, rest = decode_keys("\x1b[")
        self.assertEqual(keys, [])
        self.assertEqual(rest, "\x1b[")

    def test_split_escape_sequence_reassembles(self):
        # A multi-byte key can straddle two reads; the carried remainder must
        # decode correctly once the rest arrives.
        first, rest = decode_keys("ab\x1b[")
        self.assertEqual([k.name for k in first], ["a", "b"])
        second, tail = decode_keys(rest + "A")
        self.assertEqual([k.name for k in second], ["up"])
        self.assertEqual(tail, "")


class DisplayWidth(unittest.TestCase):
    def test_ascii_is_one_column_each(self):
        self.assertEqual(display_width("hello"), 5)

    def test_ansi_codes_take_no_space(self):
        self.assertEqual(display_width("\x1b[31mhello\x1b[0m"), 5)

    def test_cjk_takes_two_columns(self):
        self.assertEqual(display_width("日本語"), 6)

    def test_truncate_respects_width(self):
        self.assertLessEqual(display_width(truncate("abcdefghij", 5)), 5)

    def test_fit_pads_and_truncates_exactly(self):
        self.assertEqual(display_width(fit("ab", 6)), 6)
        self.assertEqual(display_width(fit("abcdefghij", 6)), 6)

    def test_fit_is_exact_for_wide_characters(self):
        self.assertEqual(display_width(fit("日本語です", 7)), 7)

    def test_paint_is_a_noop_when_colour_disabled(self):
        self.assertEqual(paint("x", fg=1), "x")


class Formatting(unittest.TestCase):
    NOW = 1785600000.0

    def test_relative_time_is_compact(self):
        self.assertEqual(relative_time(self.NOW - 10, self.NOW), "now")
        self.assertEqual(relative_time(self.NOW - 5 * 60, self.NOW), "5m")
        self.assertEqual(relative_time(self.NOW - 3 * 3600, self.NOW), "3h")
        self.assertEqual(relative_time(self.NOW - 2 * 86400, self.NOW), "2d")
        self.assertEqual(relative_time(self.NOW - 400 * 86400, self.NOW), "1y")

    def test_byte_and_count_formatting(self):
        self.assertEqual(bytes_(512), "512B")
        self.assertEqual(bytes_(2048), "2.0K")
        self.assertEqual(number(250), "250")
        self.assertEqual(number(88_000), "88k")
        self.assertEqual(number(1_250_000), "1.3M")

    def test_time_bucket_is_calendar_based(self):
        day = 86400
        self.assertEqual(time_bucket(self.NOW - 10, self.NOW), "Today")
        self.assertEqual(time_bucket(self.NOW - day, self.NOW), "Yesterday")
        self.assertEqual(time_bucket(self.NOW - 3 * day, self.NOW), "Past week")
        self.assertEqual(time_bucket(self.NOW - 20 * day, self.NOW), "Past month")
        self.assertEqual(time_bucket(self.NOW - 100 * day, self.NOW), "Past year")
        self.assertEqual(time_bucket(self.NOW - 500 * day, self.NOW), "Older")
        self.assertEqual(time_bucket(0, self.NOW), "Older")

    def test_duration_reads_naturally(self):
        self.assertEqual(duration(self.NOW, self.NOW + 30), "<1m")
        self.assertEqual(duration(self.NOW, self.NOW + 90 * 60), "1h 30m")

    def test_paths_collapse_home_and_elide_from_the_left(self):
        self.assertEqual(short_path("/home/alice/dev/api", "/home/alice"), "~/dev/api")
        long = short_path("/home/alice/a/b/c/d/e/f/g/h", "/home/alice", 14)
        self.assertTrue(long.startswith("…/"))
        self.assertIn("h", long)


if __name__ == "__main__":
    unittest.main()
