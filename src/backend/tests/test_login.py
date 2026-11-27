import os
import unittest
from contextlib import closing
from unittest.mock import patch
from uuid import uuid4

import psycopg2
from argon2 import PasswordHasher
from django.db import OperationalError
from django.test import Client

from apps.accounts import repository, services
from tests.database import use_database
from psycopg2 import sql
from psycopg2.extensions import make_dsn



class LoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        password_hash = PasswordHasher().hash("TestPass1!")
        accounts = dict.fromkeys(
            ("user1@example.com", "abcdefghijklmno@example.com"), password_hash
        )
        lookup = patch.object(
            repository,
            "find_password_hash",
            side_effect=lambda email: accounts.get(email),
        )
        cls.lookup = lookup.start()
        cls.addClassCleanup(lookup.stop)
        session = patch.object(repository, "issue_session", return_value="synthetic-session")
        session.start()
        cls.addClassCleanup(session.stop)

    def setUp(self):
        self.client = Client()

    def login(self, email, password="TestPass1!"):
        return self.client.post(
            "/api/auth/login", data={"email": email, "password": password}, content_type="application/json"
        )

    def test_accepts_registered_emails_at_both_length_limits(self):
        for email in ("USER1@EXAMPLE.COM", "abcdefghijklmno@example.com"):
            with self.subTest(email=email):
                response = self.login(email)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"email": email.lower()})

    def test_rejects_invalid_email_before_authentication(self):
        self.lookup.reset_mock()
        for email in (
            "abcd@example.com",
            "abcdefghijklmnop@example.com",
            "user_1@example.com",
            "사용자12@example.com",
            "Kuser@example.com",
            "user1@-example.com",
            "user1@",
        ):
            with self.subTest(email=email):
                response = self.login(email)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"]["code"], "INVALID_EMAIL")
        self.lookup.assert_not_called()

    def test_returns_same_failure_for_unknown_email_and_wrong_password(self):
        wrong_password = self.login("user1@example.com", "Incorrect1!")
        unknown_email = self.login("unknown@example.com")
        self.assertEqual(wrong_password.status_code, 401)
        self.assertEqual(unknown_email.status_code, 401)
        self.assertEqual(wrong_password.json(), unknown_email.json())
        self.assertEqual(wrong_password.json()["detail"]["code"], "INVALID_CREDENTIALS")

    def test_requires_password(self):
        response = self.login("user1@example.com", "")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "PASSWORD_REQUIRED")

    def test_does_not_echo_password_in_invalid_request(self):
        response = self.login(None, {"secret": "TestPass1!"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "INVALID_REQUEST")
        self.assertNotIn("TestPass1!", response.text)

    def test_database_error_returns_generic_service_error(self):
        with patch.object(
            repository,
            "find_password_hash",
            side_effect=OperationalError("private-db-host"),
        ), self.assertLogs(services.__name__, level="ERROR") as logs:
            response = self.login("user1@example.com")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "AUTH_UNAVAILABLE")
        self.assertNotIn("private-db-host", response.text)
        self.assertNotIn("private-db-host", "\n".join(logs.output))


@unittest.skipUnless(os.environ.get("VOC_TEST_DATABASE_URL"), "Set VOC_TEST_DATABASE_URL")
class PostgreSQLLoginTests(unittest.TestCase):
    def test_login_checks_employee_company_and_reads_database_each_time(self):
        dsn = os.environ["VOC_TEST_DATABASE_URL"]
        schema = f"login_test_{uuid4().hex}"
        with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
            connection.autocommit = True
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            self.addCleanup(self.drop_schema, dsn, schema)
            cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            cursor.execute(
                "CREATE TABLE company (company_id uuid PRIMARY KEY, email_domain text NOT NULL)"
            )
            cursor.execute("""
                CREATE TABLE staff_user (
                    staff_user_id uuid PRIMARY KEY,
                    company_id uuid NOT NULL REFERENCES company (company_id),
                    email text NOT NULL,
                    password_hash text NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE staff_session (
                    token_hash text PRIMARY KEY, staff_user_id uuid REFERENCES staff_user,
                    expires_at timestamptz NOT NULL)
            """)
            company_id = str(uuid4())
            cursor.execute("INSERT INTO company VALUES (%s, %s)", (company_id, "EXAMPLE.COM"))
            cursor.execute("INSERT INTO company VALUES (%s, %s)", (str(uuid4()), "other.example.com"))
            hasher = PasswordHasher()
            cursor.execute(
                "INSERT INTO staff_user VALUES (%s, %s, %s, %s)",
                (str(uuid4()), company_id, "User01@Example.com", hasher.hash("TestPass1!")),
            )

            test_dsn = make_dsn(dsn, options=f"-c search_path={schema}")
            with use_database(test_dsn):
                client = Client()
                credentials = {"email": "USER01@example.com", "password": "TestPass1!"}
                response = client.post("/api/auth/login", data=credentials, content_type="application/json")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"email": "user01@example.com"})

                duplicate_uuid = str(uuid4())
                cursor.execute(
                    "INSERT INTO staff_user VALUES (%s, %s, %s, %s)",
                    (duplicate_uuid, company_id, "user01@example.com", hasher.hash("OtherPass1!")),
                )
                self.assertEqual(client.post("/api/auth/login", data=credentials, content_type="application/json").status_code, 401)
                cursor.execute("DELETE FROM staff_user WHERE staff_user_id = %s", (duplicate_uuid,))

                cursor.execute(
                    "UPDATE company SET email_domain = %s WHERE company_id = %s",
                    ("different.example.com", company_id),
                )
                self.assertEqual(client.post("/api/auth/login", data=credentials, content_type="application/json").status_code, 401)

    @staticmethod
    def drop_schema(dsn, schema):
        with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
            connection.autocommit = True
            cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
