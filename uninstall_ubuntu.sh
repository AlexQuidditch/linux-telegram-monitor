#!/bin/bash
set -e
sudo systemctl stop linux-telegram-monitor
sudo rm /etc/systemd/system/linux-telegram-monitor.service
