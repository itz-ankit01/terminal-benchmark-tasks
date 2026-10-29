#!/bin/sh
set -eu
cp /solution/recovery.cpp /app/recovery.cpp
make -C /app
