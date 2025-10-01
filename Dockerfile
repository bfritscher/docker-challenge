# Dockerfile for Flask App
FROM python:3.13-slim

# Set up working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Copy the Flask app code and directories
COPY challenge challenge
COPY challenge_cache challenge_cache
COPY templates templates
COPY app.py ./

# Expose the application port
EXPOSE 5000

# Environment variables for Flask
ENV FLASK_APP=app.py

# Start the Flask app with entrypoint script
ENTRYPOINT ["/entrypoint.sh"]
