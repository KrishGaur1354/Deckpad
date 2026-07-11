#!/bin/bash
# DeckPad launcher — add this file to Steam as a non-Steam game.
cd "$(dirname "$0")"
exec python3 deckpad_sender.py "$@"
