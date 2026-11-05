---
title: Chess Review Studio
emoji: ♞
colorFrom: indigo
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# ♞ Chess Review Studio

A chess.com-style **Game Review** application with **pluggable UCI chess engines**.
Paste any PGN (or fetch your latest Lichess games) and get a full engine review:
move classifications, accuracy scores, eval graph, key moments, time report and
coach comments. Includes a repertoire builder, drill trainer and board editor.

Stockfish 19 (Linux) is bundled at image build time; the pure-Python fallback
engine always works. Reviews run on a 2-CPU free Space — pick a *Fast/Medium*
depth preset for the snappiest results.

> Built as a local-first desktop app (`python run.py` on your own machine);
> this Space is the hosted instance.
