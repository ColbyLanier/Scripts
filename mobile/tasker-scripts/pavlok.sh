#!/data/data/com.termux/files/usr/bin/sh
# Trigger Pavlok stimulus via BLE from phone
# Called by MacroDroid via Termux:Tasker plugin
# Args: $1 = type (zap|beep|vibe), $2 = intensity (1-255)

TYPE="${1:-zap}"
INTENSITY="${2:-50}"

# Send intent to Pavlok app
am broadcast -a com.pavlok.intent.STIMULUS \
  --es type "$TYPE" \
  --ei intensity "$INTENSITY" \
  -n com.pavlok.shock/.receivers.StimulusReceiver 2>&1 || \
  echo "Intent failed, Pavlok app may not support direct intents"

echo "Pavlok: $TYPE at intensity $INTENSITY"
