import docker
import docker.api.build
docker.api.build.process_dockerfile = lambda dockerfile, path: ('Dockerfile', dockerfile)
import urllib3
urllib3.disable_warnings()

from flask import Flask, request, jsonify, render_template, current_app, Response, stream_with_context
from flask_sqlalchemy import SQLAlchemy
import time
import requests
import json
import queue
from threading import Thread, Lock,  Condition


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
    user_id = db.Column(db.String(64), nullable=False)
    build_status = db.Column(db.String(64), nullable=False, default="queued")
    build_time_no_cache = db.Column(db.Float, nullable=True)
    build_time_with_cache = db.Column(db.Float, nullable=True)
    image_size = db.Column(db.Integer, nullable=True)
    is_valid = db.Column(db.Boolean, nullable=False, default=False)
    dockerfile_content = db.Column(db.Text, nullable=False)
    error = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f"<BuildResult {self.id}>"

with app.app_context():
    db.create_all()

def get_queue_order_message(queue):
    return {"queue": [item["id"] for item in queue]}


class Queue:
    def __init__(self):
        self.queue = []
        self.condition = Condition()
        self.lock = Condition()
        self.message = {"message": "Queue is empty"}
        # used to store messages to be sent to the client on new connection
        self.messages = []

    def put(self, item):
        with self.condition:
            # TODO replace based on user_id
            self.queue.append(item)
            self.message = get_queue_order_message(self.queue)
            with self.lock:
                self.lock.notify()
            self.condition.notify_all()

    def get(self):
        with self.lock:
            while not self.queue:
                self.lock.wait()
        with self.condition:
            item = self.queue.pop(0)
            self.messages = []
            self.message = get_queue_order_message(self.queue)
            self.condition.notify_all()
            return item

    def send_message(self, message):
        with self.condition:
            self.message = message
            self.messages.append(message)
            self.condition.notify_all()


# Queuing system for Docker builds
build_queue = Queue()


def process_build_queue():
    while True:
        with app.app_context():
            build_data = build_queue.get()
            if build_data is None:
                break

            dockerfile_content = build_data["dockerfile_content"]

            build_time_no_cache, build_time_with_cache, image_size, is_valid, error = (
                build_and_measure(dockerfile_content, build_data["id"])
            )

            # Update the database with the build results
            build_result = BuildResult.query.get(build_data["id"])
            build_result.build_time_no_cache = build_time_no_cache
            build_result.build_time_with_cache = build_time_with_cache
            build_result.image_size = image_size
            build_result.is_valid = is_valid
            build_result.build_status = "completed"
            build_result.error = error
            db.session.commit()
            current_app.logger.info(f"Build {build_data['id']} completed")
            # TODO full data?
            build_queue.send_message({"message": "Build completed", "id": build_data["id"]})


# Start a thread to process the build queue
Thread(target=process_build_queue, daemon=True).start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route("/admin/prune")
def prune():
    try:
        return jsonify({
            "images": str(client.images.prune()),
            "containers": str(client.containers.prune())
            }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/submit", methods=["POST"])
def submit_dockerfile():
    # Adjust if using user authentication
    user_id = "TODO"
    data = request.files.get("dockerfile")

    if not data:
        return jsonify({"error": "No Dockerfile provided"}), 400

    dockerfile_content = data.read().decode("utf-8")

    # Save initial build data to the database
    build_result = BuildResult(
        user_id=user_id,
        build_status="queued",
        build_time_no_cache=None,
        build_time_with_cache=None,
        image_size=None,
        is_valid=False,
        dockerfile_content=dockerfile_content,
    )
    db.session.add(build_result)
    db.session.commit()
    # Queue the build
    build_queue.put(
        {
            "user_id": user_id,
            "dockerfile_content": dockerfile_content,
            "id": build_result.id,
        }
    )
    current_app.logger.info(f"Build {build_result.id} queued")
    build_queue.send_message({"message": "Build queued", "id": build_result.id})


    return (
        jsonify(
            {"id": build_result.id}
        ),
        200,
    )

@app.route('/data')
def queue_updates():
    def event_stream():
        yield f"data: {json.dumps(get_queue_order_message(build_queue.queue))}\n\n"
        for message in build_queue.messages:
            yield f"data: {json.dumps(message)}\n\n"
        while True:
            with build_queue.condition:
                build_queue.condition.wait()
                yield f"data: {json.dumps(build_queue.message)}\n\n"
    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")


def build_and_measure(dockerfile_content, id):
    try:
        tag = f"build-{id}"
        current_app.logger.info(f"Building and measuring Dockerfile with tag {tag}")
        build_queue.send_message({"message": f"Build started", "id": id})
        # First build without cache
        start_time = time.time()
        img1, logs = client.images.build(
            path="/challenge",
            dockerfile=dockerfile_content,
            tag=tag,
            #nocache=True
        )
        end_time = time.time()
        build_time_no_cache = end_time - start_time
        current_app.logger.info(f"Build without cache took {build_time_no_cache} seconds")
        build_queue.send_message({"message": f"Build without cache took {round(build_time_no_cache, 1)} seconds", "id": id})
        time.sleep(0.5)

        image_size = img1.attrs["Size"]
        image_size_mb = round(image_size / (1024 * 1024), 2)
        current_app.logger.info(f"Image size: {image_size}")
        build_queue.send_message({"message": f"Image size: {image_size_mb} MB", "id": id})

        try:
            img1.remove(force=True)
        except Exception as e:
            pass
        # Second build with cache
        start_time = time.time()
        img2, logs = client.images.build(
            path="/challenge_cache",
            dockerfile=dockerfile_content, tag=tag
        )
        end_time = time.time()
        build_time_with_cache = end_time - start_time
        current_app.logger.info(f"Build with cache took {build_time_with_cache} seconds")
        build_queue.send_message({"message": f"Build with cache took {round(build_time_with_cache,1)} seconds", "id": id})
        time.sleep(0.5)

        is_valid, error = validate_container(id, img2)
        img2.remove(force=True)
        current_app.logger.info(f"Container is valid: {is_valid}")
        build_queue.send_message({"message": f"Container is valid: {is_valid}", "id": id})

        return build_time_no_cache, build_time_with_cache, image_size, is_valid, error

    except Exception as e:
        current_app.logger.info(e)
        return None, None, None, False, str(e)


def validate_container(id, image):
    is_valid = False
    error = None
    tag = f"build-{id}"
    try:
        current_app.logger.info(f"Starting container with tag {tag}")
        build_queue.send_message({"message": f"Starting container", "id": id})
        container = client.containers.run(
            image=image,
            detach=True, 
            security_opt=["no-new-privileges"],
            cpu_shares=512,
            mem_limit="512m",
            environment={"PORT": "8000"},
            ports={"8000/tcp": 8000},
            name=tag,
        )
        time.sleep(5)
        current_app.logger.info(f"Validating container with tag {tag}")
        build_queue.send_message({"message": f"Validating container", "id": id})
        response = requests.get("http://dind:8000")
        is_valid = response.status_code == 200 and '<div id="app"></div>' in response.text
        if not is_valid:
            error = response.status_code
    except requests.exceptions.ConnectionError:
        error = "Connection error on port 8000"
    except Exception as e:
        error = "Error starting container"
    finally:
        time.sleep(1)
        container.remove(v=True, force=True)
        return is_valid, error


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
