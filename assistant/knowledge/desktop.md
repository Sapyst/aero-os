# AeroOS desktop

## Restarting desktop parts
The taskbar is aerobar, the dock is dock, and notifications run through hermesd under aero-supervise. Background service versions are chosen in ~/.config/aero/daemons.conf.

## Local assistant
aero-assistant stop closes the local model. It also closes by itself after 5 idle minutes.

## Storage rules
/tmp is RAM and limited to 2 GB. Large temporary files go to ~/.cache/aero-temp, removed after 7 days. aero-bg runs a command at background priority.
