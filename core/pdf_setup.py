"""How every PDF the app draws is written (report cards, receipts, timetables). Imported by each PDF module.

reportlab writes each page's drawing compressed and then, by default, spelled out again in printable characters
(ASCII85), which makes the file a quarter larger and costs time to write; a PDF does not need it. Without it, a class's
report cards are about a sixth smaller to send, and quicker to make. rl_accel (requirements.txt) gives reportlab its
C speed-ups for the rest.
"""

from reportlab import rl_config

rl_config.useA85 = 0
