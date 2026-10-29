#!/bin/sh
set -eu
mkdir -p /logs/verifier
chown root:root /logs/verifier
chmod 755 /logs/verifier
chmod -R go-rwx /tests
printf '0\n' > /logs/verifier/reward.txt
printf '{"reward": 0}\n' > /logs/verifier/reward.json
if python3 -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA; then
    printf '1\n' > /logs/verifier/reward.txt
    printf '{"reward": 1}\n' > /logs/verifier/reward.json
fi
chmod 644 /logs/verifier/reward.txt /logs/verifier/reward.json
