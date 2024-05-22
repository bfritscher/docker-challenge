# Dockerfile for Flask App
FROM python:3.12-slim

# Set up working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt

# Copy the Flask app code
COPY . .

# Expose the application port
EXPOSE 5000

# Environment variables for Flask
ENV FLASK_APP=app.py
ENV FLASK_ENV=production

# Start the Flask app
CMD ["flask", "run", "--host=0.0.0.0"]
