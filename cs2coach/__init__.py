"""CS2 Coach - a read-only coaching assistant built on CS2 Game State Integration.

Safety model
------------
* Data comes ONLY from Valve's official Game State Integration (GSI), which
  CS2 itself pushes over HTTP to localhost. There is no memory reading, no DLL
  injection, no packet sniffing and no input simulation.
* The overlay is an ordinary separate OS window. It never draws into the game.
"""

__version__ = "0.1.0"
