#!/bin/bash

echo "================================"
echo "  Video Bot - Termux Setup"
echo "================================"

# Update packages
echo "[1/4] Updating packages..."
pkg update -y && pkg upgrade -y

# Install Python if not installed
echo "[2/4] Installing Python..."
pkg install python -y

# Install pip packages
echo "[3/4] Installing Python dependencies..."
pip install -r requirements.txt

echo "[4/4] Done! Starting bot..."
echo ""
echo "================================"
echo "  Edit bot.py first:"
echo "  1. Set BOT_TOKEN"
echo "  2. Set ADMIN_ID"
echo "================================"
echo ""

python bot.py
