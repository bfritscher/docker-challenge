import docker
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
import time
from io import BytesIO
import requests
import uuid
import queue
from threading import Thread

app = Flask(__name__)
client = docker.DockerClient(base_url='tcp://dind:2375')

# Configure Database URI: Adjust as needed
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///data/results.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class BuildResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(64), nullable=True)  # Optional: Add user_id to identify the user
    build_status = db.Column(db.String(64), nullable=False, default='pending')
    build_time_no_cache = db.Column(db.Float, nullable=False)
    build_time_with_cache = db.Column(db.Float, nullable=False)
    image_size = db.Column(db.Integer, nullable=False)
    is_valid = db.Column(db.Boolean, nullable=False)
    tag = db.Column(db.String(128), nullable=False)

    def __repr__(self):
        return f"<BuildResult {self.id}>"

db.create_all()

# Generate a unique tag for each build
def generate_unique_tag(user_id=None):
    unique_id = uuid.uuid4().hex
    if user_id:
        return f"{user_id}_{unique_id}"
    return unique_id

# Queuing system for Docker builds
build_queue = queue.Queue()

def queue_build(build_data):
    build_queue.put(build_data)

def process_build_queue():
    while True:
        build_data = build_queue.get()
        if build_data is None:
            break
        
        tag = build_data['tag']
        dockerfile_content = build_data['dockerfile_content']
        
        build_time_no_cache, build_time_with_cache, image_size, is_valid = build_and_measure(dockerfile_content, tag)
        
        # Update the database with the build results
        build_result = BuildResult.query.filter_by(tag=tag).first()
        build_result.build_time_no_cache = build_time_no_cache
        build_result.build_time_with_cache = build_time_with_cache
        build_result.image_size = image_size
        build_result.is_valid = is_valid
        build_result.build_status = 'completed'
        db.session.commit()
        
        build_queue.task_done()

# Start a thread to process the build queue
Thread(target=process_build_queue, daemon=True).start()

@app.route('/submit', methods=['POST'])
def submit_dockerfile():
    user_id = request.form.get('user_id')  # Adjust if using user authentication
    data = request.files.get('dockerfile')
    
    if not data:
        return jsonify({'error': 'No Dockerfile provided'}), 400

    dockerfile_content = data.read().decode('utf-8')
    unique_tag = generate_unique_tag(user_id)

    # Save initial build data to the database
    build_result = BuildResult(
        user_id=user_id,
        build_status='queued',
        build_time_no_cache=0,
        build_time_with_cache=0,
        image_size=0,
        is_valid=False,
        tag=unique_tag
    )
    db.session.add(build_result)
    db.session.commit()

    # Queue the build
    queue_build({'user_id': user_id, 'dockerfile_content': dockerfile_content, 'tag': unique_tag})

    return jsonify({'message': 'Build queued', 'tag': unique_tag}), 200

def build_and_measure(dockerfile_content, tag):
    try:
        # First build without cache
        start_time = time.time()
        img, logs = client.images.build(fileobj=BytesIO(dockerfile_content.encode('utf-8')), tag=tag, nocache=True)
        end_time = time.time()
        build_time_no_cache = end_time - start_time

        img = client.images.get(tag)
        image_size = img.attrs['Size']

        # Second build with cache
        start_time = time.time()
        img, logs = client.images.build(fileobj=BytesIO(dockerfile_content.encode('utf-8')), tag=tag)
        end_time = time.time()
        build_time_with_cache = end_time - start_time

        # Run container with security options for validation
        container = client.containers.run(tag, detach=True, security_opt=["no-new-privileges"], cpus=0.5, mem_limit="512m", readonly_rootfs=True)
        is_valid = validate_container(tag)
        container.stop()
        container.remove()

        return build_time_no_cache, build_time_with_cache, image_size, is_valid

    except Exception as e:
        print(f"Error: {e}")
        return None, None, None, False

def validate_container(tag):
    try:
        container = client.containers.run(tag, detach=True, ports={'80/tcp': 8000})
        response = requests.get("http://dind:8000")
        container.stop()
        container.remove()
        return response.status_code == 200
    except requests.ConnectionError:
        return False

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
    