import docker
import docker.api.build
from collections import Counter
from contextlib import closing, contextmanager
from functools import wraps
from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPException
from io import BytesIO
from pathlib import Path, PurePosixPath
from random import SystemRandom
from tarfile import TarError, open as open_tar
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

docker.api.build.process_dockerfile = lambda dockerfile, path: (
    "Dockerfile",
    dockerfile,
)
import urllib3

urllib3.disable_warnings()

import click
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
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, desc, asc
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.sql.expression import over
import time
import json
import posixpath
from threading import Thread, Condition
from datetime import datetime, timezone
import os
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView
from flask_admin.theme import Bootstrap4Theme
from markupsafe import Markup

JWT_SECRET = os.getenv("JWT_SECRET")
SESSION_SECRET = os.getenv("SESSION_SECRET")
ADMIN_EMAILS = os.getenv("ADMIN_EMAILS", "").split(",")

BUILD_STATUS_QUEUED = "queued"
BUILD_STATUS_COMPLETED = "completed"
BUILD_STATUS_CANCELED = "canceled"
CHALLENGE_PATH = Path("/challenge")
WARM_CHALLENGE_PATH = Path("/challenge_cache")
MAX_PROBE_BODY = 2 * 1024 * 1024

app = Flask(__name__)
app.secret_key = SESSION_SECRET
tls_config = docker.tls.TLSConfig(
    client_cert=("/certs/client/cert.pem", "/certs/client/key.pem"),
    ca_cert="/certs/client/ca.pem",
    verify=False,
)
client = docker.DockerClient(base_url="tcp://dind:2376", tls=tls_config)

admin = Admin(app, theme=Bootstrap4Theme(swatch="yeti"))

# Configure Database URI: Adjust as needed
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////data/results.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


def get_queue_order_message(queue):
    return {"queue": [item["id"] for item in queue]}


def buildresult_to_queue_item(build_result):
    return {
        "user_id": build_result.user_id,
        "id": build_result.id,
    }



# Baseline values
baseline_min_image_size = 0
baseline_max_image_size = 800 * 1024 * 1024
baseline_min_build_time_no_cache = 0
baseline_max_build_time_no_cache = 60
baseline_min_build_time_with_cache = 0
baseline_max_build_time_with_cache = 140

# Weights
weight_image_size = 0.7
weight_build_time_with_cache = 0.2
weight_build_time_no_cache = 0.1

class BuildResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    user = db.relationship("User", lazy="joined", backref="build_results")  # Relationship
    build_status = db.Column(db.String(64), nullable=False, default=BUILD_STATUS_QUEUED)
    build_time_no_cache = db.Column(db.Float, nullable=True)
    build_time_with_cache = db.Column(db.Float, nullable=True)
    image_size = db.Column(db.Integer, nullable=True)
    is_valid = db.Column(db.Boolean, nullable=False, default=False)
    dockerfile_content = db.Column(db.Text, nullable=False)
    error = db.Column(db.Text, nullable=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    @hybrid_property
    def score(self):
        if self.build_time_no_cache is None or self.build_time_with_cache is None or self.image_size is None:
            return None
        return (
                weight_image_size
                * (
                    1
                    - (self.image_size - baseline_min_image_size)
                    / (baseline_max_image_size - baseline_min_image_size)
                )
                + weight_build_time_with_cache
                * (
                    1
                    - (
                        self.build_time_with_cache
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
                        self.build_time_no_cache
                        - baseline_min_build_time_no_cache
                    )
                    / (
                        baseline_max_build_time_no_cache
                        - baseline_min_build_time_no_cache
                    )
                )
            )
    
    @hybrid_property
    def score_rounded(self):
        return func.round(self.score * 1000, 0)

    def __repr__(self):
        return f"<BuildResult {self.id}>"


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    firstname = db.Column(db.String(50), nullable=False)
    lastname = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    affiliation = db.Column(db.String(120), nullable=True)
    uniqueID = db.Column(db.String(50), unique=True, nullable=False)

    def __repr__(self):
        return f"<User {self.firstname} {self.lastname}>"

# config admin
class SecureModelView(ModelView):
    def is_accessible(self):
        return is_admin(session.get("user_id"))

    # Redirect users who do not have access
    def inaccessible_callback(self, name, **kwargs):
        return redirect(url_for('index', next=request.url))

class UserAdmin(SecureModelView):
    column_list = ['id', 'firstname', 'lastname', 'email', 'affiliation', 'uniqueID']

class BuildResultAdmin(SecureModelView):
    column_list = [
        'id', 'user', 'build_status', 'build_time_no_cache', 
        'build_time_with_cache', 'image_size', 'is_valid', 
        'updated_at', 'score'
    ]
    form_columns = [
        'user', 'build_status', 'build_time_no_cache', 'build_time_with_cache', 
        'image_size', 'is_valid', 'dockerfile_content', 'error'
    ]

    # Link Formatter
    def user_link_formatter(view, context, model, name):
        if model.user is None:
            return ''
        return Markup('<a href="{}">{} {}</a>').format(
            url_for("user.edit_view", id=model.user.id),
            model.user.firstname,
            model.user.lastname,
        )

    column_formatters = {
        'user': user_link_formatter
    }

admin.add_view(UserAdmin(User, db))
admin.add_view(BuildResultAdmin(BuildResult, db))


class Queue:
    def __init__(self):
        self.queue = []
        self.condition = Condition()
        self.lock = Condition()
        self.message = {"message": "Queue is empty"}
        # used to store messages to be sent to the client on new connection
        self.messages = []

    def find_item_index_by_user_id(self, user_id):
        for i, item in enumerate(self.queue):
            if item["user_id"] == user_id:
                return i
        return -1

    def put(self, item):
        with self.condition:
            index = self.find_item_index_by_user_id(item["user_id"])
            if index >= 0:
                previous_item = self.queue[index]
                cancel_item(previous_item["id"])
                self.queue[index] = item
            else:
                self.queue.append(item)
            self.message = get_queue_order_message(self.queue)
            self.condition.notify_all()
            with self.lock:
                self.lock.notify()

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
            message["updated_at"] = datetime.now(timezone.utc).isoformat()
            self.message = message
            self.messages.append(message)
            self.condition.notify_all()


with app.app_context():
    db.create_all()
    app.logger.info(f"Admin emails configured: {ADMIN_EMAILS}")
    # Queuing system for Docker builds
    build_queue = Queue()
    # initialize queue from db get all BuildResults with status queued order by updated_at
    queued_builds = (
        BuildResult.query.filter_by(build_status=BUILD_STATUS_QUEUED)
        .order_by(BuildResult.updated_at)
        .all()
    )
    build_queue.queue = [buildresult_to_queue_item(build) for build in queued_builds]
    app.logger.info(f"Initialized queue with {len(build_queue.queue)} items")


def authentication_required():
    response = jsonify({"error": "Authentication required"})
    response.status_code = 401  # Unauthorized
    return response


def is_admin(user_id):
    """Check if a user is an admin by looking up their email"""
    if not user_id:
        return False
    user = db.session.get(User, user_id)
    if not user:
        return False
    return user.email in ADMIN_EMAILS


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return authentication_required()
        return f(*args, **kwargs)

    return decorated_function


def cancel_item(id):
    with app.app_context():
        build_result = BuildResult.query.get(id)
        build_result.build_status = BUILD_STATUS_CANCELED
        db.session.commit()
        current_app.logger.info(f"Build {id} canceled")


@contextmanager
def grading_lock():
    from fcntl import LOCK_EX, flock

    # Share the build daemon and its cache with the web worker one grader at a time.
    with open("/data/grading.lock", "a") as lock:
        flock(lock, LOCK_EX)
        yield


class BuildMeasurements(NamedTuple):
    build_time_no_cache: float | None
    build_time_with_cache: float | None
    image_size: int | None
    is_valid: bool
    error: str | None


def save_build_result(build_id, dockerfile, measurements, *, previous_status):
    values = {**measurements._asdict(), "build_status": BUILD_STATUS_COMPLETED}
    if previous_status == BUILD_STATUS_COMPLETED:
        # Keep the original tie-break timestamp during a regrade.
        values["updated_at"] = BuildResult.updated_at
    result = db.session.execute(
        db.update(BuildResult)
        .where(
            BuildResult.id == build_id,
            BuildResult.build_status == previous_status,
            BuildResult.dockerfile_content == dockerfile,
        )
        .values(values)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return result.rowcount == 1


def grade_build(build_id, previous_status):
    while True:
        dockerfile = db.session.scalar(
            db.select(BuildResult.dockerfile_content).where(
                BuildResult.id == build_id, BuildResult.build_status == previous_status
            )
        )
        if dockerfile is None:
            return None
        measurements = build_and_measure(dockerfile, build_id)
        if previous_status == BUILD_STATUS_COMPLETED and not measurements.is_valid:
            # A lost daemon must not turn all remaining attempts into failures.
            client.ping()
        if save_build_result(build_id, dockerfile, measurements, previous_status=previous_status):
            return measurements
        # An admin may have edited the Dockerfile or removed the attempt during grading.


@app.cli.command("regrade")
def regrade_completed_builds():
    """Rebuild and revalidate every completed submission in the stored results."""
    with grading_lock():
        client.ping()
        completed_ids = db.session.scalars(
            db.select(BuildResult.id)
            .where(BuildResult.build_status == BUILD_STATUS_COMPLETED)
            .order_by(BuildResult.id)
        ).all()
        regraded = 0
        for build_id in completed_ids:
            measurements = grade_build(build_id, BUILD_STATUS_COMPLETED)
            if measurements is None:
                click.echo(f"Build {build_id} skipped")
                continue
            regraded += 1
            verdict = "passed" if measurements.is_valid else "failed"
            click.echo(f"Build {build_id} {verdict}")
        click.echo(f"Regraded {regraded} completed attempts.")


def process_build_queue():
    while True:
        with app.app_context():
            build_data = build_queue.get()
            if build_data is None:
                break

            build_id = build_data["id"]
            with grading_lock():
                if grade_build(build_id, BUILD_STATUS_QUEUED) is None:
                    continue
                build_result = db.session.get(BuildResult, build_id)
                if build_result is None:
                    continue
            current_app.logger.info(f"Build {build_id} completed")
            build_queue.send_message({
                "message": "Build completed",
                 "id": build_id,
                 "is_valid": build_result.is_valid,
                 "score": round(build_result.score * 1000) if build_result.score else None,
                 "updated_at": build_result.updated_at.isoformat(),
                 "user_id": build_result.user_id,
            })


@app.route("/")
def index():
    user_id = session.get("user_id")
    queued_item_id = None
    last_build = None
    if user_id:
        last_build = (
            db.session.query(
                BuildResult.id,
                BuildResult.is_valid,
                BuildResult.score_rounded,
                BuildResult.updated_at,
            ).filter_by(
                user_id=user_id, build_status=BUILD_STATUS_COMPLETED
            )
            .order_by(BuildResult.updated_at.desc())
            .first()
        )
        index = build_queue.find_item_index_by_user_id(user_id)
        if index >= 0:
            queued_item_id = build_queue.queue[index]["id"]
        if last_build:
            last_build = {
                "id": last_build.id,
                "is_valid": last_build.is_valid,
                "score": last_build.score_rounded,
                "updated_at": last_build.updated_at.isoformat(),
            }

    return render_template(
        "index.html",
        user_id=user_id,
        last_build=last_build,
        queue=[item["id"] for item in build_queue.queue],
        queued_item_id=queued_item_id,
    )


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect("/")


@app.route("/login", methods=["POST"])
def login_handle():
    jwt_token = request.values.get("jwt")
    app.logger.info(f"Received JWT token: {jwt_token}")
    try:
        payload = jwt.decode(jwt_token, JWT_SECRET, algorithms=["HS256"], leeway=10)
    except jwt.InvalidTokenError:
        return "Invalid token", 400

    email = payload.get("email")
    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(
            firstname=payload.get("firstname"),
            lastname=payload.get("lastname"),
            email=email,
            affiliation=payload.get("affiliation"),
            uniqueID=payload.get("uniqueID"),
        )
        db.session.add(user)
        db.session.commit()

    app.logger.info(f"User {payload} logged in")
    session["user_id"] = user.id

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


@app.route("/build/<int:id>")
@login_required
def get_build(id):
    build_result = BuildResult.query.get(id)
    if build_result is None:
        return jsonify({"error": "Build not found"}), 404

    response = {
        "id": build_result.id,
        "user_id": build_result.user_id,
        "build_status": build_result.build_status,
        "build_time_no_cache": round(build_result.build_time_no_cache, 1) if build_result.build_time_no_cache else None,
        "build_time_with_cache": round(build_result.build_time_with_cache, 1) if build_result.build_time_with_cache else None,
        "image_size": size_to_mb(build_result.image_size) if build_result.image_size else None,
        "is_valid": build_result.is_valid,
        "error": build_result.error,
        "score": round(build_result.score * 1000) if build_result.score else None,
        "score_raw": build_result.score,
        "updated_at": build_result.updated_at.isoformat(),
    }
    if build_result.user_id == session["user_id"] or is_admin(session.get("user_id")):
        response["dockerfile_content"] = build_result.dockerfile_content
        response["firstname"] = build_result.user.firstname
        response["lastname"] = build_result.user.lastname
        response["email"] = build_result.user.email
    return jsonify(response)


@app.route("/submit", methods=["POST"])
@login_required
def submit_dockerfile():
    data = request.files.get("dockerfile")

    if not data:
        return jsonify({"error": "No Dockerfile provided"}), 400

    dockerfile_content = data.read().decode("utf-8")

    # Save initial build data to the database
    build_result = BuildResult(
        user_id=session["user_id"],
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
    current_app.logger.info(f"Build {build_result.id} queued")
    build_queue.put(buildresult_to_queue_item(build_result))

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


def remove_build_image(image, build_id):
    try:
        image.remove(force=True)
    except docker.errors.NotFound:
        pass
    except (docker.errors.DockerException, OSError):
        current_app.logger.exception("Could not remove an image for build %s", build_id)


def build_and_measure(dockerfile_content, id):
    build_time_no_cache = build_time_with_cache = image_size = None
    img1 = img2 = None
    try:
        tag = f"build-{id}"
        current_app.logger.info(f"Building and measuring Dockerfile with tag {tag}")
        build_queue.send_message({"message": f"Build started", "id": id})
        # First build without cache
        start_time = time.time()
        img1, logs = client.images.build(
            path=str(CHALLENGE_PATH), dockerfile=dockerfile_content, tag=tag, nocache=True
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

        remove_build_image(img1, id)
        img1 = None
        # Second build with cache
        start_time = time.time()
        img2, logs = client.images.build(
            path=str(WARM_CHALLENGE_PATH), dockerfile=dockerfile_content, tag=tag
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
        current_app.logger.info(f"Container is valid: {is_valid}")
        build_queue.send_message(
            {"message": f"Container is valid: {is_valid}", "id": id}
        )

        return BuildMeasurements(build_time_no_cache, build_time_with_cache, image_size, is_valid, error)

    except Exception as e:
        current_app.logger.info(e)
        return BuildMeasurements(build_time_no_cache, build_time_with_cache, image_size, False, str(e))
    finally:
        for image in (img2, img1):
            if image is not None:
                remove_build_image(image, id)


class ValidationError(Exception):
    pass


class GuestbookPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.has_mount = False
        self.module_scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and attrs.get("id") == "app":
            self.has_mount = True
        if tag == "script" and attrs.get("type") == "module" and attrs.get("src"):
            self.module_scripts.append(attrs["src"])


def verify_supplied_app(container, working_dir, expected_source):
    file_path = PurePosixPath(working_dir or "/") / "app.py"
    for _ in range(8):
        try:
            archive, stat = container.get_archive(str(file_path))
        except docker.errors.NotFound as error:
            raise ValidationError("app.py is missing from the image's WORKDIR") from error

        with closing(archive):
            if stat.get("linkTarget"):
                target = file_path.parent / stat["linkTarget"]
                file_path = PurePosixPath(posixpath.normpath(str(target)))
                continue
            if stat["size"] != len(expected_source):
                raise ValidationError("app.py must match the supplied Guestbook file")
            data = bytearray()
            for chunk in archive:
                data.extend(chunk)
                if len(data) > len(expected_source) + 64 * 1024:
                    raise ValidationError("app.py must match the supplied Guestbook file")
            break
    else:
        raise ValidationError("Could not resolve app.py from the image's WORKDIR")

    try:
        with open_tar(fileobj=BytesIO(data), mode="r:") as tar:
            member = tar.next()
            if member and member.name == file_path.name and member.isfile():
                if member.size == len(expected_source) and tar.extractfile(member).read() == expected_source:
                    return
    except TarError as error:
        raise ValidationError("Could not read app.py from the image") from error
    raise ValidationError("app.py must match the supplied Guestbook file")


def guestbook_request(port, method, path, status, body=None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    with closing(HTTPConnection("dind", port, timeout=2)) as connection:
        connection.request(method, path, body=body, headers=headers)
        with connection.getresponse() as response:
            if response.status != status:
                target = path if path in ("/", "/entry", "/entries") else "Guestbook JavaScript"
                raise ValidationError(
                    f"{method} {target} returned HTTP {response.status}, expected {status}"
                )
            content = response.read(MAX_PROBE_BODY + 1)
            if len(content) > MAX_PROBE_BODY:
                raise ValidationError("Guestbook response exceeded the size limit")
            return response.headers.get_content_type(), content


def check_guestbook(port):
    startup_deadline = time.monotonic() + 15
    while True:
        try:
            content_type, homepage = guestbook_request(port, "GET", "/", 200)
            break
        except (ConnectionError, TimeoutError) as error:
            if time.monotonic() >= startup_deadline:
                raise ValidationError("Guestbook did not respond on the assigned PORT") from error
            time.sleep(0.25)

    if content_type != "text/html":
        raise ValidationError("Guestbook homepage must be served as HTML")
    page = GuestbookPage()
    page.feed(homepage.decode("utf-8", errors="replace"))
    page.close()
    if not page.has_mount or not page.module_scripts:
        raise ValidationError("Homepage did not include the Guestbook frontend")
    if len(page.module_scripts) > 8:
        raise ValidationError("Guestbook homepage included too many JavaScript modules")

    for script in page.module_scripts:
        asset = urlsplit(urljoin("/", script))
        if asset.scheme or asset.netloc or not asset.path.startswith("/"):
            raise ValidationError("Guestbook JavaScript must be served by the container")
        asset_path = asset.path + (f"?{asset.query}" if asset.query else "")
        content_type, javascript = guestbook_request(port, "GET", asset_path, 200)
        if not javascript.strip() or content_type not in (
            "text/javascript", "application/javascript", "text/ecmascript", "application/ecmascript",
        ):
            raise ValidationError("Guestbook JavaScript did not load")

    entry = {
        "name": "Guestbook validator",
        "email": "validator@example.invalid",
        "message": uuid4().hex,
    }
    guestbook_request(port, "POST", "/entry", 204, body=json.dumps(entry))
    _, body = guestbook_request(port, "GET", "/entries", 200)
    try:
        entries = json.loads(body)
    except (ValueError, RecursionError) as error:
        raise ValidationError("Guestbook entries were not valid JSON") from error
    if not isinstance(entries, list) or not any(
        isinstance(saved, dict) and all(saved.get(key) == value for key, value in entry.items())
        for saved in entries
    ):
        raise ValidationError("Guestbook entries did not include the newly submitted entry")


def validate_container(id, image):
    ports = SystemRandom().sample(range(10000, 65000), 3)
    expected_source = (WARM_CHALLENGE_PATH / "app.py").read_bytes()
    runs = (
        ("Normal startup (first port)", ports[0], {}),
        ("Normal startup (second port)", ports[1], {}),
        ("Direct python app.py startup", ports[2], {"entrypoint": ["python"], "command": ["app.py"]}),
    )
    for label, port, startup in runs:
        container = None
        try:
            build_queue.send_message({"message": f"Validating {label.lower()}", "id": id})
            container_port = f"{port}/tcp"
            container = client.containers.create(
                image=image,
                security_opt=["no-new-privileges"],
                cpu_shares=512,
                mem_limit="512m",
                environment={"PORT": str(port)},
                ports={container_port: None},
                **startup,
            )
            if startup:
                verify_supplied_app(container, image.attrs["Config"].get("WorkingDir"), expected_source)
            container.start()
            container.reload()
            host_port = int(container.ports[container_port][0]["HostPort"])
            check_guestbook(host_port)
        except ValidationError as error:
            return False, f"{label}: {error}"
        except (docker.errors.APIError, HTTPException, OSError, ValueError, KeyError, TypeError, IndexError):
            current_app.logger.exception("Build %s failed during %s", id, label)
            return False, f"{label}: could not start or contact the Guestbook container"
        finally:
            if container is not None:
                try:
                    container.remove(v=True, force=True)
                except docker.errors.NotFound:
                    pass
                except (docker.errors.DockerException, OSError):
                    current_app.logger.exception("Could not remove a container for build %s", id)

    return True, None


@app.route("/scores", methods=["GET"])
def get_scores():
    score_subquery = (
        db.session.query(
            BuildResult.id,
            BuildResult.user_id,
            BuildResult.score,
        )
        .filter(
            BuildResult.build_time_with_cache.isnot(None),
            BuildResult.image_size.isnot(None),
            BuildResult.build_time_no_cache.isnot(None),
            BuildResult.build_status == BUILD_STATUS_COMPLETED,
            BuildResult.is_valid == True,
        )
        .subquery()
    )

    # Subquery to find the maximum score per user_id with ranking
    rank_subquery = (
        db.session.query(
            score_subquery.c.user_id,
            score_subquery.c.id,
            score_subquery.c.score.label("score"),
            func.row_number().over(
                partition_by=score_subquery.c.user_id,
                order_by=score_subquery.c.score.desc()
            ).label('rank')
        )
        .subquery()
    )

     # Subquery to find the maximum score per user_id with ranking
    max_score_subquery = (
        db.session.query(
            rank_subquery.c.user_id,
            rank_subquery.c.id.label("best_result_id"),
            rank_subquery.c.score
        )
        .filter(rank_subquery.c.rank == 1)
        .subquery()
    )

    # Subquery to count total attempts including failed ones
    total_attempts_subquery = (
        db.session.query(
            BuildResult.user_id, func.count(BuildResult.id).label("total_attempts")
        )
        .filter(BuildResult.build_status == BUILD_STATUS_COMPLETED)
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
            User.lastname,
            BuildResult.build_time_no_cache,
            BuildResult.build_time_with_cache,
            BuildResult.image_size,
            BuildResult.updated_at,
            BuildResult.score,
            BuildResult.score_rounded,
            total_attempts_subquery.c.total_attempts,
            func.row_number().over(order_by=(desc(BuildResult.score), asc(BuildResult.updated_at))).label('rank')
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
            & (score_subquery.c.id == max_score_subquery.c.best_result_id),
        )
        .order_by(BuildResult.score.desc(), BuildResult.updated_at.asc())
        .all()
    )

    first_name_counts = Counter(row.firstname.strip().casefold() for row in best_results)
    scores = []
    for (
        id,
        user_id,
        firstname,
        lastname,
        build_time_no_cache,
        build_time_with_cache,
        image_size,
        updated_at,
        score,
        score_rounded,
        total_attempts,
        rank
    ) in best_results:
        display_name = firstname.strip()
        initial = lastname.strip()[:1]
        if first_name_counts[display_name.casefold()] > 1 and initial:
            display_name = f"{display_name} {initial}."
        scores.append(
            {
                "id": id,
                "user_id": user_id,
                "firstname": firstname,
                "display_name": display_name,
                "build_time_no_cache": round(build_time_no_cache, 1),
                "build_time_with_cache": round(build_time_with_cache, 1),
                "image_size": size_to_mb(image_size),
                "updated_at": updated_at.isoformat(),
                "score_raw": score,
                "score": score_rounded,
                "total_attempts": total_attempts,
                "rank": rank
            }
        )

    return jsonify(scores)


if __name__ == "__main__":
    Thread(target=process_build_queue, daemon=True).start()
    app.run(host="0.0.0.0", use_reloader=False)
