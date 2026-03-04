#!/bin/bash
set -e

mkdir -p /data
cd /src
exec python3 -m app.main
