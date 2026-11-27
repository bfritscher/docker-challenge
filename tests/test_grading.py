import unittest
from contextlib import nullcontext
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
        self.context = grader.app.app_context()
        self.context.push()
        self.addCleanup(grader.db.engine.dispose)
        self.addCleanup(self.context.pop)
        grader.db.create_all()
        self.addCleanup(grader.db.drop_all)

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
            result = grader.app.test_cli_runner().invoke(args=["regrade"])

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
            result = grader.app.test_cli_runner().invoke(args=["regrade"])

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
