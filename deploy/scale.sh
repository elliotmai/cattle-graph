#!/usr/bin/env bash
# Port of scale.ps1. Where the Windows version launched four console windows,
# systemd owns the processes now -- so this is just the enable line, kept as a
# script so there is still one obvious command.
#
# ANGUS is deliberately absent: angus.org serves a JS challenge that a plain
# HTTP client cannot clear (see angus.py), so its frontier is empty and the
# service would restart-loop. Re-add it once that adapter works.
set -euo pipefail
sudo systemctl enable --now crawl@CHIA crawl@MAINE crawl@SHORT
sudo systemctl enable --now cattle-dashboard
# The board is fed by a timer, not a daemon: one POST a minute.
sudo systemctl enable --now cattle-publish.timer
systemctl --no-pager --plain list-units 'crawl@*' cattle-dashboard.service
systemctl --no-pager --plain list-timers cattle-publish.timer
