import unittest
from subprocess import CompletedProcess, TimeoutExpired
from contextlib import nullcontext
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep
from unittest.mock import patch

import docker
from flask_sqlalchemy import SQLAlchemy


init_app = SQLAlchemy.init_app


def use_test_database(extension, app):
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    init_app(extension, app)


with (
    patch.object(SQLAlchemy, "init_app", use_test_database),
    patch.object(docker.tls, "TLSConfig"),
    patch.object(docker, "DockerClient"),
):
    import app as grader


class GradingTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.checkpoint = Path(directory.name) / "regrade.json"
        self.context = grader.app.app_context()
        self.context.push()
        self.addCleanup(grader.db.engine.dispose)
        self.addCleanup(self.context.pop)
        grader.db.create_all()
        self.addCleanup(grader.db.drop_all)

    def regrade(self):
        return grader.app.test_cli_runner().invoke(
            args=["regrade", "--checkpoint", str(self.checkpoint)]
        )

    def add_build(self, firstname, lastname, *, valid=True):
        user = grader.User(
            firstname=firstname,
            lastname=lastname,
            email=f"{lastname}@example.test",
            uniqueID=lastname,
        )
        build = grader.BuildResult(
            user=user,
            build_status=grader.BUILD_STATUS_COMPLETED,
            dockerfile_content="FROM scratch",
            build_time_no_cache=1,
            build_time_with_cache=1,
            image_size=1,
            is_valid=valid,
        )
        grader.db.session.add(build)
        grader.db.session.commit()
        return build

    def test_scores_add_initials_only_for_duplicate_ranked_names(self):
        self.add_build("Alex", "Miller")
        self.add_build(" alex ", "Reed")
        self.add_build("Sam", "Jones")
        self.add_build("sam", "Clark", valid=False)

        response = grader.app.test_client().get("/scores")

        self.assertEqual(response.status_code, 200)
        scores = response.get_json()
        self.assertEqual(
            {row["display_name"] for row in scores}, {"Alex M.", "alex R.", "Sam"}
        )
        self.assertTrue(all("lastname" not in row for row in scores))

    def test_regrade_updates_each_completed_attempt_and_preserves_its_timestamp(self):
        first = self.add_build("Alex", "Miller")
        second = grader.BuildResult(
            user=first.user,
            build_status=grader.BUILD_STATUS_COMPLETED,
            dockerfile_content="FROM second",
            is_valid=False,
        )
        grader.db.session.add(second)
        pending = self.add_build("Sam", "Jones")
        pending.build_status = grader.BUILD_STATUS_QUEUED
        canceled = self.add_build("Pat", "Clark")
        canceled.build_status = grader.BUILD_STATUS_CANCELED
        grader.db.session.commit()
        first_id, second_id = first.id, second.id
        first_timestamp, second_timestamp = first.updated_at, second.updated_at

        with (
            patch.object(grader, "build_and_measure", side_effect=[
                grader.BuildMeasurements(2, 3, 4, False, "Guestbook API check failed"),
                grader.BuildMeasurements(5, 6, 7, True, None),
            ]) as build,
            patch.object(grader.client, "ping", return_value=True),
            patch.object(grader, "grading_lock", nullcontext),
        ):
            result = self.regrade()

        self.assertEqual(result.exit_code, 0, result.output or result.exception)
        self.assertEqual([call.args for call in build.call_args_list], [
            ("FROM scratch", first_id), ("FROM second", second_id),
        ])
        self.assertFalse(first.is_valid)
        self.assertEqual(first.error, "Guestbook API check failed")
        self.assertEqual(first.updated_at, first_timestamp)
        self.assertEqual(second.updated_at, second_timestamp)
        self.assertEqual(pending.build_status, grader.BUILD_STATUS_QUEUED)
        self.assertEqual(canceled.build_status, grader.BUILD_STATUS_CANCELED)
        scores = grader.app.test_client().get("/scores").get_json()
        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0]["id"], second_id)
        self.assertEqual(scores[0]["total_attempts"], 2)

    def test_regrade_resumes_after_a_committed_failure_and_interruption(self):
        first = self.add_build("Alex", "Miller")
        second = self.add_build("Sam", "Jones")
        failed = grader.BuildMeasurements(1, 1, 1, False, "Assigned PORT did not respond")
        passed = grader.BuildMeasurements(1, 1, 1, True, None)

        with (
            patch.object(grader, "grading_lock", nullcontext),
            patch.object(
                grader, "build_and_measure", side_effect=[failed, KeyboardInterrupt(), passed]
            ) as measure,
        ):
            interrupted = self.regrade()
            self.assertEqual(interrupted.exit_code, 1)
            self.assertIn(f"Build {first.id} failed", interrupted.output)
            self.assertTrue(self.checkpoint.is_file())

            resumed = self.regrade()
            self.assertEqual(resumed.exit_code, 0, resumed.output or resumed.exception)
            self.assertIn("1 already regraded", resumed.output)

        self.assertEqual([call.args[1] for call in measure.call_args_list], [
            first.id, second.id, second.id,
        ])
        self.assertFalse(first.is_valid)
        self.assertTrue(second.is_valid)
        self.assertFalse(self.checkpoint.exists())

    def test_regrade_reports_a_slow_build_before_its_verdict(self):
        build = self.add_build("Alex", "Miller")

        def fail_build(*_, **options):
            sleep(0.08)
            raise TimeoutExpired("build worker", options["timeout"])

        with (
            patch.object(grader, "grading_lock", nullcontext),
            patch.object(grader, "GRADING_HEARTBEAT", 0.01),
            patch.object(grader.subprocess, "run", side_effect=fail_build),
        ):
            result = self.regrade()

        self.assertEqual(result.exit_code, 0, result.output or result.exception)
        self.assertIn(f"Regrading build {build.id}", result.output)
        self.assertIn(f"Build {build.id}: still building without cache", result.output)
        self.assertIn("exceeded the 200-second limit while building without cache", result.output)
        self.assertIn(f"Build {build.id} failed", result.output)
        self.assertFalse(build.is_valid)
        self.assertIsNone(build.build_time_with_cache)

    def test_warm_build_has_its_own_deadline_and_preserves_cold_measurements(self):
        build = self.add_build("Alex", "Miller")
        with (
            patch.object(grader, "grading_lock", nullcontext),
            patch.object(grader.client.images.get.return_value, "attrs", {"Size": 42}),
            patch.object(
                grader.subprocess, "run",
                side_effect=[CompletedProcess([], 0), TimeoutExpired("build worker", 200)],
            ) as run,
            patch.object(grader, "validate_container") as validate,
        ):
            result = self.regrade()

        self.assertEqual(result.exit_code, 0, result.output or result.exception)
        self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [200, 200])
        self.assertFalse(build.is_valid)
        self.assertEqual(build.image_size, 42)
        self.assertIsNotNone(build.build_time_no_cache)
        self.assertIsNone(build.build_time_with_cache)
        self.assertIn("exceeded the 200-second limit while building with cache", build.error)
        validate.assert_not_called()

    def test_regrade_uses_an_admin_edit_made_during_the_build(self):
        build = self.add_build("Alex", "Miller")
        build_id = build.id

        def measure(dockerfile, _build_id):
            if dockerfile == "FROM scratch":
                build.dockerfile_content = "FROM edited"
                grader.db.session.commit()
                return grader.BuildMeasurements(1, 1, 1, False, "Old Dockerfile failed")
            return grader.BuildMeasurements(2, 2, 2, True, None)

        with (
            patch.object(grader, "build_and_measure", side_effect=measure) as run,
            patch.object(grader, "grading_lock", nullcontext),
        ):
            result = self.regrade()

        self.assertEqual(result.exit_code, 0, result.output or result.exception)
        self.assertEqual(
            [call.args for call in run.call_args_list],
            [("FROM scratch", build_id), ("FROM edited", build_id)],
        )
        self.assertTrue(build.is_valid)
        self.assertIsNone(build.error)
        self.assertEqual(build.image_size, 2)

    def test_admin_can_open_and_save_the_build_edit_form(self):
        build = self.add_build("Alex", "Miller")
        edit_url = f"/admin/buildresult/edit/?id={build.id}"
        client = grader.app.test_client()
        self.assertEqual(client.get(edit_url).status_code, 302)

        with (
            patch.dict(grader.app.config, SECRET_KEY="test-admin-session"),
            patch.object(grader, "ADMIN_EMAILS", [build.user.email]),
        ):
            with client.session_transaction() as session:
                session["user_id"] = build.user_id

            self.assertEqual(client.get("/admin/buildresult/").status_code, 200)
            user_edit = client.get(f"/admin/user/edit/?id={build.user_id}")
            self.assertEqual(user_edit.status_code, 200)
            form = client.get(edit_url)
            self.assertEqual(form.status_code, 200)
            self.assertIn(b"dockerfile_content", form.data)
            saved = client.post(edit_url, data={
                "user": build.user_id,
                "build_status": grader.BUILD_STATUS_COMPLETED,
                "build_time_no_cache": 1,
                "build_time_with_cache": 1,
                "image_size": 1,
                "is_valid": "y",
                "dockerfile_content": "FROM scratch",
                "error": "Edited in admin",
            })

        self.assertEqual(saved.status_code, 302)
        grader.db.session.refresh(build)
        self.assertEqual(build.error, "Edited in admin")


class GuestbookProbeTest(unittest.TestCase):
    def test_a_slow_stream_cannot_keep_a_probe_open(self):
        class SlowResponse(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", "20")
                self.end_headers()
                try:
                    for _ in range(20):
                        self.wfile.write(b"x")
                        sleep(0.04)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        with HTTPServer(("127.0.0.1", 0), SlowResponse) as server:
            thread = Thread(target=server.handle_request, daemon=True)
            thread.start()
            try:
                with (
                    patch.object(grader, "PROBE_TIMEOUT", 0.2),
                    patch.object(
                        grader, "HTTPConnection",
                        side_effect=lambda _, port, timeout: HTTPConnection("127.0.0.1", port, timeout=timeout),
                    ),
                    self.assertRaisesRegex(TimeoutError, "Guestbook HTTP probe timed out"),
                ):
                    grader.guestbook_request(server.server_port, "GET", "/", 200)
            finally:
                thread.join(timeout=2)
