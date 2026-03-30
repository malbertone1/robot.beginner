import gpiod
import time
import signal
import sys

chip = gpiod.Chip('/dev/gpiochip0')
lines = chip.request_lines(
    {
        16: gpiod.LineSettings(direction=gpiod.line.Direction.OUTPUT, output_value=gpiod.line.Value.INACTIVE),
        20: gpiod.LineSettings(direction=gpiod.line.Direction.OUTPUT, output_value=gpiod.line.Value.INACTIVE),
    },
    consumer='mosfet-safety'
)

signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
while True:
    time.sleep(60)
