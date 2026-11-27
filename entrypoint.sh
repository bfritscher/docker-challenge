#!/bin/sh
set -e

# Initialize challenge directory if empty
if [ -z "$(ls -A /challenge 2>/dev/null)" ]; then
    echo "Initializing /challenge directory..."
    cp -r /app/challenge/* /challenge/
fi
cp /app/challenge/app.py /challenge/app.py

# Initialize challenge_cache directory if empty
if [ -z "$(ls -A /challenge_cache 2>/dev/null)" ]; then
    echo "Initializing /challenge_cache directory..."
    cp -r /app/challenge_cache/* /challenge_cache/
fi
cp /app/challenge_cache/app.py /challenge_cache/app.py

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

echo "Starting Flask application..."
exec python app.py
