#!/bin/sh
set -e

# Initialize challenge directory if empty
if [ -z "$(ls -A /challenge 2>/dev/null)" ]; then
    echo "Initializing /challenge directory..."
    cp -r /app/challenge/* /challenge/
fi

# Initialize challenge_cache directory if empty
if [ -z "$(ls -A /challenge_cache 2>/dev/null)" ]; then
    echo "Initializing /challenge_cache directory..."
    cp -r /app/challenge_cache/* /challenge_cache/
fi

echo "Starting Flask application..."
exec flask run --host=0.0.0.0
