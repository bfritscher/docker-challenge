import docker
import docker.api.build
from functools import wraps

docker.api.build.process_dockerfile = lambda dockerfile, path: (
    "Dockerfile",
    dockerfile,
)
import urllib3

urllib3.disable_warnings()

import jwt

from flask import (
    Flask,
    request,
    redirect,
    session,
    jsonify,
    render_template,
    current_app,
    Response,
    stream_with_context,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
import time
import requests
import json
import queue
from threading import Thread, Lock, Condition
from datetime import datetime
import os

JWT_SECRET = os.getenv("JWT_SECRET")
SESSION_SECRET = os.getenv("SESSION_SECRET")

app = Flask(__name__)
app.secret_key = SESSION_SECRET
tls_config = docker.tls.TLSConfig(
    client_cert=("/certs/client/cert.pem", "/certs/client/key.pem"),
    ca_cert="/certs/client/ca.pem",
    verify=False,
)
client = docker.DockerClient(base_url="tcp://dind:2376", tls=tls_config)

# Configure Database URI: Adjust as needed
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////data/results.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


class BuildResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship("User", lazy="joined")  # Relationship
    build_status = db.Column(db.String(64), nullable=False, default="queued")
    build_time_no_cache = db.Column(db.Float, nullable=True)
    build_time_with_cache = db.Column(db.Float, nullable=True)
    image_size = db.Column(db.Integer, nullable=True)
    is_valid = db.Column(db.Boolean, nullable=False, default=False)
    dockerfile_content = db.Column(db.Text, nullable=False)
    error = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<BuildResult {self.id}>"

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    firstname = db.Column(db.String(50), nullable=False)
    lastname = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    affiliation = db.Column(db.String(120), nullable=True)
    uniqueID = db.Column(db.String(50), unique=True, nullable=False)


with app.app_context():
    db.create_all()


def authentication_required():
    response = jsonify({"error": "Authentication required"})
    response.status_code = 401  # Unauthorized
    return response

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return authentication_required()
        return f(*args, **kwargs)
    return decorated_function

def cancel_item(id):
    with app.app_context():
        build_result = BuildResult.query.get(id)
        build_result.build_status = "canceled"
        db.session.commit()
        current_app.logger.info(f"Build {id} canceled")


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

    def find_item_by_user_id(self, user_id):
        for i, item in enumerate(self.queue):
            if item["user_id"] == user_id:
                return i
        return -1

    def put(self, item):
        with self.condition:
            index = self.find_item_by_user_id(item["user_id"])
            if index >= 0:
                previous_item = self.queue[index]
                cancel_item(previous_item["id"])
                self.queue[index] = item
            else:
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
            build_queue.send_message(
                {"message": "Build completed", "id": build_data["id"]}
            )


# Start a thread to process the build queue
Thread(target=process_build_queue, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html", user_id=session.get('user_id'))


@app.route("/logout")
def logout():
    session.pop('user_id', None)
    return redirect("/")


@app.route("/login", methods=["POST"])
def login_handle():
    jwt_token = request.values.get("jwt")
    app.logger.info(f"Received JWT token: {jwt_token}")
    try:
        payload = jwt.decode(jwt_token, JWT_SECRET, algorithms=['HS256'], leeway=10)
    except jwt.InvalidTokenError:
        return "Invalid token", 400

    email = payload.get('email')
    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(
            firstname=payload.get('firstname'),
            lastname=payload.get('lastname'),
            email=email,
            affiliation=payload.get('affiliation'),
            uniqueID=payload.get('uniqueID')
        )
        db.session.add(user)
        db.session.commit()

    app.logger.info(f"User {payload} logged in")
    session['user_id'] = user.id

    return redirect("/")


@app.route("/admin/prune")
def prune():
    try:
        return (
            jsonify(
                {
                    "images": str(client.images.prune()),
                    "containers": str(client.containers.prune()),
                }
            ),
            200,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/submit", methods=["POST"])
@login_required
def submit_dockerfile():
    data = request.files.get("dockerfile")

    if not data:
        return jsonify({"error": "No Dockerfile provided"}), 400

    dockerfile_content = data.read().decode("utf-8")

    # Save initial build data to the database
    build_result = BuildResult(
        user_id=session['user_id'],
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
            "user_id": session['user_id'],
            "dockerfile_content": dockerfile_content,
            "id": build_result.id,
        }
    )
    current_app.logger.info(f"Build {build_result.id} queued")
    build_queue.send_message({"message": "Build queued", "id": build_result.id})

    return (
        jsonify({"id": build_result.id}),
        200,
    )


@app.route("/data")
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


def size_to_mb(image_size):
    image_size_mb = round(image_size / (1024 * 1024), 2)
    return f"{image_size_mb} MB"


def build_and_measure(dockerfile_content, id):
    try:
        tag = f"build-{id}"
        current_app.logger.info(f"Building and measuring Dockerfile with tag {tag}")
        build_queue.send_message({"message": f"Build started", "id": id})
        # First build without cache
        start_time = time.time()
        img1, logs = client.images.build(
            path="/challenge", dockerfile=dockerfile_content, tag=tag, nocache=True
        )
        end_time = time.time()
        build_time_no_cache = end_time - start_time
        current_app.logger.info(
            f"Build without cache took {build_time_no_cache} seconds"
        )
        build_queue.send_message(
            {
                "message": f"Build without cache took {round(build_time_no_cache, 1)} seconds",
                "id": id,
            }
        )
        time.sleep(0.5)

        image_size = img1.attrs["Size"]
        current_app.logger.info(f"Image size: {image_size}")
        build_queue.send_message(
            {"message": f"Image size: {size_to_mb(image_size)}", "id": id}
        )

        try:
            img1.remove(force=True)
        except Exception as e:
            pass
        # Second build with cache
        start_time = time.time()
        img2, logs = client.images.build(
            path="/challenge_cache", dockerfile=dockerfile_content, tag=tag
        )
        end_time = time.time()
        build_time_with_cache = end_time - start_time
        current_app.logger.info(
            f"Build with cache took {build_time_with_cache} seconds"
        )
        build_queue.send_message(
            {
                "message": f"Build with cache took {round(build_time_with_cache,1)} seconds",
                "id": id,
            }
        )
        time.sleep(0.5)

        is_valid, error = validate_container(id, img2)
        img2.remove(force=True)
        current_app.logger.info(f"Container is valid: {is_valid}")
        build_queue.send_message(
            {"message": f"Container is valid: {is_valid}", "id": id}
        )

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
        is_valid = (
            response.status_code == 200 and '<div id="app"></div>' in response.text
        )
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


# Baseline values
baseline_min_image_size = 0
baseline_max_image_size = 1000 * 1024 * 1024
baseline_min_build_time_no_cache = 0
baseline_max_build_time_no_cache = 1200
baseline_min_build_time_with_cache = 0
baseline_max_build_time_with_cache = 1200

# Weights
weight_image_size = 0.7
weight_build_time_with_cache = 0.2
weight_build_time_no_cache = 0.1


@app.route("/scores", methods=["GET"])
def get_scores():
    # max score for a user
    score_subquery = (
        db.session.query(
            BuildResult.id,
            BuildResult.user_id,
            (
                weight_image_size
                * (
                    1
                    - (BuildResult.image_size - baseline_min_image_size)
                    / (baseline_max_image_size - baseline_min_image_size)
                )
                + weight_build_time_with_cache
                * (
                    1
                    - (
                        BuildResult.build_time_with_cache
                        - baseline_min_build_time_with_cache
                    )
                    / (
                        baseline_max_build_time_with_cache
                        - baseline_min_build_time_with_cache
                    )
                )
                + weight_build_time_no_cache
                * (
                    1
                    - (
                        BuildResult.build_time_no_cache
                        - baseline_min_build_time_no_cache
                    )
                    / (
                        baseline_max_build_time_no_cache
                        - baseline_min_build_time_no_cache
                    )
                )
            ).label("score"),
        )
        .filter(
            BuildResult.build_time_with_cache.isnot(None),
            BuildResult.image_size.isnot(None),
            BuildResult.build_time_no_cache.isnot(None),
            BuildResult.build_status == "completed",
            BuildResult.is_valid == True,
        )
        .subquery()
    )

    # Subquery to find the maximum score per user_id
    max_score_subquery = (
        db.session.query(
            score_subquery.c.user_id,
            func.max(score_subquery.c.score).label("max_score"),
        )
        .group_by(score_subquery.c.user_id)
        .subquery()
    )

    # Subquery to count total attempts including failed ones
    total_attempts_subquery = (
        db.session.query(
            BuildResult.user_id, func.count(BuildResult.id).label("total_attempts")
        )
        .group_by(BuildResult.user_id)
        .subquery()
    )
    # maybe filter out cancelled builds?

    # Join subqueries to get details with the highest score per user_id and total attempts
    best_results = (
        db.session.query(
            BuildResult.id,
            BuildResult.user_id,
            User.firstname,
            BuildResult.build_time_no_cache,
            BuildResult.build_time_with_cache,
            BuildResult.image_size,
            BuildResult.updated_at,
            score_subquery.c.score,
            total_attempts_subquery.c.total_attempts,
        )
        .join(User, User.id == BuildResult.user_id)
        .join(score_subquery, score_subquery.c.id == BuildResult.id)
        .join(
            total_attempts_subquery,
            BuildResult.user_id == total_attempts_subquery.c.user_id,
        )
        .join(
            max_score_subquery,
            (BuildResult.user_id == max_score_subquery.c.user_id)
            & (score_subquery.c.score == max_score_subquery.c.max_score),
        )
        .order_by(score_subquery.c.score.desc())
        .all()
    )

    scores = []
    for (
        id,
        user_id,
        firstname,
        build_time_no_cache,
        build_time_with_cache,
        image_size,
        updated_at,
        score,
        total_attempts,
    ) in best_results:
        scores.append(
            {
                "id": id,
                "user_id": user_id,
                "firstname": firstname,
                "build_time_no_cache": round(build_time_no_cache, 1),
                "build_time_with_cache": round(build_time_with_cache, 1),
                "image_size": size_to_mb(image_size),
                'updated_at': updated_at,
                "score": round(score * 1000),
                "total_attempts": total_attempts,
            }
        )

    return jsonify(scores)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
