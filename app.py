import docker
from flask import Flask, request, jsonify, render_template, current_app
from flask_sqlalchemy import SQLAlchemy
import time
from io import BytesIO
import requests
import queue
from threading import Thread, Lock

app = Flask(__name__)
tls_config = docker.tls.TLSConfig(
    client_cert=('/certs/client/cert.pem', '/certs/client/key.pem'),
    ca_cert='/certs/client/ca.pem',
    verify=False
)
client = docker.DockerClient(base_url="tcp://dind:2376", tls=tls_config)

# Configure Database URI: Adjust as needed
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////data/results.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


class BuildResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(64), nullable=True)
    build_status = db.Column(db.String(64), nullable=False, default="queued")
    build_time_no_cache = db.Column(db.Float, nullable=False)
    build_time_with_cache = db.Column(db.Float, nullable=False)
    image_size = db.Column(db.Integer, nullable=False)
    is_valid = db.Column(db.Boolean, nullable=False)
    dockerfile_content = db.Column(db.Text, nullable=False)

    def __repr__(self):
        return f"<BuildResult {self.id}>"

with app.app_context():
    db.create_all()


class QueueWithPosition:
    def __init__(self):
        self.queue = queue.Queue()
        self.order = []
        self.lock = Lock()

    def put(self, item):
        with self.lock:
            self.queue.put(item)
            self.order.append(item)
        return self.get_position(item)

    def get(self):
        item = self.queue.get()
        with self.lock:
            self.order.remove(item)
        return item

    def get_position(self, item):
        with self.lock:
            try:
                return (
                    self.order.index(item) + 1
                )  # +1 because we want position to start from 1, not 0
            except ValueError:
                return None

    def task_done(self):
        self.queue.task_done()


# Queuing system for Docker builds
build_queue = QueueWithPosition()


def process_build_queue():
    while True:
        build_data = build_queue.get()
        if build_data is None:
            break

        tag = f"build-{build_data["id"]}"
        dockerfile_content = build_data["dockerfile_content"]

        build_time_no_cache, build_time_with_cache, image_size, is_valid = (
            build_and_measure(dockerfile_content, tag)
        )

        # Update the database with the build results
        build_result = BuildResult.query.get(build_data["id"])
        build_result.build_time_no_cache = build_time_no_cache
        build_result.build_time_with_cache = build_time_with_cache
        build_result.image_size = image_size
        build_result.is_valid = is_valid
        build_result.build_status = "completed"
        db.session.commit()

        build_queue.task_done()


# Start a thread to process the build queue
Thread(target=process_build_queue, daemon=True).start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route("/submit", methods=["POST"])
def submit_dockerfile():
    #user_id = request.form.get("user_id")  # Adjust if using user authentication
    user_id = "TODO"
    current_app.logger.info("request")
    current_app.logger.info(request.files.get("dockerfile"))
    data = request.files.get("dockerfile")

    if not data:
        return jsonify({"error": "No Dockerfile provided"}), 400

    dockerfile_content = data.read().decode("utf-8")

    # Save initial build data to the database
    build_result = BuildResult(
        user_id=user_id,
        build_status="queued",
        build_time_no_cache=0,
        build_time_with_cache=0,
        image_size=0,
        is_valid=False,
        dockerfile_content=dockerfile_content,
    )
    db.session.add(build_result)
    db.session.commit()

    # Queue the build
    position = build_queue.put(
        {
            "user_id": user_id,
            "dockerfile_content": dockerfile_content,
            "id": build_result.id,
        }
    )

    return (
        jsonify(
            {"message": "Build queued", "id": build_result.id, "position": position}
        ),
        200,
    )


def build_and_measure(dockerfile_content, tag):
    try:
        # First build without cache
        start_time = time.time()
        img, logs = client.images.build(
            path="/challenge",
            fileobj=BytesIO(dockerfile_content.encode("utf-8")), tag=tag, nocache=True
        )
        end_time = time.time()
        build_time_no_cache = end_time - start_time

        img = client.images.get(tag)
        image_size = img.attrs["Size"]

        # Second build with cache
        start_time = time.time()
        img, logs = client.images.build(
            path="/challenge",
            fileobj=BytesIO(dockerfile_content.encode("utf-8")), tag=tag
        )
        end_time = time.time()
        build_time_with_cache = end_time - start_time

        # Run container with security options for validation
        container = client.containers.run(
            tag,
            detach=True,
            security_opt=["no-new-privileges"],
            cpus=0.5,
            mem_limit="512m",
            readonly_rootfs=True,
        )
        is_valid = validate_container(tag)
        container.stop()
        container.remove()

        return build_time_no_cache, build_time_with_cache, image_size, is_valid

    except Exception as e:
        print(f"Error: {e}")
        return None, None, None, False


def validate_container(tag):
    try:
        container = client.containers.run(tag, detach=True, environment={"PORT": "8000"}, ports={"80/tcp": 8000}), 
        response = requests.get("http://dind:8000")
        container.stop()
        container.remove()
        return response.status_code == 200
    except requests.ConnectionError:
        return False


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
