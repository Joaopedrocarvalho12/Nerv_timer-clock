#!/usr/bin/env python3
"""
NERV-style terminal clock for the kitty terminal (v2).
"""

import os
import sys
import time
import select
import shutil
import signal
import termios
import tty
import fcntl
import struct
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
BACKGROUND = os.path.join(HERE, "nerv_bgPM.png")

# Fractional bounding box of the LCD panel within background.png
# (measured from the original NERV Timer SVG frame; scale-invariant).
LCD_X0, LCD_X1 = 0.1969, 0.7777
LCD_Y0, LCD_Y1 = 0.30, 0.5910

RED_BOLD = "\x1b[38;2;255;30;30m"
RESET = "\x1b[0m"
HIDE_CUR = "\x1b[?25l"
SHOW_CUR = "\x1b[?25h"
ALT_ON = "\x1b[?1049h"
ALT_OFF = "\x1b[?1049l"

_resize_pending = True  # trigger an initial layout on first loop iteration


def _on_resize(signum, frame):
    global _resize_pending
    _resize_pending = True


def get_term_pixels():
    buf = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
    rows, cols, xpix, ypix = struct.unpack("HHHH", buf)
    if xpix == 0 or ypix == 0:
        xpix, ypix = cols * 8, rows * 17
    return cols, rows, xpix, ypix


def show_background(cols, rows):
    subprocess.run(
        [
            "kitten", "icat",
            "--stdin=no",
            "--transfer-mode=file",
            f"--place={cols}x{rows}@0x0",
            "--z-index=-1",
            "--align=left",
            "--scale-up",
            BACKGROUND,
        ],
        check=False,
    )


def move(row, col):
    sys.stdout.write(f"\x1b[{row};{col}H")


def clear_rect(row0, col0, height, width):
    blank = " " * max(0, width)
    for r in range(height):
        move(row0 + r, col0)
        sys.stdout.write(blank)


def render_big(text, row, col, scale):
    """Draw `text` at `scale`x size using kitty's OSC 66 text-sizing protocol."""
    move(row, col)
    sys.stdout.write(f"{RED_BOLD}\x1b]66;s={scale};{text}\x07{RESET}")


def fmt_hms(total_seconds):
    total_seconds = max(0, int(total_seconds))
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


class RawInput:
    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def get_key(self, timeout=0.1):
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            return sys.stdin.read(1)
        return None


def main():
    global _resize_pending

    if shutil.which("kitten") is None:
        print("Este script precisa rodar dentro do kitty (comando 'kitten' não encontrado).")
        sys.exit(1)
    if not os.path.exists(BACKGROUND):
        print(f"Não achei {BACKGROUND} — coloque background.png ao lado deste script.")
        sys.exit(1)

    signal.signal(signal.SIGWINCH, _on_resize)

    sys.stdout.write(ALT_ON + HIDE_CUR + "\x1b[2J")
    sys.stdout.flush()

    layout = {}

    def recompute_layout():
        cols, rows, xpix, ypix = get_term_pixels()
        sys.stdout.write("\x1b[2J")
        sys.stdout.flush()
        show_background(cols, rows)
        cell_w = xpix / cols
        cell_h = ypix / rows
        layout["cols"], layout["rows"] = cols, rows
        layout["lcd_col0"] = int((LCD_X0 * xpix) / cell_w) + 1
        layout["lcd_col1"] = int((LCD_X1 * xpix) / cell_w) + 1
        layout["lcd_row0"] = int((LCD_Y0 * ypix) / cell_h) + 1
        layout["lcd_row1"] = int((LCD_Y1 * ypix) / cell_h) + 1
        layout["lcd_w"] = max(10, layout["lcd_col1"] - layout["lcd_col0"])
        layout["lcd_h"] = max(6, layout["lcd_row1"] - layout["lcd_row0"])

    try:
        mode = 1
        crono_running = False
        crono_elapsed = 0.0
        crono_start_ts = None

        count_target = 300
        count_running = False
        count_remaining = float(count_target)
        count_last_ts = None

        with RawInput() as ri:
            last_text = None
            while True:
                now = time.time()

                if _resize_pending:
                    recompute_layout()
                    _resize_pending = False
                    last_text = None

                if mode == 1:
                    text = time.strftime("%H:%M:%S")
                    status = time.strftime("%Y-%m-%d")
                elif mode == 2:
                    disp = crono_elapsed + (now - crono_start_ts) if crono_running else crono_elapsed
                    text = fmt_hms(disp)
                    status = "RUNNING" if crono_running else "PAUSED"
                else:
                    if count_running:
                        delta = now - count_last_ts
                        count_remaining = max(0.0, count_remaining - delta)
                        count_last_ts = now
                        if count_remaining <= 0:
                            count_running = False
                    text = fmt_hms(count_remaining)
                    status = "RUNNING" if count_running else ("DONE" if count_remaining <= 0 else "PAUSED")

                if text != last_text:
                    lcd_col0, lcd_row0 = layout["lcd_col0"], layout["lcd_row0"]
                    lcd_w, lcd_h = layout["lcd_w"], layout["lcd_h"]
                    cols, rows = layout["cols"], layout["rows"]

                    scale_w = lcd_w // len(text)
                    scale = max(1, min(scale_w, lcd_h))
                    text_w = len(text) * scale
                    start_col = lcd_col0 + max(0, (lcd_w - text_w) // 2)
                    start_row = lcd_row0 + max(0, (lcd_h - scale) // 2)

                    clear_rect(lcd_row0, lcd_col0, lcd_h, lcd_w)
                    render_big(text, start_row, start_col, scale)

                    status_row = lcd_row0 + lcd_h + 2
                    clear_rect(status_row, lcd_col0, 1, lcd_w)
                    move(status_row, lcd_col0 + max(0, (lcd_w - len(status)) // 2))
                    sys.stdout.write(RED_BOLD + status + RESET)

                    # Button bar: [ CLOCK ] [ CRONO ] [ COUNT ] [ RESET ]
                    buttons = [("CLOCK", mode == 1), ("CRONO", mode == 2),
                               ("COUNT", mode == 3), ("RESET", False)]
                    btn_row0 = status_row + 3
                    clear_rect(btn_row0, 1, 3, cols - 1)

                    labels = [f" {name} " for name, _ in buttons]
                    gap = 2
                    total_w = sum(len(l) + 2 for l in labels) + gap * (len(labels) - 1)
                    x = max(1, (cols - total_w) // 2)
                    for (name, active), label in zip(buttons, labels):
                        top = "┌" + "─" * len(label) + "┐"
                        mid = "│" + label + "│"
                        bot = "└" + "─" * len(label) + "┘"
                        color = "\x1b[7m" + RED_BOLD if active else RED_BOLD
                        move(btn_row0, x)
                        sys.stdout.write(RED_BOLD + top + RESET)
                        move(btn_row0 + 1, x)
                        sys.stdout.write(color + mid + RESET)
                        move(btn_row0 + 2, x)
                        sys.stdout.write(RED_BOLD + bot + RESET)
                        x += len(top) + gap

                    hint_row = rows - 1
                    clear_rect(hint_row, 1, 1, cols - 1)
                    hints = "1/2/3 modo  SPACE inicia/pausa  r reseta  +/- ajusta  q sai"
                    move(hint_row, max(1, (cols - len(hints)) // 2))
                    sys.stdout.write(RED_BOLD + hints[: cols - 2] + RESET)
                    sys.stdout.flush()
                    last_text = text

                key = ri.get_key(timeout=0.2)
                if key == "q":
                    break
                elif key == "1":
                    mode, last_text = 1, None
                elif key == "2":
                    mode, last_text = 2, None
                elif key == "3":
                    mode, last_text = 3, None
                elif key == " ":
                    if mode == 2:
                        if crono_running:
                            crono_elapsed += now - crono_start_ts
                            crono_running = False
                        else:
                            crono_start_ts = now
                            crono_running = True
                    elif mode == 3:
                        if count_running:
                            count_running = False
                        else:
                            if count_remaining <= 0:
                                count_remaining = float(count_target)
                            count_last_ts = now
                            count_running = True
                    last_text = None
                elif key == "r":
                    if mode == 2:
                        crono_running = False
                        crono_elapsed = 0.0
                    elif mode == 3:
                        count_running = False
                        count_remaining = float(count_target)
                    last_text = None
                elif key in ("+", "="):
                    if mode == 3 and not count_running:
                        count_target += 30
                        count_remaining = float(count_target)
                        last_text = None
                elif key == "-":
                    if mode == 3 and not count_running:
                        count_target = max(30, count_target - 30)
                        count_remaining = float(count_target)
                        last_text = None
    finally:
        sys.stdout.write(SHOW_CUR + ALT_OFF)
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.stdout.write(SHOW_CUR + ALT_OFF)
        sys.exit(0)